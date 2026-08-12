"""Pro plans, gjc implements, the local gate decides.

Sol Pro judgment costs a subscription message and roughly two minutes per turn;
the repository's own tests cost seconds. So Pro is consulted once per attempt
and never sits in the implementation loop:

    Pro plans -> gjc implements with its own tools -> local gate -> (fail) Pro replans

The gate is authoritative. Pro proposes; the tests decide.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import proc

# Lane-owned and machine-generated paths change on every run; they must not
# count as "the implementation did something".
FINGERPRINT_IGNORED = (".git", ".ai-bridge", ".insane-review", ".venv", "__pycache__", ".pytest_cache")
# Redacting shorter values would turn ordinary words into noise.
MIN_REDACTED_SECRET = 8

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
    already_satisfied: bool = False

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


def run_gate(root: Path, gate: str, *, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Run the repository's own gate. Its exit code is the verdict.

    The environment is an allowlist because this output is forwarded to Sol Pro
    in the retry prompt, and anything still recognisable as a live secret is
    redacted before it can travel.
    """
    environment = dict(env) if env is not None else proc.sanitized_env()
    try:
        result = proc.run(gate, cwd=root, shell=True, env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        raise DriveError(f"gate could not run: {error}") from error
    redacted, _ = redact_secrets(result.output[-GATE_LOG_LIMIT:])
    return result.returncode == 0, redacted


def redact_secrets(text: str, env: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Replace live environment values with their variable name."""
    source = os.environ if env is None else env
    redacted, hits = text, 0
    for key, value in source.items():
        if not value or len(value) < MIN_REDACTED_SECRET or value not in redacted:
            continue
        redacted = redacted.replace(value, f"<redacted:{key}>")
        hits += 1
    return redacted, hits


def worktree_fingerprint(root: Path, *, ignored: tuple[str, ...] = FINGERPRINT_IGNORED) -> str:
    """Cheap identity of the tree, used to detect an implementation that did nothing."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            digest.update(f"{relative}\0node\0".encode())
            continue
        info = path.stat()
        digest.update(f"{relative}\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def drive(root: Path, intent: str, gate: str, *, max_iters: int, planner, implementer, gate_runner,
          log=print, fingerprint=worktree_fingerprint) -> DriveOutcome:
    """One planning consultation per attempt; the gate ends the loop."""
    if max_iters < 1:
        raise DriveError("max_iters must be at least 1")

    # A gate that is already green cannot distinguish a working implementation
    # from one that changed nothing, so establish that it is red first.
    log(f"[0/{max_iters}] gate before any work: {gate}")
    if gate_runner()[0]:
        log(f"[0/{max_iters}] gate already passes — nothing to drive")
        return DriveOutcome(passed=True, attempts=(), already_satisfied=True)

    attempts: list[Attempt] = []
    failure: str | None = None
    for iteration in range(1, max_iters + 1):
        log(f"[{iteration}/{max_iters}] asking Sol Pro for a plan")
        plan_text = planner(plan_request(intent, gate=gate, failure=failure))
        if not isinstance(plan_text, str) or not plan_text.strip():
            raise DriveError("planner returned an empty plan")
        plan_path = write_plan(root, plan_text)
        log(f"[{iteration}/{max_iters}] plan {len(plan_text)} chars -> {plan_path}")

        log(f"[{iteration}/{max_iters}] implementing with gjc")
        before = fingerprint(root)
        implementer(plan_path, iteration == 1)
        if fingerprint(root) == before:
            raise DriveError(
                "implementation produced no repository change; "
                "a gate verdict now would describe the previous state"
            )

        log(f"[{iteration}/{max_iters}] gate: {gate}")
        passed, output = gate_runner()
        attempts.append(Attempt(iteration=iteration, plan_chars=len(plan_text),
                                gate_passed=passed, gate_log=output))
        log(f"[{iteration}/{max_iters}] gate {'PASS' if passed else 'FAIL'}")
        if passed:
            return DriveOutcome(passed=True, attempts=tuple(attempts))
        failure = output

    return DriveOutcome(passed=False, attempts=tuple(attempts))
