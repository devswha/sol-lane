from __future__ import annotations

import time
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


def test_harvest_command_sends_nothing(write_config, tmp_path: Path):
    """A harvest recovers an answer already paid for: no pack, no prompt, no send."""
    command = review_module.harvest_command(tmp_path / "engine.py", demo(write_config),
                                           "https://chatgpt.com/c/6a7d67cb-cfb4-83ee-b43f-b2b3d842bb47")

    assert command[command.index("--harvest") + 1].endswith("6a7d67cb-cfb4-83ee-b43f-b2b3d842bb47")
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
