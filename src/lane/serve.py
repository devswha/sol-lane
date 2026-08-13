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
    # Without a token the endpoint is only safe on loopback: every request spends
    # a subscription message, so anyone who can reach the port can spend them.
    token: str | None = None
    # Pro reasons for minutes, and gjc aborts a stream whose first *event* has
    # not arrived within retry.streamFirstEventTimeoutMs (100s by default),
    # then retries — spending another Pro message on the same question. SSE
    # comments do not count as events, so the heartbeat has to be a real chunk.
    heartbeat_seconds: float = 10.0

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

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length) or b"{}"
            if not self._authorized():
                return self._error(401, "missing or invalid bearer token")
            try:
                request = json.loads(body)
            except ValueError:
                return self._error(400, "request body is not JSON")

            messages = request.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return self._error(400, "messages must be a non-empty array")
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
            except ServeError as error:
                log(f"!! {error}")
                return self._error(502, str(error))
            finally:
                guard.release()
            try:
                content, calls = split_reply(answer, allowed)
            except tools_module.ToolBridgeError as error:
                log(f"!! {error}")
                return self._error(502, str(error))
            log(f"<- {len(answer)} chars, {len(calls)} tool call(s) in {time.time() - started:.0f}s")
            return self._json(200, completion_payload(model, content, prompt, delta=False,
                                                      calls=tuple(calls)))

        def do_GET(self):
            if not self._authorized():
                return self._error(401, "missing or invalid bearer token")
            self._json(200, {"object": "list", "data": [{"id": settings.model, "object": "model"}]})

        def _authorized(self) -> bool:
            if settings.token is None:
                return True
            header = self.headers.get("authorization", "")
            presented = header[7:].strip() if header[:7].lower() == "bearer " else ""
            return hmac.compare_digest(presented, settings.token)

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _error(self, status: int, message: str):
            self._json(status, {"error": {"message": message, "type": "lane_delivery_error"}})

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
                except ServeError as error:
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
                self._write(json_frame({"error": {"message": outcome["error"],
                                                  "type": "lane_delivery_error"}}))
                self._write(b"data: [DONE]\n\n")
                return

            answer = outcome["answer"]
            try:
                content, calls = split_reply(answer, allowed)
            except tools_module.ToolBridgeError as error:
                log(f"!! {error}")
                self._write(json_frame({"error": {"message": str(error),
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


def serve(settings: ServeSettings, *, host: str = "127.0.0.1", port: int = 8799, log=print) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(settings, log=log))
    log(f"lane serve  http://{host}:{port}/v1  → {settings.engine.name} "
        f"(model={settings.model}, require={settings.require_model})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
