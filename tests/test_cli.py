from __future__ import annotations

import builtins
from pathlib import Path

from lane import cli
from lane import engine as engine_module


def vendored_engine(lane_repo: Path) -> Path:
    path = engine_module.engine_path(lane_repo)
    path.write_text("x = 1\n", encoding="utf-8")
    return path


def test_projects_lists_configured_roots(write_config, capsys, project_root: Path):
    config = write_config()

    assert cli.main(["--config", str(config), "projects"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "demo" in out and str(project_root) in out and "ok" in out


def test_review_dry_run_prints_the_verified_engine_command(write_config, lane_repo: Path, capsys):
    config = write_config()
    engine = vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "왜 느린가", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert str(engine) in out
    assert "--require-model GPT-5.6" in out
    assert "왜 느린가" in out


def test_review_without_a_vendored_engine_is_a_config_error(write_config, capsys):
    config = write_config()

    assert cli.main(["--config", str(config), "review", "demo", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "git checkout" in capsys.readouterr().err


def test_review_refuses_an_unknown_project(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "ghost", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "unknown project" in capsys.readouterr().err


def test_review_refuses_a_root_that_holds_secrets(write_config, lane_repo: Path, project_root: Path, capsys):
    (project_root / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "holds secrets" in capsys.readouterr().err


def test_include_override_reaches_the_command(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    cli.main(["--config", str(config), "review", "demo", "p", "--include", "docs/**.md, x.py", "--dry-run"])

    assert "--include docs/**.md,x.py" in capsys.readouterr().out


def test_paste_dry_run_prints_the_codexpro_bundle_command(write_config, lane_repo: Path, capsys, project_root: Path):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "p", "--paste", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("codexpro pro-bundle --root ")
    assert str(project_root) in out
    assert "--glob src/**/*.py" in out


def test_doctor_reports_config_problems_with_exit_two(write_config, capsys, tmp_path: Path):
    config = write_config(root=tmp_path / "gone")

    assert cli.main(["--config", str(config), "doctor"]) == cli.EXIT_CONFIG
    out = capsys.readouterr().out
    assert "engine     missing" in out
    assert "root:demo   missing" in out


def test_doctor_is_clean_when_engine_and_root_are_fine(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "doctor"]) == cli.EXIT_OK
    assert "root:demo   ok" in capsys.readouterr().out


def test_drive_dry_run_prints_plan_implement_and_gate(write_config, lane_repo: Path, capsys, project_root: Path):
    config = write_config(extra='gate = "pytest -q"\n')
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "drive", "demo", "do the thing", "--dry-run"]) == cli.EXIT_OK
    lines = capsys.readouterr().out.strip().splitlines()
    assert "--require-model GPT-5.6" in lines[0] and "--council" in lines[0]
    assert lines[1].startswith("gjc -p --no-title --session-dir ")
    assert str(project_root) in lines[1]
    assert lines[2] == "pytest -q"


def test_drive_without_a_gate_is_refused(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "drive", "demo", "x", "--dry-run"]) == cli.EXIT_CONFIG
    assert "needs a gate command" in capsys.readouterr().err


def test_serve_refuses_a_public_bind_without_a_token(write_config, lane_repo: Path, capsys, monkeypatch):
    config = write_config()
    vendored_engine(lane_repo)
    monkeypatch.delenv("SOL_PRO_LOCAL_KEY", raising=False)

    assert cli.main(["--config", str(config), "serve", "--host", "0.0.0.0"]) == cli.EXIT_CONFIG
    assert "spends a subscription message" in capsys.readouterr().err


def test_missing_config_is_a_config_error(tmp_path: Path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "projects"]) == cli.EXIT_CONFIG
    assert "cannot read" in capsys.readouterr().err


def test_a_failed_review_names_the_conversation_and_the_free_retry(
        write_config, lane_repo: Path, project_root: Path, capsys, monkeypatch):
    """A spent message must not become a lost one."""
    config = write_config()
    vendored_engine(lane_repo)
    manifests = project_root / ".insane-review"
    manifests.mkdir(exist_ok=True)
    (manifests / "manifest_review_1.json").write_text(
        '{"chat_url": "https://chatgpt.com/c/6a7d67cb-cfb4-83ee-b43f-b2b3d842bb47"}', encoding="utf-8")

    from lane import review as review_module
    monkeypatch.setattr(review_module, "cdp_up", lambda *a, **k: True)
    monkeypatch.setattr(review_module, "run",
                        lambda *a, **k: review_module.ReviewOutcome(returncode=1, response=None))

    assert cli.main(["--config", str(config), "review", "demo", "audit"]) == cli.EXIT_DELIVERY
    err = capsys.readouterr().err
    assert "6a7d67cb-cfb4-83ee-b43f-b2b3d842bb47" in err
    assert "lane harvest demo" in err


def test_harvest_dry_run_sends_nothing(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "harvest", "demo",
                     "https://chatgpt.com/c/abcd1234-1111-2222-3333-444444444444",
                     "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "--harvest https://chatgpt.com/c/abcd1234-1111-2222-3333-444444444444" in out
    assert "--prompt" not in out and "--include" not in out


def test_harvest_without_a_source_or_a_manifest_is_a_delivery_error(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "harvest", "demo"]) == cli.EXIT_DELIVERY
    assert "no run manifest" in capsys.readouterr().err


def test_a_closed_pipe_is_not_a_traceback(write_config, project_root: Path, monkeypatch, capsys):
    """`lane projects | head -1` closes the pipe; the caller asked for that."""
    config = write_config()
    real_print = builtins.print
    calls = {"n": 0}

    def print_then_break(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise BrokenPipeError(32, "Broken pipe")
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", print_then_break)

    assert cli.main(["--config", str(config), "projects"]) == cli.EXIT_OK


def test_doctor_flags_an_engine_override_as_a_problem(write_config, lane_repo: Path,
                                                      tmp_path: Path, monkeypatch, capsys):
    """Sol Pro, reviewing engine.py 2026-08-13: LANE_ENGINE skipped the pin, the
    manifest and both hash checks, and said nothing about it."""
    config = write_config()
    override = tmp_path / "wip.py"
    override.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("LANE_ENGINE", str(override))

    assert cli.main(["--config", str(config), "doctor"]) == cli.EXIT_CONFIG
    out = capsys.readouterr().out
    assert "override" in out
    assert "unverified" in out


def test_review_warns_before_running_an_overridden_engine(write_config, lane_repo: Path,
                                                          tmp_path: Path, monkeypatch, capsys):
    config = write_config()
    override = tmp_path / "wip.py"
    override.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("LANE_ENGINE", str(override))

    assert cli.main(["--config", str(config), "review", "demo", "질문", "--dry-run"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "unverified" in captured.err
    assert str(override) in captured.out, "and it really did use the override"


def test_followup_dry_run_targets_the_newest_conversation(write_config, lane_repo: Path,
                                                          project_root: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)
    manifests = project_root / ".insane-review"
    manifests.mkdir(exist_ok=True)
    (manifests / "manifest_review_9.json").write_text(
        '{"chat_url": "https://chatgpt.com/c/6a7dac02-c75c-83ea-b372-5fe8b837addf"}', encoding="utf-8")

    assert cli.main(["--config", str(config), "followup", "demo", "한 줄만", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "--continue-chat" in out
    assert "manifest_review_9.json" in out
    assert "--include" not in out, "a follow-up must not repack the project"


def test_repair_dry_run_prints_brief_and_command_without_writing(write_config, lane_repo: Path,
                                                                  project_root: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)
    evidence = project_root / ".insane-review"
    evidence.mkdir(exist_ok=True)
    (evidence / "failed_20260827_150000_abcd.log").write_text(
        "# failed review run\n# exit 1\n❌ 컴포저를 찾을 수 없음\n", encoding="utf-8")

    assert cli.main(["--config", str(config), "repair", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "컴포저를 찾을 수 없음" in out
    assert "vendor/pack_and_ask.py" in out
    assert out.count("gjc -p") == 1
    assert not list((lane_repo / ".ai-bridge").glob("repair-brief*")), \
        "dry-run must not write anything"


def test_repair_without_evidence_teaches_how_to_make_some(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "repair"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "failed_*.log" in err
    assert "--evidence" in err


def test_repair_refuses_a_missing_evidence_path(write_config, lane_repo: Path, tmp_path: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "repair",
                     "--evidence", str(tmp_path / "gone.log")]) == cli.EXIT_CONFIG
    assert "not found" in capsys.readouterr().err
