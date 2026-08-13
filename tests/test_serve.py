from __future__ import annotations

import dataclasses
import json
import threading
from contextlib import contextmanager
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from lane import proc
from lane import serve as serve_module

SETTINGS = serve_module.ServeSettings(engine=Path("/tmp/engine.py"), max_wait=30)


def post(url: str, payload: dict, *, timeout: float = 10.0):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    return urllib.request.urlopen(request, timeout=timeout)


@contextmanager
def running(runner, settings=SETTINGS):
    handler = serve_module.make_handler(settings, runner=runner, log=lambda *_: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def server(request):
    runner = request.param if hasattr(request, "param") else (lambda settings, prompt: "hello")
    with running(runner) as url:
        yield url


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


def _slow_answer(settings, prompt):
    time.sleep(0.6)
    return "late answer"


def test_stream_sends_chunk_heartbeats_before_the_answer():
    """gjc aborts a stream whose first event is late, and SSE comments are not
    events — so the heartbeat must be a parseable chunk."""
    settings = dataclasses.replace(SETTINGS, heartbeat_seconds=0.1)
    with running(_slow_answer, settings) as url:
        response = post(f"{url}/v1/chat/completions",
                        {"stream": True, "messages": [{"role": "user", "content": "hi"}]})
        body = response.read().decode()

    chunks = [json.loads(line.removeprefix("data: "))
              for line in body.splitlines()
              if line.startswith("data: ") and not line.endswith("[DONE]")]
    contents = [chunk["choices"][0]["delta"].get("content") for chunk in chunks]
    assert contents[0] == "", "the first event must arrive before the engine answers"
    assert contents.count("") >= 2, "heartbeats keep arriving while Pro reasons"
    assert "late answer" in contents
    assert contents.index("late answer") > 0
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.parametrize("server", [_failing], indirect=True)
def test_stream_failure_emits_an_error_frame_not_a_silent_stop(server):
    response = post(f"{server}/v1/chat/completions",
                    {"stream": True, "messages": [{"role": "user", "content": "hi"}]})

    body = response.read().decode()
    contents = [json.loads(line.removeprefix("data: "))["choices"][0]["delta"].get("content")
                for line in body.splitlines()
                if line.startswith("data: ") and "choices" in line]
    assert "lane_delivery_error" in body
    assert "fail-closed" in body
    assert set(contents) <= {""}, "a failed delivery must not emit answer text"


@pytest.mark.parametrize("server", [_slow_answer], indirect=True)
def test_a_client_hang_up_does_not_take_the_server_down(server):
    request = urllib.request.Request(
        f"{server}/v1/chat/completions",
        data=json.dumps({"stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"content-type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).close()

    later = post(f"{server}/v1/chat/completions", {"messages": [{"role": "user", "content": "again"}]})
    assert json.loads(later.read())["choices"][0]["message"]["content"] == "late answer"


def stub_engine(monkeypatch, returncode, stdout="", stderr=""):
    completed = proc.Completed(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(serve_module.proc, "run", lambda *a, **k: completed)


def test_a_token_is_required_when_configured():
    settings = dataclasses.replace(SETTINGS, token="secret-token")
    with running(lambda s, p: "hello", settings) as url:
        with pytest.raises(urllib.error.HTTPError) as caught:
            post(f"{url}/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        assert caught.value.code == 401

        request = urllib.request.Request(
            f"{url}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"content-type": "application/json", "authorization": "Bearer secret-token"},
        )
        payload = json.loads(urllib.request.urlopen(request, timeout=10).read())
    assert payload["choices"][0]["message"]["content"] == "hello"


def test_a_wrong_token_is_refused_on_model_listing():
    settings = dataclasses.replace(SETTINGS, token="secret-token")
    with running(lambda s, p: "hello", settings) as url:
        request = urllib.request.Request(f"{url}/v1/models", headers={"authorization": "Bearer nope"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 401


def test_an_empty_tools_array_still_works(server):
    response = post(f"{server}/v1/chat/completions",
                    {"messages": [{"role": "user", "content": "hi"}], "tools": []})

    assert json.loads(response.read())["choices"][0]["message"]["content"] == "hello"


def test_non_text_content_parts_are_refused(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}],
        })

    assert caught.value.code == 400


def test_a_queued_request_whose_client_left_never_spends_a_message():
    """The expensive part must start only after the browser is actually free."""
    settings = dataclasses.replace(SETTINGS, heartbeat_seconds=0.05)
    started = threading.Semaphore(0)
    release = threading.Event()
    calls = []

    def runner(_settings, prompt):
        calls.append(prompt)
        started.release()
        release.wait(10)
        return "first answer"

    with running(runner, settings) as url:
        holder = threading.Thread(target=lambda: post(
            f"{url}/v1/chat/completions",
            {"stream": True, "messages": [{"role": "user", "content": "first"}]}).read(), daemon=True)
        holder.start()
        assert started.acquire(timeout=10), "the first request never reached the engine"

        queued = post(f"{url}/v1/chat/completions",
                      {"stream": True, "messages": [{"role": "user", "content": "second"}]})
        queued.read(1)          # a heartbeat proves the handler is alive and waiting
        queued.close()          # ...and then the client leaves
        time.sleep(0.4)

        release.set()
        holder.join(10)

    assert calls == [calls[0]], f"the abandoned request still ran the engine: {len(calls)} calls"


def test_run_engine_reports_a_non_zero_exit(monkeypatch):
    stub_engine(monkeypatch, 3, stderr="boom")

    with pytest.raises(serve_module.ServeError, match="engine exited 3: boom"):
        serve_module.run_engine(SETTINGS, "prompt")


def test_run_engine_refuses_an_empty_answer(monkeypatch):
    stub_engine(monkeypatch, 0, stdout="  \n")

    with pytest.raises(serve_module.ServeError, match="empty answer"):
        serve_module.run_engine(SETTINGS, "prompt")


WEATHER_TOOL = {
    "type": "function",
    "function": {"name": "get_weather", "description": "weather for a city",
                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
}


def call_block(payload: str) -> str:
    from lane import tools as tools_module
    return f"```{tools_module.FENCE}\n{payload}\n```"


def test_offered_tools_render_into_the_prompt():
    prompt = serve_module.render_prompt([{"role": "user", "content": "weather?"}], [WEATHER_TOOL])

    assert "get_weather" in prompt
    assert "tool_call" in prompt, "the model needs the reply protocol, not just the names"
    assert '"city"' in prompt


def test_tool_choice_none_keeps_the_turn_in_prose():
    request = {"tools": [WEATHER_TOOL], "tool_choice": "none"}

    assert serve_module.offered_tools(request) == []
    assert "get_weather" not in serve_module.render_prompt(
        [{"role": "user", "content": "hi"}], serve_module.offered_tools(request))


def test_a_forced_tool_choice_is_refused_because_prose_cannot_enforce_it(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        })

    assert caught.value.code == 400
    assert "cannot be enforced" in json.loads(caught.value.read())["error"]["message"]


def test_a_tool_result_is_replayed_into_the_transcript():
    prompt = serve_module.render_prompt([
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}}]},
        {"role": "tool", "name": "get_weather", "tool_call_id": "call_1", "content": "-3C"},
    ])

    assert "called get_weather" in prompt
    assert "Seoul" in prompt
    assert "-3C" in prompt, "without the result the next turn just calls again"


def _calls_a_tool(settings, prompt):
    return call_block('{"name": "get_weather", "arguments": {"city": "Seoul"}}')


@pytest.mark.parametrize("server", [_calls_a_tool], indirect=True)
def test_a_parsed_call_comes_back_as_tool_calls(server):
    response = post(f"{server}/v1/chat/completions", {
        "messages": [{"role": "user", "content": "weather?"}], "tools": [WEATHER_TOOL]})
    payload = json.loads(response.read())

    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None, "a call carries no prose of its own"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Seoul"}


@pytest.mark.parametrize("server", [_calls_a_tool], indirect=True)
def test_a_stream_carries_the_call_and_closes_on_tool_calls(server):
    response = post(f"{server}/v1/chat/completions", {
        "messages": [{"role": "user", "content": "weather?"}], "tools": [WEATHER_TOOL],
        "stream": True})
    frames = [json.loads(line.removeprefix("data: ")) for line in response.read().decode().splitlines()
              if line.startswith("data: ") and "[DONE]" not in line]

    assert frames[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


def _calls_an_unoffered_tool(settings, prompt):
    return call_block('{"name": "rm_rf", "arguments": {}}')


@pytest.mark.parametrize("server", [_calls_an_unoffered_tool], indirect=True)
def test_a_call_to_an_unoffered_tool_is_502_not_an_answer(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {
            "messages": [{"role": "user", "content": "hi"}], "tools": [WEATHER_TOOL]})

    assert caught.value.code == 502
    assert "was not offered" in json.loads(caught.value.read())["error"]["message"]


def _emits_a_truncated_call(settings, prompt):
    from lane import tools as tools_module
    return f'checking\n```{tools_module.FENCE}\n{{"name": "get_weather"'


@pytest.mark.parametrize("server", [_emits_a_truncated_call], indirect=True)
def test_a_truncated_call_is_502_rather_than_executed(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(f"{server}/v1/chat/completions", {
            "messages": [{"role": "user", "content": "hi"}], "tools": [WEATHER_TOOL]})

    assert caught.value.code == 502
    assert "never closes" in json.loads(caught.value.read())["error"]["message"]


@pytest.mark.parametrize("server", [_calls_a_tool], indirect=True)
def test_a_fenced_block_is_plain_text_when_no_tools_were_offered(server):
    """Nothing was on the table, so nothing can be called — and an answer that
    happens to contain the fence must not become a delivery error."""
    response = post(f"{server}/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
    payload = json.loads(response.read())

    assert payload["choices"][0]["finish_reason"] == "stop"
    assert "get_weather" in payload["choices"][0]["message"]["content"]
