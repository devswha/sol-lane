"""OpenAI-compatible shim in front of the CDP engine.

gjc speaks `/v1/chat/completions` with `stream: true`; the engine speaks
one-prompt-in / one-answer-out. This module is the translation, and it is where
every Sol Pro constraint is enforced:

- one browser, one conversation → requests are serialized behind a lock
- the engine opens a fresh chat per call → the whole transcript is re-rendered
  into the prompt instead of relying on ChatGPT-side history
- a non-zero engine exit or an empty answer is an error, never an empty
  completion: gjc must not mistake a failed delivery for a model that had
  nothing to say
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import locks, proc
from . import tools as tools_module
from .review import browser_env, refusal_in

ROLE_LABELS = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
MAX_CONTENT_LENGTH = 1_048_576
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_CONCURRENT_HANDLERS = 16
TRANSCRIPT_HEADER = (
    "You are the assistant in an ongoing conversation. The full transcript follows. "
    "Reply with the assistant's next message only — no transcript, no role labels, "
    "no restatement of the question."
)


class ServeError(Exception):
    """Engine delivery failed. Surfaced to the client as HTTP 502."""


@dataclass(frozen=True)
class ServeSettings:
    engine: Path
    model: str = "pro"
    require_model: str = "GPT-5.6"
    max_wait: int = 1200
    force_answer_after: int = 0
    python: str | None = None
    # A token protects a loopback endpoint shared with other local users.
    token: str | None = None
    # Pro reasons for minutes, and gjc aborts a stream whose first *event* has
    # not arrived within retry.streamFirstEventTimeoutMs (100s by default),
    # then retries — spending another Pro message on the same question. SSE
    # comments do not count as events, so the heartbeat has to be a real chunk.
    heartbeat_seconds: float = 10.0

    def __post_init__(self):
        if self.token == "":
            raise ValueError("serve token must be non-empty when configured")

    def command(self, prompt: str) -> list[str]:
        args = [
            self.python or sys.executable,
            str(self.engine),
            "--model", self.model,
            "--require-model", self.require_model,
            "--max-wait", str(self.max_wait),
            "--no-project",
            "--council",
        ]
        if self.force_answer_after:
            args += ["--force-answer-after", str(self.force_answer_after)]
        return [*args, prompt]


def text_of(content: object) -> str:
    """Flatten OpenAI content: a string, or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def offered_tools(request: dict) -> list[dict]:
    """The tools this request actually puts on the table.

    `tool_choice: "none"` means the caller wants prose, so the specs are not
    rendered and a fenced block in the reply is just text.
    """
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return []
    if request.get("tool_choice") == "none":
        return []
    return [tool for tool in tools if isinstance(tool, dict)]


def unsupported_request(request: dict) -> str | None:
    """Why this request cannot be served honestly, if it cannot.

    Tools are bridged now (see tools.py), but a *forced* call is not: the bridge
    asks in prose and cannot make Pro comply, so promising `required` would be a
    lie the caller acts on.
    """
    choice = request.get("tool_choice")
    if choice is not None and choice not in ("none", "auto"):
        return (f"tool_choice {json.dumps(choice)} cannot be enforced through a prose "
                "bridge; use \"auto\"")
    for message in request.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list) and any(
            not (isinstance(part, dict) and isinstance(part.get("text"), str)) for part in content
        ):
            return "only text content parts are supported"
    return None


def invalid_request(request: object) -> str | None:
    """Return an admission error before rendering untrusted request data."""
    if not isinstance(request, dict):
        return "request body must be a JSON object"
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    if "model" in request and (not isinstance(request["model"], str) or not request["model"].strip()):
        return "model must be a non-empty string"
    if "stream" in request and not isinstance(request["stream"], bool):
        return "stream must be a boolean"
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages[{index}] must be an object"
        role = message.get("role")
        if role not in ROLE_LABELS:
            return f"messages[{index}].role is not supported"
        content = message.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list) and all(
            isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        ):
            continue
        if role == "assistant" and content is None and isinstance(message.get("tool_calls"), list):
            continue
        return f"messages[{index}].content must be text"
    tools = request.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            return "tools must be an array"
        for index, tool in enumerate(tools):
            function = tool.get("function") if isinstance(tool, dict) else None
            if (not isinstance(function, dict) or tool.get("type") != "function"
                    or not isinstance(function.get("name"), str) or not function["name"]):
                return f"tools[{index}] must declare a function name"
    return None


def render_prompt(messages: list[dict], tools: list[dict] | None = None) -> str:
    """Render a chat transcript the engine can send as a single prompt.

    Tool traffic is rendered as prose because the model never sees the OpenAI
    envelope: a call it made last turn and the result it got back have to read as
    part of the conversation or the next turn repeats the call.
    """
    blocks = [TRANSCRIPT_HEADER, ""]
    if tools:
        blocks.append(tools_module.render_tools(tools))
    for message in messages:
        role = str(message.get("role"))
        if role == "tool":
            body = tools_module.render_tool_result(message).strip()
        else:
            body = text_of(message.get("content")).strip()
            calls = tools_module.render_assistant_calls(message)
            body = f"{body}\n{calls}".strip() if calls else body
        if not body:
            continue
        blocks.append(f"[{ROLE_LABELS.get(role, 'USER')}]\n{body}")
    blocks.append("[ASSISTANT]")
    return "\n\n".join(blocks)


def estimate_tokens(text: str) -> int:
    """Rough token count. gjc rejects completions reporting implausibly few."""
    return max(1, len(text) // 4)


def run_engine(settings: ServeSettings, prompt: str) -> str:
    try:
        with locks.exclusive(locks.browser_lock_path()):
            result = proc.run(settings.command(prompt), timeout=settings.max_wait + 120,
                              env=browser_env())
    except (OSError, subprocess.SubprocessError) as error:
        raise ServeError(f"engine could not run: {error}") from error
    if result.returncode != 0:
        raise ServeError(f"engine exited {result.returncode}: {result.detail()}")
    answer = result.stdout.strip()
    if not answer:
        raise ServeError("engine returned an empty answer (fail-closed)")
    # The same false success `lane review` learned to reject: a refusal page is a
    # delivered page, not a delivered answer. Here it would flow into a gjc agent
    # loop as an assistant turn and be acted on.
    refusal = refusal_in(answer)
    if refusal is not None:
        raise ServeError(f"engine delivered a page but not an answer — {refusal}")
    return answer


def completion_payload(model: str, answer: str, prompt: str, *, delta: bool,
                       calls: tuple[tools_module.ToolCall, ...] = ()) -> dict:
    # A tool call carries no prose of its own; content must be null, not "", or a
    # client renders an empty assistant turn beside the call.
    content = (answer.strip() or None) if calls else answer
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [call.as_openai() for call in calls]
    stop_reason = "tool_calls" if calls else "stop"
    choice: dict = {"index": 0, "finish_reason": None if delta else stop_reason}
    choice["delta" if delta else "message"] = message
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(answer)
    return {
        "id": "chatcmpl-lane",
        "object": "chat.completion.chunk" if delta else "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def split_reply(answer: str, allowed: list[str]) -> tuple[str, list[tools_module.ToolCall]]:
    """Separate prose from tool calls, but only when tools were on the table.

    With no tools offered there is nothing to call, so a fenced block is just
    text the caller asked for — parsing it would turn an answer into an error.
    """
    if not allowed:
        return answer, []
    return tools_module.parse_reply(answer, allowed=allowed)


def json_frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def heartbeat_frame(model: str) -> bytes:
    """A content-free chunk that still counts as a stream event."""
    return json_frame({
        "id": "chatcmpl-lane",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    })


def sse_frames(model: str, answer: str, prompt: str,
               calls: tuple[tools_module.ToolCall, ...] = ()) -> list[bytes]:
    opening = completion_payload(model, answer, prompt, delta=True, calls=calls)
    closing = completion_payload(model, answer, prompt, delta=True, calls=calls)
    closing["choices"][0]["delta"] = {}
    closing["choices"][0]["finish_reason"] = "tool_calls" if calls else "stop"
    frames = [f"data: {json.dumps(payload)}\n\n".encode() for payload in (opening, closing)]
    frames.append(b"data: [DONE]\n\n")
    return frames


def make_handler(settings: ServeSettings, *, runner=run_engine, lock=None, log=print):
    guard = lock or threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            self._admission_timer = None
            self._admission_expired = False

        def handle_one_request(self):
            """Turn slow request lines and headers into an explicit timeout."""
            self._admission_timer = threading.Timer(
                REQUEST_TIMEOUT_SECONDS,
                self._expire_admission,
            )
            self._admission_timer.daemon = True
            self._admission_timer.start()
            try:
                self.raw_requestline = self.rfile.readline(65537)
                if len(self.raw_requestline) > 65536:
                    self.requestline = self.request_version = self.command = ""
                    self.send_error(414)
                    return
                if not self.raw_requestline:
                    self.close_connection = True
                    return
                if not self.parse_request():
                    return
                method = getattr(self, f"do_{self.command}", None)
                if method is None:
                    self._wrong_route_or_method()
                else:
                    method()
                self.wfile.flush()
            except (TimeoutError, OSError):
                self.close_connection = True
                if getattr(self, "request_version", "") and not self._admission_expired:
                    self._error(408, "request timed out")
            finally:
                self._finish_admission()

        def _expire_admission(self):
            self._admission_expired = True
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def _finish_admission(self):
            timer = self._admission_timer
            if timer is not None:
                timer.cancel()
                self._admission_timer = None
            if not self._admission_expired:
                self.connection.settimeout(None)

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.close_connection = True
                return self._wrong_route_or_method()
            if not self._authorized():
                self.close_connection = True
                return self._error(401, "missing or invalid bearer token")
            length = self._content_length()
            if length is None:
                return
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                return self._error(408, "request body timed out")
            if len(body) != length:
                return self._error(400, "request body ended early")
            self._finish_admission()
            try:
                request = json.loads(body, parse_constant=self._reject_json_constant)
            except ValueError:
                return self._error(400, "request body is not JSON")
            if not isinstance(request, dict):
                return self._error(400, "request must be an object")
            malformed = invalid_request(request)
            if malformed is not None:
                return self._error(400, malformed)
            messages = request["messages"]
            refusal = unsupported_request(request)
            if refusal is not None:
                log(f"!! refused: {refusal}")
                return self._error(400, refusal)
            tools = offered_tools(request)
            allowed = tools_module.declared_names(tools)
            prompt = render_prompt(messages, tools)
            model = str(request.get("model", settings.model))

            started = time.time()
            log(f"-> {len(messages)} messages, {len(tools)} tools, {len(prompt)} chars; "
                "waiting for Sol Pro")

            if request.get("stream"):
                return self._stream(model, prompt, started, allowed)

            # Queueing is not free: the first request holds the browser for
            # minutes, and a client that leaves in the meantime must not have a
            # Pro message spent on its behalf. The stream path has always checked
            # this; this one used to block on the lock and never look.
            if not self._acquire(model, heartbeat=False):
                log("!! client hung up while queued; engine never started")
                return
            try:
                answer = runner(settings, prompt)
            except Exception as error:
                log(f"!! {error}")
                return self._error(502, "engine delivery failed")
            finally:
                guard.release()
            try:
                content, calls = split_reply(answer, allowed)
            except tools_module.ToolBridgeError as error:
                log(f"!! {error}")
                return self._error(502, "engine delivery failed")
            log(f"<- {len(answer)} chars, {len(calls)} tool call(s) in {time.time() - started:.0f}s")
            return self._json(200, completion_payload(model, content, prompt, delta=False,
                                                      calls=tuple(calls)))

        def do_GET(self):
            if self.path != "/v1/models":
                return self._wrong_route_or_method()
            if not self._authorized():
                return self._error(401, "missing or invalid bearer token")
            self._finish_admission()
            self._json(200, {"object": "list", "data": [{"id": settings.model, "object": "model"}]})

        def do_DELETE(self):
            self._wrong_route_or_method()

        def do_HEAD(self):
            self._wrong_route_or_method()

        def do_OPTIONS(self):
            self._wrong_route_or_method()

        def do_PATCH(self):
            self._wrong_route_or_method()

        def do_PUT(self):
            self._wrong_route_or_method()

        def _authorized(self) -> bool:
            if not settings.token:
                return True
            header = self.headers.get("authorization", "")
            presented = header[7:].strip() if header[:7].lower() == "bearer " else ""
            return hmac.compare_digest(presented, settings.token)

        def _content_length(self) -> int | None:
            values = self.headers.get_all("content-length") or []
            if not values:
                self.close_connection = True
                self._error(411, "content-length is required")
                return None
            if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
                self.close_connection = True
                self._error(400, "content-length must be a non-negative decimal integer")
                return None
            length = int(values[0])
            if length > MAX_CONTENT_LENGTH:
                self.close_connection = True
                self._error(413, "request body is too large")
                return None
            return length

        @staticmethod
        def _reject_json_constant(value: str):
            raise ValueError(f"invalid JSON constant {value}")

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _error(self, status: int, message: str):
            self._json(status, {"error": {"message": message, "type": "lane_delivery_error"}})

        def _not_found(self):
            self._error(404, "route not found")

        def _method_not_allowed(self):
            self.send_response(405)
            self.send_header("allow", "GET, POST")
            body = json.dumps({"error": {"message": "method not allowed",
                                         "type": "lane_delivery_error"}}).encode()
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _wrong_route_or_method(self):
            if self.path in {"/v1/chat/completions", "/v1/models"}:
                return self._method_not_allowed()
            return self._not_found()

        def _stream(self, model: str, prompt: str, started: float, allowed: list[str]):
            """Headers first, heartbeats while Pro thinks, then the answer."""
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()

            if not self._write(heartbeat_frame(model)):
                return
            # Take the browser before starting anything: a request that queues
            # behind another one and then loses its client must not wake up
            # later and spend a Pro message for nobody.
            if not self._acquire(model):
                log("!! client hung up while queued; engine never started")
                return

            outcome: dict = {}

            def work():
                try:
                    outcome["answer"] = runner(settings, prompt)
                except Exception as error:
                    outcome["error"] = str(error)

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            try:
                while True:
                    worker.join(timeout=settings.heartbeat_seconds)
                    if not worker.is_alive():
                        break
                    if not self._write(heartbeat_frame(model)):
                        worker.join()  # the message is already in flight; do not abandon the lock
                        return
            finally:
                guard.release()

            if "error" in outcome:
                log(f"!! {outcome['error']}")
                self._write(json_frame({"error": {"message": "engine delivery failed",
                                                  "type": "lane_delivery_error"}}))
                self._write(b"data: [DONE]\n\n")
                return

            answer = outcome["answer"]
            try:
                content, calls = split_reply(answer, allowed)
            except tools_module.ToolBridgeError as error:
                log(f"!! {error}")
                self._write(json_frame({"error": {"message": "engine delivery failed",
                                                  "type": "lane_delivery_error"}}))
                self._write(b"data: [DONE]\n\n")
                return
            log(f"<- {len(answer)} chars, {len(calls)} tool call(s) in {time.time() - started:.0f}s")
            for frame in sse_frames(model, content, prompt, tuple(calls)):
                if not self._write(frame):
                    return

        def _acquire(self, model: str, *, heartbeat: bool = True) -> bool:
            """Wait for the browser, giving up if the client leaves.

            A stream is kept alive with heartbeats while it waits, which is also
            how its hang-up is noticed. A plain request cannot be written to yet,
            so the socket itself is asked instead.
            """
            while not guard.acquire(timeout=settings.heartbeat_seconds):
                if heartbeat:
                    if not self._write(heartbeat_frame(model)):
                        return False
                elif self._client_gone():
                    return False
            if not heartbeat and self._client_gone():
                guard.release()
                return False
            return True

        def _client_gone(self) -> bool:
            """True once the peer has closed: readable, and a peek returns nothing."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except OSError:
                return True

        def _write(self, payload: bytes) -> bool:
            """Write to the client, reporting a hang-up instead of raising."""
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log("!! client hung up mid-stream")
                return False
            return True

        def log_message(self, *args):
            pass

    return Handler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A small handler pool prevents slow clients from creating unbounded threads."""

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, *, max_handlers=MAX_CONCURRENT_HANDLERS):
        super().__init__(server_address, RequestHandlerClass)
        self._handler_slots = threading.BoundedSemaphore(max_handlers)

    def process_request(self, request, client_address):
        if not self._handler_slots.acquire(blocking=False):
            body = b'{"error":{"message":"service unavailable","type":"lane_delivery_error"}}'
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"content-type: application/json\r\nconnection: close\r\n"
                    + f"content-length: {len(body)}\r\n\r\n".encode()
                    + body
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(settings: ServeSettings, *, host: str = "127.0.0.1", port: int = 8799, log=print) -> None:
    if not is_loopback_host(host):
        raise ValueError(
            "refusing plaintext non-loopback serving: put TLS and authentication at a reverse "
            "proxy, then bind lane serve to loopback"
        )
    server = BoundedThreadingHTTPServer((host, port), make_handler(settings, log=log))
    log(f"lane serve  http://{host}:{port}/v1  → {settings.engine.name} "
        f"(model={settings.model}, require={settings.require_model})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
