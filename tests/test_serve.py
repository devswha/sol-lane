from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from lane import serve as serve_module

SETTINGS = serve_module.ServeSettings(engine=Path("/tmp/engine.py"), max_wait=30)


def post(url: str, payload: dict, *, timeout: float = 10.0):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    return urllib.request.urlopen(request, timeout=timeout)


@pytest.fixture
def server(request):
    runner = request.param if hasattr(request, "param") else (lambda settings, prompt: "hello")
    handler = serve_module.make_handler(SETTINGS, runner=runner, log=lambda *_: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_render_prompt_keeps_order_and_labels_roles():
    prompt = serve_module.render_prompt([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answered"},
        {"role": "user", "content": "second"},
    ])

    assert prompt.index("[SYSTEM]") < prompt.index("[USER]\nfirst")
    assert prompt.index("[ASSISTANT]\nanswered") < prompt.index("[USER]\nsecond")
    assert prompt.rstrip().endswith("[ASSISTANT]")


def test_render_prompt_flattens_typed_content_parts():
    prompt = serve_module.render_prompt([
        {"role": "user", "content": [{"type": "text", "text": "alpha"}, {"type": "image"}, {"type": "text", "text": "beta"}]}
    ])

    assert "alpha\nbeta" in prompt


def test_render_prompt_drops_empty_messages():
    prompt = serve_module.render_prompt([
        {"role": "user", "content": "   "},
        {"role": "user", "content": "real"},
    ])

    assert prompt.count("[USER]") == 1


def test_command_uses_council_mode_and_verifies_the_model():
    command = SETTINGS.command("PROMPT")

    assert command[-1] == "PROMPT"
    assert "--council" in command
    assert command[command.index("--require-model") + 1] == "GPT-5.6"
    assert "--force-answer-after" not in command


def test_command_passes_force_answer_after_when_configured():
    command = serve_module.ServeSettings(engine=Path("/tmp/e.py"), force_answer_after=90).command("p")

    assert command[command.index("--force-answer-after") + 1] == "90"


def test_sse_frames_open_with_content_and_close_with_stop():
    frames = serve_module.sse_frames("sol-pro", "answer", "prompt")

    first = json.loads(frames[0].decode().removeprefix("data: "))
    last = json.loads(frames[1].decode().removeprefix("data: "))
    assert first["choices"][0]["delta"]["content"] == "answer"
    assert last["choices"][0]["finish_reason"] == "stop"
    assert frames[2] == b"data: [DONE]\n\n"


def test_usage_is_never_reported_as_zero():
    payload = serve_module.completion_payload("sol-pro", "a", "b", delta=False)

    assert payload["usage"]["prompt_tokens"] >= 1
    assert payload["usage"]["completion_tokens"] >= 1


def test_streaming_request_returns_sse(server):
    response = post(f"{server}/v1/chat/completions",
                    {"model": "sol-pro", "stream": True, "messages": [{"role": "user", "content": "hi"}]})

    body = response.read().decode()
    assert response.headers["content-type"] == "text/event-stream"
    assert '"content": "hello"' in body
    assert body.endswith("data: [DONE]\n\n")


def test_non_streaming_request_returns_a_completion(server):
    response = post(f"{server}/v1/chat/completions",
                    {"model": "sol-pro", "messages": [{"role": "user", "content": "hi"}]})

    payload = json.loads(response.read())
    assert payload["choices"][0]["message"]["content"] == "hello"
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_empty_messages_are_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {"model": "sol-pro", "messages": []})

    assert caught.value.code == 400


def _failing(settings, prompt):
    raise serve_module.ServeError("engine returned an empty answer (fail-closed)")


@pytest.mark.parametrize("server", [_failing], indirect=True)
def test_delivery_failure_is_502_not_an_empty_completion(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})

    assert caught.value.code == 502
    assert "fail-closed" in json.loads(caught.value.read())["error"]["message"]


_concurrent: list[str] = []


def _slow(settings, prompt):
    _concurrent.append("enter")
    assert _concurrent.count("enter") - _concurrent.count("exit") == 1, "engine ran concurrently"
    time.sleep(0.2)
    _concurrent.append("exit")
    return "ok"


@pytest.mark.parametrize("server", [_slow], indirect=True)
def test_requests_are_serialized_because_there_is_one_browser(server):
    _concurrent.clear()
    errors: list[BaseException] = []

    def call():
        try:
            post(f"{server}/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}).read()
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _concurrent.count("enter") == 3


def test_run_engine_reports_a_non_zero_exit(monkeypatch):
    monkeypatch.setattr(serve_module.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 3, "stdout": "", "stderr": "boom"})())

    with pytest.raises(serve_module.ServeError, match="engine exited 3: boom"):
        serve_module.run_engine(SETTINGS, "prompt")


def test_run_engine_refuses_an_empty_answer(monkeypatch):
    monkeypatch.setattr(serve_module.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "  \n", "stderr": ""})())

    with pytest.raises(serve_module.ServeError, match="empty answer"):
        serve_module.run_engine(SETTINGS, "prompt")
