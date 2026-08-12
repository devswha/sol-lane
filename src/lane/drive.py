"""Pro plans, gjc implements, the local gate decides.

Sol Pro judgment costs a subscription message and roughly two minutes per turn;
the repository's own tests cost seconds. So Pro is consulted once per attempt
and never sits in the implementation loop:

    Pro plans -> gjc implements with its own tools -> local gate -> (fail) Pro replans

The gate is authoritative. Pro proposes; the tests decide.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import proc

PLAN_RELPATH = ".ai-bridge/current-plan.md"
SESSION_RELPATH = ".ai-bridge/lane-session"
GATE_LOG_LIMIT = 4000

PLAN_REQUEST = """\
Write an implementation plan for the task below.

Rules:
- Cite file:line for every claim about existing behaviour.
- List concrete edits per file, in the order they must be applied.
- State how the change is verified with commands that already exist in this repo.
- No alternatives, no commentary: one plan that can be executed as written.

Task: {intent}
"""

RETRY_REQUEST = """\
The previous plan was executed and the repository's gate rejected the result.

Revise the plan so the gate passes. Address the failure directly; do not
restate the parts that already worked, and do not weaken or skip the gate.

Task: {intent}

Gate command: {gate}
Gate output (tail):
{failure}
"""

IMPLEMENT_INSTRUCTION = (
    "Execute the attached plan in this repository. Make the edits, then run the "
    "verification the plan names. Report changed files and the verification result."
)


class DriveError(Exception):
    """The drive loop could not proceed. Maps to exit code 1."""


@dataclass(frozen=True)
class Attempt:
    iteration: int
    plan_chars: int
    gate_passed: bool
    gate_log: str


@dataclass(frozen=True)
class DriveOutcome:
    passed: bool
    attempts: tuple[Attempt, ...]

    @property
    def iterations(self) -> int:
        return len(self.attempts)


def plan_request(intent: str, *, gate: str | None = None, failure: str | None = None) -> str:
    if failure is None:
        return PLAN_REQUEST.format(intent=intent)
    return RETRY_REQUEST.format(intent=intent, gate=gate or "(unknown)", failure=failure)


def write_plan(root: Path, text: str) -> Path:
    path = root / PLAN_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def implement_command(root: Path, plan: Path, *, first: bool, session: str | None = None,
                      timeout_ms: int = 1_800_000) -> list[str]:
    """Command that makes gjc execute the plan.

    Default path is a lane-owned headless session directory: `--continue` keeps
    context across attempts without ever writing into a session the operator is
    using. An explicit session id is the only way to target a live session.
    """
    if session:
        return ["gjc", "sdk", "session", "send", "--session", session,
                "--text", f"{IMPLEMENT_INSTRUCTION}\n\n{plan.read_text(encoding='utf-8')}",
                "--wait", "--timeout-ms", str(timeout_ms)]
    command = ["gjc", "-p", "--no-title", "--session-dir", str(root / SESSION_RELPATH)]
    if not first:
        command.append("--continue")
    return [*command, f"@{plan}", IMPLEMENT_INSTRUCTION]


def implement(root: Path, plan: Path, *, first: bool, session: str | None = None) -> str:
    command = implement_command(root, plan, first=first, session=session)
    try:
        result = proc.run(command, cwd=root)
    except (OSError, subprocess.SubprocessError) as error:
        raise DriveError(f"gjc could not run: {error}") from error
    if result.returncode != 0:
        raise DriveError(f"gjc exited {result.returncode}: {result.detail()}")
    return result.stdout.strip()


def run_gate(root: Path, gate: str) -> tuple[bool, str]:
    """Run the repository's own gate. Its exit code is the verdict."""
    try:
        result = proc.run(gate, cwd=root, shell=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise DriveError(f"gate could not run: {error}") from error
    return result.returncode == 0, result.output[-GATE_LOG_LIMIT:]


def drive(root: Path, intent: str, gate: str, *, max_iters: int, planner, implementer, gate_runner,
          log=print) -> DriveOutcome:
    """One planning consultation per attempt; the gate ends the loop."""
    if max_iters < 1:
        raise DriveError("max_iters must be at least 1")

    attempts: list[Attempt] = []
    failure: str | None = None
    for iteration in range(1, max_iters + 1):
        log(f"[{iteration}/{max_iters}] asking Sol Pro for a plan")
        plan_text = planner(plan_request(intent, gate=gate, failure=failure))
        plan_path = write_plan(root, plan_text)
        log(f"[{iteration}/{max_iters}] plan {len(plan_text)} chars -> {plan_path}")

        log(f"[{iteration}/{max_iters}] implementing with gjc")
        implementer(plan_path, iteration == 1)

        log(f"[{iteration}/{max_iters}] gate: {gate}")
        passed, output = gate_runner()
        attempts.append(Attempt(iteration=iteration, plan_chars=len(plan_text),
                                gate_passed=passed, gate_log=output))
        log(f"[{iteration}/{max_iters}] gate {'PASS' if passed else 'FAIL'}")
        if passed:
            return DriveOutcome(passed=True, attempts=tuple(attempts))
        failure = output

    return DriveOutcome(passed=False, attempts=tuple(attempts))
