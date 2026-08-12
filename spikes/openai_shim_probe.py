"""Minimal OpenAI-compatible shim: proves a gjc session can run on a local provider."""

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG = Path("/tmp/shim-requests.log")
ANSWER = "SHIM-OK: this text came from the local provider, not from any hosted model."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) or b"{}"
        request = json.loads(raw)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{self.path} stream={request.get('stream')} keys={sorted(request)}\n")

        if request.get("stream"):
            self._stream(request)
        else:
            self._json(request)

    def _completion(self, request, *, delta=False):
        message = {"role": "assistant", "content": ANSWER}
        choice = {"index": 0, "finish_reason": None if delta else "stop"}
        choice["delta" if delta else "message"] = message
        return {
            "id": "chatcmpl-shim",
            "object": "chat.completion.chunk" if delta else "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", "sol-pro"),
            "choices": [choice],
            "usage": {"prompt_tokens": 512, "completion_tokens": 64, "total_tokens": 576},
        }

    def _json(self, request):
        body = json.dumps(self._completion(request)).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, request):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        first = self._completion(request, delta=True)
        final = self._completion(request, delta=True)
        final["choices"][0]["delta"] = {}
        final["choices"][0]["finish_reason"] = "stop"
        for payload in (first, final):
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        body = json.dumps({"data": [{"id": "sol-pro", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", 8799), Handler).serve_forever()
