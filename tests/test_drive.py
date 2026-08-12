from __future__ import annotations

from pathlib import Path

import pytest

from lane import drive as drive_module


def test_first_plan_request_carries_the_intent_and_demands_citations():
    request = drive_module.plan_request("fix the egress guard")

    assert "fix the egress guard" in request
    assert "file:line" in request
    assert "Gate output" not in request


def test_retry_request_carries_the_failure_and_forbids_weakening_the_gate():
    request = drive_module.plan_request("fix it", gate="uv run pytest -q", failure="E   assert 1 == 2")

    assert "E   assert 1 == 2" in request
    assert "uv run pytest -q" in request
    assert "do not weaken or skip the gate" in request


def test_write_plan_creates_the_handoff_file(tmp_path: Path):
    path = drive_module.write_plan(tmp_path, "step one")

    assert path == tmp_path / drive_module.PLAN_RELPATH
    assert path.read_text(encoding="utf-8") == "step one\n"


def test_implement_uses_a_lane_owned_session_directory(tmp_path: Path):
    command = drive_module.implement_command(tmp_path, tmp_path / "plan.md", first=True)

    assert command[:2] == ["gjc", "-p"]
    assert command[command.index("--session-dir") + 1] == str(tmp_path / drive_module.SESSION_RELPATH)
    assert "--continue" not in command, "the first attempt starts a fresh session"


def test_later_attempts_continue_the_same_session(tmp_path: Path):
    command = drive_module.implement_command(tmp_path, tmp_path / "plan.md", first=False)

    assert "--continue" in command


def test_an_explicit_session_id_switches_to_the_sdk_path(tmp_path: Path):
    plan = tmp_path / "plan.md"
    plan.write_text("do the thing", encoding="utf-8")

    command = drive_module.implement_command(tmp_path, plan, first=True, session="019f-abc")

    assert command[:4] == ["gjc", "sdk", "session", "send"]
    assert command[command.index("--session") + 1] == "019f-abc"
    assert "do the thing" in command[command.index("--text") + 1]
    assert "--wait" in command


def test_gate_exit_code_is_the_verdict(tmp_path: Path):
    passed, log = drive_module.run_gate(tmp_path, "echo ok")
    failed, failure_log = drive_module.run_gate(tmp_path, "echo boom >&2; exit 1")

    assert (passed, log) == (True, "ok")
    assert failed is False
    assert "boom" in failure_log


def test_gate_log_is_bounded(tmp_path: Path):
    _, log = drive_module.run_gate(tmp_path, "python3 -c \"print('x' * 20000)\"; exit 1")

    assert len(log) == drive_module.GATE_LOG_LIMIT


def make_loop(gate_results, tmp_path: Path):
    prompts: list[str] = []
    firsts: list[bool] = []

    def planner(prompt):
        prompts.append(prompt)
        return f"plan {len(prompts)}"

    def implementer(plan, first):
        firsts.append(first)
        return "done"

    results = iter(gate_results)

    def gate_runner():
        return next(results)

    return prompts, firsts, planner, implementer, gate_runner


def test_a_passing_gate_ends_the_loop_after_one_consultation(tmp_path: Path):
    prompts, firsts, planner, implementer, gate_runner = make_loop([(True, "ok")], tmp_path)

    outcome = drive_module.drive(tmp_path, "task", "gate", max_iters=3, planner=planner,
                                 implementer=implementer, gate_runner=gate_runner, log=lambda *_: None)

    assert outcome.passed is True
    assert outcome.iterations == 1
    assert len(prompts) == 1, "Pro is consulted once per attempt, not once per step"
    assert firsts == [True]


def test_a_failing_gate_feeds_its_output_into_the_next_plan(tmp_path: Path):
    prompts, firsts, planner, implementer, gate_runner = make_loop(
        [(False, "E   assert 1 == 2"), (True, "ok")], tmp_path)

    outcome = drive_module.drive(tmp_path, "task", "uv run pytest -q", max_iters=3, planner=planner,
                                 implementer=implementer, gate_runner=gate_runner, log=lambda *_: None)

    assert outcome.passed is True
    assert outcome.iterations == 2
    assert "E   assert 1 == 2" in prompts[1]
    assert firsts == [True, False], "the second attempt continues the same gjc session"


def test_the_loop_stops_at_max_iters_and_reports_failure(tmp_path: Path):
    prompts, _, planner, implementer, gate_runner = make_loop(
        [(False, "fail 1"), (False, "fail 2")], tmp_path)

    outcome = drive_module.drive(tmp_path, "task", "gate", max_iters=2, planner=planner,
                                 implementer=implementer, gate_runner=gate_runner, log=lambda *_: None)

    assert outcome.passed is False
    assert outcome.iterations == 2
    assert len(prompts) == 2, "a bounded loop cannot spend more Pro messages than max_iters"
    assert outcome.attempts[-1].gate_log == "fail 2"


def test_max_iters_below_one_is_rejected(tmp_path: Path):
    with pytest.raises(drive_module.DriveError, match="at least 1"):
        drive_module.drive(tmp_path, "task", "gate", max_iters=0, planner=lambda p: "",
                           implementer=lambda *a: None, gate_runner=lambda: (True, ""))


def test_the_plan_file_holds_the_latest_plan(tmp_path: Path):
    _, _, planner, implementer, gate_runner = make_loop([(False, "f"), (True, "ok")], tmp_path)

    drive_module.drive(tmp_path, "task", "gate", max_iters=2, planner=planner,
                       implementer=implementer, gate_runner=gate_runner, log=lambda *_: None)

    assert (tmp_path / drive_module.PLAN_RELPATH).read_text(encoding="utf-8") == "plan 2\n"
