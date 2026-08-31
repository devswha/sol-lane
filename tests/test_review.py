from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lane import review as review_module
from lane.config import load


def demo(write_config, **kwargs):
    return load(write_config(**kwargs)).project("demo")


def test_engine_args_never_request_lossy_packing(write_config):
    args = review_module.engine_args(demo(write_config), "why is this slow?")

    assert "--compress" not in args
    assert "--remove-comments" not in args
    assert "--no-line-numbers" not in args


def test_engine_args_pin_the_model_and_verify_it(write_config):
    args = review_module.engine_args(demo(write_config), "prompt")

    assert args[args.index("--model") + 1] == "pro"
    assert args[args.index("--require-model") + 1] == "GPT-5.6"


def test_engine_args_join_globs_for_repomix(write_config):
    args = review_module.engine_args(demo(write_config), "prompt", include=("a/**.py", "b.md"))

    assert args[args.index("--include") + 1] == "a/**.py,b.md"


def test_force_answer_after_is_omitted_when_disabled(write_config):
    assert "--force-answer-after" not in review_module.engine_args(demo(write_config), "prompt")


def test_force_answer_after_is_passed_when_configured(write_config):
    args = review_module.engine_args(demo(write_config, extra="force_answer_after = 600\n"), "prompt")

    assert args[args.index("--force-answer-after") + 1] == "600"


def test_pack_hygiene_toggles_follow_the_config(write_config):
    on = review_module.engine_args(demo(write_config), "prompt")
    off = review_module.engine_args(
        demo(write_config, extra="no_project = false\ndelete_pack = false\n"), "prompt"
    )

    assert {"--no-project", "--delete-pack"} <= set(on)
    assert {"--no-project", "--delete-pack"}.isdisjoint(off)


def test_command_runs_the_engine_with_an_interpreter(write_config, tmp_path: Path):
    engine = tmp_path / "pack_and_ask.py"
    command = review_module.command(engine, demo(write_config), "prompt", python="/usr/bin/python3")

    assert command[:2] == ["/usr/bin/python3", str(engine)]


def test_relative_project_root_is_anchored_to_the_config_directory(lane_repo: Path):
    root = lane_repo / "worktree"
    root.mkdir()
    path = lane_repo / "lane.toml"
    path.write_text(
        '[projects.demo]\nroot = "worktree"\ninclude = ["src/**/*.py"]\n',
        encoding="utf-8",
    )

    assert load(path).project("demo").root == root.resolve()


def test_pack_bytes_counts_matched_files_once(project_root: Path):
    (project_root / "src" / "extra.py").write_text("y = 2\n", encoding="utf-8")

    total, count = review_module.pack_bytes(project_root, ("src/**/*.py", "src/app.py"))

    assert count == 2, "overlapping globs must not double-count a file"
    assert total == sum((project_root / "src" / name).stat().st_size for name in ("app.py", "extra.py"))


def test_pack_bytes_ignores_directories_and_misses(project_root: Path):
    total, count = review_module.pack_bytes(project_root, ("src", "nothing/**/*.py"))

    assert (total, count) == (0, 0)


def test_browser_env_keeps_an_existing_display():
    assert review_module.browser_env({"DISPLAY": ":7"})["DISPLAY"] == ":7"


def test_browser_env_derives_the_display_from_a_running_x_socket(tmp_path: Path):
    (tmp_path / "X0").touch()

    assert review_module.browser_env({}, socket_glob=str(tmp_path / "X*"))["DISPLAY"] == ":0"


def test_browser_env_leaves_display_unset_without_a_socket(tmp_path: Path):
    assert "DISPLAY" not in review_module.browser_env({}, socket_glob=str(tmp_path / "X*"))


def test_cdp_up_is_false_without_a_listener():
    assert review_module.cdp_up("http://127.0.0.1:9/json/version", timeout=0.5) is False


def test_cdp_up_accepts_only_a_successful_loopback_json_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"Browser": "sandbox"}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        host, port = server.server_address
        assert review_module.cdp_up(f"http://{host}:{port}/json/version")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_cdp_up_rejects_non_loopback_and_malformed_urls():
    assert not review_module.cdp_up("file:///etc/passwd")
    assert not review_module.cdp_up("http://example.com/json/version")
    assert not review_module.cdp_up("http://127.0.0.1:not-a-port/json/version")


def test_newest_new_response_ignores_pre_existing_files(project_root: Path):
    directory = project_root / ".insane-review"
    directory.mkdir()
    stale = directory / "response_old.md"
    stale.write_text("old", encoding="utf-8")
    before = review_module.responses(project_root)
    time.sleep(0.01)
    fresh = directory / "response_new.md"
    fresh.write_text("new", encoding="utf-8")

    assert review_module.newest_new_response(project_root, before) == fresh


def test_newest_new_response_is_none_when_nothing_was_written(project_root: Path):
    before = review_module.responses(project_root)

    assert review_module.newest_new_response(project_root, before) is None


def test_response_discovery_ignores_symlinked_forged_artifacts(project_root: Path, tmp_path: Path):
    directory = project_root / ".insane-review"
    directory.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text(saved(ANSWER), encoding="utf-8")
    (directory / "response_forged.md").symlink_to(victim)

    assert review_module.responses(project_root) == set()
    assert review_module.newest_new_response(project_root, set()) is None


def test_successful_run_response_must_be_the_engine_reported_new_artifact(project_root: Path):
    directory = project_root / ".insane-review"
    directory.mkdir()
    forged = directory / "response_forged.md"
    forged.write_text(saved(ANSWER), encoding="utf-8")
    reported = directory / "response_prompt_current.md"
    reported.write_text(saved(ANSWER), encoding="utf-8")
    manifest = directory / "manifest_prompt_current.json"
    manifest.write_text(
        '{"chat_url": "https://chatgpt.com/c/0c3d4e5f-6078-89ab-cdef-234567890abc"}',
        encoding="utf-8",
    )
    result = review_module.proc.Completed(
        returncode=0,
        stdout=f"[완료] 응답 저장: {reported}\n",
        stderr="",
    )

    assert review_module.response_from_successful_run(project_root, set(), result, set()) == reported
    assert review_module.response_from_successful_run(project_root, {reported}, result, set()) is None


def test_followup_accepts_only_the_engine_reported_artifact(
        write_config, project_root: Path, monkeypatch):
    directory = project_root / ".insane-review"
    directory.mkdir()
    response = directory / "response_followup_current.md"
    manifest = directory / "manifest_followup_current.json"

    def run_engine(*args, **kwargs):
        response.write_text(saved(ANSWER), encoding="utf-8")
        manifest.write_text(
            '{"chat_url": "https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444"}',
            encoding="utf-8",
        )
        return review_module.proc.Completed(
            returncode=0,
            stdout=f"[완료] 응답 저장: {response}\n",
            stderr="",
        )

    monkeypatch.setattr(review_module.proc, "run", run_engine)

    outcome = review_module.followup(
        Path("engine.py"),
        demo(write_config),
        project_root,
        "https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444",
        "next question",
    )

    assert outcome.response == response


def test_harvest_command_sends_nothing(write_config, tmp_path: Path):
    """A harvest recovers an answer already paid for: no pack, no prompt, no send."""
    command = review_module.harvest_command(tmp_path / "engine.py", demo(write_config),
                                           "https://chatgpt.com/c/0a1b2c3d-4e5f-6789-abcd-0123456789ab")

    assert command[command.index("--harvest") + 1].endswith("0a1b2c3d-4e5f-6789-abcd-0123456789ab")
    for absent in ("--prompt", "--council", "--include", "--target", "--delete-pack"):
        assert absent not in command, f"{absent} would repack or resend"
    assert command[command.index("--require-model") + 1] == "GPT-5.6", "a harvest still verifies the model"
    # Measured 2026-08-13: the engine exits with "--require-model은 --model과 함께
    # 써야 합니다" when the pair is split, even though harvest selects nothing.
    assert command[command.index("--model") + 1] == "pro"


def test_harvest_does_not_inherit_the_send_budget(write_config, tmp_path: Path):
    """max_wait is for a message in flight. A harvest of an interrupted chat would
    otherwise sit on the browser for the full 70 minutes for nothing."""
    project = demo(write_config, extra="max_wait = 4200\n")

    default = review_module.harvest_command(tmp_path / "engine.py", project, "https://chatgpt.com/c/abc-1234")
    explicit = review_module.harvest_command(tmp_path / "engine.py", project,
                                            "https://chatgpt.com/c/abc-1234", max_wait=1800)

    assert default[default.index("--max-wait") + 1] == str(review_module.HARVEST_WAIT_SECONDS)
    assert review_module.HARVEST_WAIT_SECONDS < 4200
    assert explicit[explicit.index("--max-wait") + 1] == "1800"


def test_newest_manifest_and_its_conversation(tmp_path: Path):
    directory = tmp_path / ".insane-review"
    directory.mkdir()
    old = directory / "manifest_review_1.json"
    old.write_text('{"chat_url": "https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444"}',
                   encoding="utf-8")
    time.sleep(0.01)
    new = directory / "manifest_review_2.json"
    new.write_text('{"chat_url": "https://chatgpt.com/c/bbbbbbbb-1111-2222-3333-444444444444"}',
                   encoding="utf-8")

    found = review_module.newest_manifest(tmp_path)

    assert found == new
    assert review_module.conversation_of(found).endswith("bbbbbbbb-1111-2222-3333-444444444444")


def test_no_manifest_is_not_an_error(tmp_path: Path):
    assert review_module.newest_manifest(tmp_path) is None


def test_a_manifest_without_a_conversation_is_refused(tmp_path: Path):
    path = tmp_path / "manifest_review_1.json"
    path.write_text('{"chat_url": "https://chatgpt.com/"}', encoding="utf-8")
    broken = tmp_path / "manifest_review_2.json"
    broken.write_text("not json", encoding="utf-8")

    assert review_module.conversation_of(path) is None
    assert review_module.conversation_of(broken) is None


def test_conversation_manifest_must_be_regular_utf8(tmp_path: Path):
    invalid = tmp_path / "manifest_invalid.json"
    invalid.write_bytes(b"\xff")
    victim = tmp_path / "manifest_real.json"
    victim.write_text(
        '{"chat_url": "https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444"}',
        encoding="utf-8",
    )
    linked = tmp_path / "manifest_linked.json"
    linked.symlink_to(victim)

    assert review_module.conversation_of(invalid) is None
    assert review_module.conversation_of(linked) is None


HEADER = "# sol-lane — GPT 응답\n\n- 모델: `GPT-5.6 Sol (Pro)`\n- 프롬프트: 게이트 동결에 구멍이 있나...\n"
ANSWER = "(a) src/lane/drive.py:120 (b) baseline은 루프 전에만 계산된다 (c) 재현 ... (d) 수정 ..."


def saved(body: str) -> str:
    return f"{HEADER}\n---\n\n{body}\n"


def test_answer_body_strips_the_engine_header():
    assert review_module.answer_body(saved(ANSWER)) == ANSWER


def test_answer_body_passes_through_a_file_without_a_header():
    assert review_module.answer_body("bare answer\n") == "bare answer"


def test_a_real_answer_is_not_rejected():
    assert review_module.rejection_reason(ANSWER, "게이트 동결에 구멍이 있나") is None


def test_an_empty_answer_is_rejected():
    assert "empty" in review_module.rejection_reason("   \n ", "prompt")


def test_a_refusal_page_is_not_an_answer():
    """Measured 2026-08-13: an audit phrased as 'find the way to break this'
    came back as a refusal page, and the lane filed it with exit 0."""
    body = f"{review_module.REFUSAL_MARKERS[0]}\n어쩌구"

    reason = review_module.rejection_reason(body, "prompt")

    assert reason is not None and "refused" in reason


def test_the_prompt_echoed_back_is_not_an_answer():
    """Same run: the text saved as the answer was the user turn."""
    prompt = ("첨부 파일 두 개만 본다. 짧게 답해라 — 5분 안에 끝낼 분량으로. 한 가지만 묻는다. "
              "보장: 구현자는 게이트 자체를 약화시켜 drive를 통과할 수 없다. "
              "구현: drive()가 루프 시작 전에 검증 파일 해시를 얼리고 대조한다. "
              "형식: (a) 파일:라인 (b) 문제 코드 (c) 재현 (d) 최소 수정안.")

    reason = review_module.rejection_reason(prompt, prompt)

    assert reason is not None and "prompt" in reason


def test_a_short_shared_phrase_does_not_look_like_an_echo():
    prompt = "게이트 동결에 구멍이 있나"

    assert review_module.rejection_reason(f"{prompt} — 있다. drive.py:120 ...", prompt) is None


def test_reject_moves_the_file_out_of_the_response_namespace(tmp_path: Path):
    path = tmp_path / "response_sol-lane_20260813_182740.md"
    path.write_text(saved("nope"), encoding="utf-8")

    moved = review_module.reject(path, "the model refused")

    assert not path.exists(), "a rejected page must not stay where responses live"
    assert moved.name == "rejected_sol-lane_20260813_182740.md"
    text = moved.read_text(encoding="utf-8")
    assert text.startswith("# REJECTED — the model refused")
    assert "nope" in text, "the evidence is kept, only its status changes"


def test_verification_turns_a_refusal_into_a_delivery_failure(tmp_path: Path):
    path = tmp_path / "response_run.md"
    path.write_text(saved(review_module.REFUSAL_MARKERS[0]), encoding="utf-8")

    outcome = review_module._verified(0, path, "prompt")

    assert outcome.response is None, "exit 0 with a saved file is exactly the false success"
    assert outcome.rejected is not None and outcome.rejected.exists()
    assert "refused" in outcome.reason


def test_verification_leaves_a_real_answer_alone(tmp_path: Path):
    path = tmp_path / "response_run.md"
    path.write_text(saved(ANSWER), encoding="utf-8")

    outcome = review_module._verified(0, path, "게이트 동결에 구멍이 있나")

    assert outcome.response == path
    assert (outcome.rejected, outcome.reason) == (None, None)


def test_verification_never_accepts_a_response_after_a_nonzero_engine_exit(tmp_path: Path):
    path = tmp_path / "response_run.md"
    path.write_text(saved(ANSWER), encoding="utf-8")

    outcome = review_module._verified(1, path, "prompt")

    assert outcome.response is None
    assert outcome.reason == "engine exited unsuccessfully"


def test_reject_refuses_to_write_through_a_symlink(tmp_path: Path):
    response = tmp_path / "response_run.md"
    response.write_text(saved("bad"), encoding="utf-8")
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    (tmp_path / "rejected_run.md").symlink_to(victim)

    try:
        review_module.reject(response, "bad response")
    except review_module.ReviewError:
        pass
    else:
        raise AssertionError("a pre-existing rejection symlink must be refused")

    assert victim.read_text(encoding="utf-8") == "keep"


def test_only_a_real_engine_header_is_stripped():
    """Sol Pro, reviewing this file 2026-08-13: splitting on the first rule made a
    refusal disappear *as* the header, leaving a footer to pass verification."""
    disguised = f"{review_module.REFUSAL_MARKERS[0]}\n---\nfooter line"

    body = review_module.answer_body(disguised)

    assert review_module.REFUSAL_MARKERS[0] in body
    assert review_module.rejection_reason(body, "prompt") is not None


def test_a_real_header_is_still_stripped():
    assert review_module.answer_body(saved(ANSWER)) == ANSWER


def test_a_header_shaped_text_without_the_engine_markers_is_content():
    text = "# 제목\n\n본문 첫 줄\n---\n본문 둘째 줄"

    assert review_module.answer_body(text) == text.strip()


def test_a_refusal_hidden_where_a_header_would_be_is_still_caught(tmp_path: Path):
    path = tmp_path / "response_run.md"
    path.write_text(f"{review_module.REFUSAL_MARKERS[0]}\n---\nfooter", encoding="utf-8")

    outcome = review_module._verified(0, path, "prompt")

    assert outcome.response is None
    assert "refused" in outcome.reason


def test_followup_sends_into_the_conversation_without_packing(write_config, tmp_path: Path):
    command = review_module.followup_command(tmp_path / "engine.py", demo(write_config),
                                            "https://chatgpt.com/c/abcd1234-1111-2222-3333-444444444444",
                                            "한 줄만 더")

    assert command[command.index("--continue-chat") + 1].endswith("444444444444")
    assert command[command.index("--prompt") + 1] == "한 줄만 더"
    for absent in ("--include", "--target", "--council", "--harvest", "--delete-pack"):
        assert absent not in command, f"{absent} would repack, resend, or open a new chat"


def test_followup_passes_the_model_pair_the_engine_demands(write_config, tmp_path: Path):
    command = review_module.followup_command(tmp_path / "engine.py", demo(write_config), "u", "p")

    assert command[command.index("--model") + 1] == "pro"
    assert command[command.index("--require-model") + 1] == "GPT-5.6"


def test_followup_can_override_the_wait(write_config, tmp_path: Path):
    command = review_module.followup_command(tmp_path / "engine.py", demo(write_config), "u", "p",
                                            max_wait=90)

    assert command[command.index("--max-wait") + 1] == "90"
