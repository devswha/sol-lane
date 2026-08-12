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
