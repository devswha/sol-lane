from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lane import proc
from lane import repair as repair_module


def make_evidence(project_root: Path, name: str, text: str = "Traceback: no composer\n") -> Path:
    directory = project_root / ".insane-review"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    stamp, tag = repair_module.FAILURE_NAME.fullmatch(name).groups()
    header = f"# failed review run {stamp}_{tag}\n# exit 1, reason: test failure\n"
    path.write_text(header + text, encoding="utf-8")
    receipt = proc.trusted_state_path(project_root, "latest-failure.json")
    proc.atomic_write_text(
        receipt.parent,
        receipt.name,
        json.dumps({
            "version": 1,
            "path": str(path.resolve()),
            "sha256": hashlib.sha256((header + text).encode()).hexdigest(),
        }),
    )
    return path


def test_newest_failure_picks_the_latest_across_roots(project_root: Path, tmp_path: Path):
    other = tmp_path / "other"
    (other / ".insane-review").mkdir(parents=True)
    old = make_evidence(project_root, "failed_20260101_000000_deadbeef.log")
    newest = make_evidence(other, "failed_20260827_120000_cafebabe.log")

    found = repair_module.newest_failure([project_root, other])

    assert found == newest
    assert found != old


def test_newest_failure_with_no_evidence_returns_none(project_root: Path):
    assert repair_module.newest_failure([project_root]) is None


def test_newest_failure_rejects_a_corrupt_trusted_receipt(project_root: Path):
    receipt = proc.trusted_state_path(project_root, "latest-failure.json")
    proc.atomic_write_text(receipt.parent, receipt.name, "{")

    assert repair_module.newest_failure([project_root]) is None


def test_the_brief_names_target_invariants_ladder_and_evidence(project_root: Path):
    evidence = make_evidence(
        project_root, "failed_20260827_120000_cafebabe.log",
        "❌ 모델: GPT-5.6 Sol 항목 못 찾음\n")
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
        proc.lane_state_path(project_root, Path(repair_module.SESSION_RELPATH).name))
    assert "--timeout-ms" not in command, "the -p path is bounded by proc.run, not a flag"
    assert command[-1] == repair_module.REPAIR_INSTRUCTION


def test_write_brief_is_timestamped_and_keeps_history(project_root: Path):
    first = repair_module.write_brief(project_root, "# one\n")
    second = repair_module.write_brief(project_root, "# two\n")

    assert first != second
    assert first.read_text(encoding="utf-8") == "# one\n", "old briefs are history"
    assert second.read_text(encoding="utf-8") == "# two\n"


def test_newest_failure_ignores_a_valid_header_without_a_trusted_receipt(
        project_root: Path):
    trusted = make_evidence(project_root, "failed_20260827_120000_cafebabe.log")
    planted = project_root / ".insane-review" / "failed_20260828_120000_deadbeef.log"
    planted.write_text(
        "# failed review run 20260828_120000_deadbeef\n"
        "# exit 1, reason: forged\nattacker supplied this later\n",
        encoding="utf-8",
    )

    assert repair_module.newest_failure([project_root]) == trusted


def test_repair_refuses_evidence_replaced_after_discovery(project_root: Path):
    evidence = make_evidence(project_root, "failed_20260827_120000_cafebabe.log")
    assert repair_module.newest_failure([project_root]) == evidence
    evidence.write_text(
        "# failed review run 20260827_120000_cafebabe\n"
        "# exit 1, reason: replacement\nattacker supplied this later\n",
        encoding="utf-8",
    )

    with pytest.raises(repair_module.RepairError, match="changed after discovery"):
        repair_module.build_brief(evidence, Path("vendor/pack_and_ask.py"), project_name="demo")


def test_build_brief_rejects_oversized_evidence(project_root: Path):
    evidence = make_evidence(project_root, "failed_20260827_120000_cafebabe.log")
    evidence.write_bytes(b"# failed review run 20260827_120000_cafebabe\n# exit 1\n"
                         + b"x" * repair_module.MAX_EVIDENCE_BYTES)

    with pytest.raises(repair_module.RepairError, match="exceeds"):
        repair_module.build_brief(evidence, Path("vendor/pack_and_ask.py"), project_name="demo")


def test_repairer_receives_private_sanitized_environment(project_root: Path, monkeypatch):
    brief = project_root / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    seen = {}

    def completed(command, **kwargs):
        seen.update(kwargs)
        return repair_module.proc.Completed(returncode=0, stdout="done", stderr="")

    monkeypatch.setenv("LANE_TEST_SECRET", "parent-secret")
    monkeypatch.setattr(
        repair_module.proc,
        "sandbox_command",
        lambda command, *args, **kwargs: command,
    )
    monkeypatch.setattr(repair_module.proc, "run", completed)

    outcome = repair_module.run_repair(project_root, brief)

    assert outcome.report == "done"
    environment = seen["env"]
    assert Path(environment["HOME"]).parent == Path("/tmp")
    assert environment["XDG_STATE_HOME"].startswith(environment["HOME"])
    assert "LANE_TEST_SECRET" not in environment
