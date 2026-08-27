from __future__ import annotations

from pathlib import Path

from lane import repair as repair_module


def make_evidence(project_root: Path, name: str, text: str = "Traceback: no composer\n") -> Path:
    directory = project_root / ".insane-review"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_newest_failure_picks_the_latest_across_roots(project_root: Path, tmp_path: Path):
    other = tmp_path / "other"
    (other / ".insane-review").mkdir(parents=True)
    old = make_evidence(project_root, "failed_20260101_000000_aaaa.log")
    newest = make_evidence(other, "failed_20260827_120000_bbbb.log")

    found = repair_module.newest_failure([project_root, other])

    assert found == newest
    assert found != old


def test_newest_failure_with_no_evidence_returns_none(project_root: Path):
    assert repair_module.newest_failure([project_root]) is None


def test_the_brief_names_target_invariants_ladder_and_evidence(project_root: Path):
    evidence = make_evidence(project_root, "failed_1.log", "❌ 모델: GPT-5.6 Sol 항목 못 찾음\n")
    engine = Path("/repo/vendor/pack_and_ask.py")

    brief = repair_module.build_brief(evidence, engine, project_name="demo")

    assert str(engine) in brief
    assert "failed_1.log" in brief or "demo" in brief
    assert "GPT-5.6 Sol 항목 못 찾음" in brief, "the engine's own error must be in the brief"
    assert "Fail-closed stays fail-closed" in brief
    assert "one Pro message" in brief
    assert "uv run pytest -q" in brief
    assert "--check-env" in brief
    assert "Do\nnot commit" in brief or "not commit" in brief


def test_repair_command_uses_a_lane_owned_session(project_root: Path):
    command = repair_module.repair_command(project_root, project_root / "brief.md")

    assert command[:2] == ["gjc", "-p"]
    assert command[command.index("--session-dir") + 1] == str(
        project_root / repair_module.SESSION_RELPATH)
    assert "--timeout-ms" not in command, "the -p path is bounded by proc.run, not a flag"
    assert command[-1] == repair_module.REPAIR_INSTRUCTION


def test_write_brief_is_timestamped_and_keeps_history(project_root: Path):
    first = repair_module.write_brief(project_root, "# one\n")
    second = repair_module.write_brief(project_root, "# two\n")

    assert first != second
    assert first.read_text(encoding="utf-8") == "# one\n", "old briefs are history"
    assert second.read_text(encoding="utf-8") == "# two\n"
