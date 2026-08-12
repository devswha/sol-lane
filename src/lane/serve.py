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

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .review import browser_env

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
    # Pro reasons for minutes; an HTTP client that sees no bytes gives up and
    # retries, which costs another Pro message. Keep the stream warm.
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


def render_prompt(messages: list[dict]) -> str:
    """Render a chat transcript the engine can send as a single prompt."""
    blocks = [TRANSCRIPT_HEADER, ""]
    for message in messages:
        body = text_of(message.get("content")).strip()
        if not body:
            continue
        label = ROLE_LABELS.get(str(message.get("role")), "USER")
        blocks.append(f"[{label}]\n{body}")
    blocks.append("[ASSISTANT]")
    return "\n\n".join(blocks)


def estimate_tokens(text: str) -> int:
    """Rough token count. gjc rejects completions reporting implausibly few."""
    return max(1, len(text) // 4)


def run_engine(settings: ServeSettings, prompt: str) -> str:
    try:
        result = subprocess.run(
            settings.command(prompt),
            capture_output=True,
            text=True,
            timeout=settings.max_wait + 120,
            env=browser_env(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ServeError(f"engine could not run: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise ServeError(f"engine exited {result.returncode}: {detail[-1] if detail else 'no detail'}")
    answer = result.stdout.strip()
    if not answer:
        raise ServeError("engine returned an empty answer (fail-closed)")
    return answer


def completion_payload(model: str, answer: str, prompt: str, *, delta: bool) -> dict:
    message = {"role": "assistant", "content": answer}
    choice: dict = {"index": 0, "finish_reason": None if delta else "stop"}
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


def json_frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def sse_frames(model: str, answer: str, prompt: str) -> list[bytes]:
    opening = completion_payload(model, answer, prompt, delta=True)
    closing = completion_payload(model, answer, prompt, delta=True)
    closing["choices"][0]["delta"] = {}
    closing["choices"][0]["finish_reason"] = "stop"
    frames = [f"data: {json.dumps(payload)}\n\n".encode() for payload in (opening, closing)]
    frames.append(b"data: [DONE]\n\n")
    return frames


def make_handler(settings: ServeSettings, *, runner=run_engine, lock=None, log=print):
    guard = lock or threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._error(400, "request body is not JSON")

            messages = request.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return self._error(400, "messages must be a non-empty array")
            prompt = render_prompt(messages)
            model = str(request.get("model", settings.model))

            started = time.time()
            log(f"-> {len(messages)} messages, {len(prompt)} chars; waiting for Sol Pro")

            if request.get("stream"):
                return self._stream(model, prompt, started)

            with guard:  # one browser, one conversation
                try:
                    answer = runner(settings, prompt)
                except ServeError as error:
                    log(f"!! {error}")
                    return self._error(502, str(error))
            log(f"<- {len(answer)} chars in {time.time() - started:.0f}s")
            return self._json(200, completion_payload(model, answer, prompt, delta=False))

        def do_GET(self):
            self._json(200, {"object": "list", "data": [{"id": settings.model, "object": "model"}]})

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _error(self, status: int, message: str):
            self._json(status, {"error": {"message": message, "type": "lane_delivery_error"}})

        def _stream(self, model: str, prompt: str, started: float):
            """Headers first, heartbeats while Pro thinks, then the answer."""
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()

            outcome: dict = {}

            def work():
                with guard:  # one browser, one conversation
                    try:
                        outcome["answer"] = runner(settings, prompt)
                    except ServeError as error:
                        outcome["error"] = str(error)

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            while True:
                worker.join(timeout=settings.heartbeat_seconds)
                if not worker.is_alive():
                    break
                if not self._write(b": lane waiting for Sol Pro\n\n"):
                    return  # client hung up; the worker finishes and is discarded

            if "error" in outcome:
                log(f"!! {outcome['error']}")
                self._write(json_frame({"error": {"message": outcome["error"],
                                                  "type": "lane_delivery_error"}}))
                self._write(b"data: [DONE]\n\n")
                return

            answer = outcome["answer"]
            log(f"<- {len(answer)} chars in {time.time() - started:.0f}s")
            for frame in sse_frames(model, answer, prompt):
                if not self._write(frame):
                    return

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
