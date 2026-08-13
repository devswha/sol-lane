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
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import proc

# Lane-owned and machine-generated paths change on every run; they must not
# count as "the implementation did something", nor as verification tampering.
GENERATED_PATHS = (".git", ".ai-bridge", ".insane-review", ".venv", "__pycache__", ".pytest_cache")
GENERATED_SUFFIXES = (".pyc", ".pyo")
HASH_CHUNK_BYTES = 131072

# Frozen paths may not change. These may not *appear* either: a new conftest.py,
# runner config, or lockfile rewires how the tests that already exist are
# collected and run, so "it only added files" is not harmless for them. A new
# test module still is — a red gate does not turn green because a test was added.
GATE_SEALED_NAMES = (
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "noxfile.py",
    "setup.cfg", "setup.py", "pyproject.toml", "uv.toml", "uv.lock",
    ".python-version", "Makefile",
)
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


def run_gate(root: Path, gate: str, *, env: Mapping[str, str] | None = None,
             timeout: float | None = None) -> tuple[bool, str]:
    """Run the repository's own gate. Its exit code is the verdict.

    Only the tail is kept, and it is kept as it arrives: a test suite can print
    more log than this machine has memory. The environment is an allowlist
    because this output is forwarded to Sol Pro in the retry prompt, and anything
    still recognisable as a live secret is redacted before it can travel.
    """
    environment = dict(env) if env is not None else proc.sanitized_env()
    try:
        result = proc.run_tail(gate, cwd=root, shell=True, env=environment,
                              limit=GATE_LOG_LIMIT, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        # A gate that hangs holds the drive lock, and that lock is what stops two
        # drives from executing each other's plans in one worktree.
        raise DriveError(
            f"gate did not finish within {timeout:.0f}s and was killed; "
            "no verdict was produced"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise DriveError(f"gate could not run: {error}") from error
    redacted, _ = redact_secrets(result.output)
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


def gate_digests(root: Path, gate: str, globs: Sequence[str]) -> dict[str, str]:
    """sha256 of every existing file the gate's verdict depends on.

    The implementer and the gate share one mutable worktree, so "make the gate
    pass" can be satisfied by deleting the failing test, excluding it in
    pyproject.toml, or rewriting the gate script. Nothing in the prompt can stop
    that; a hash taken before the implementation runs can.
    """
    digests: dict[str, str] = {}
    for pattern in (*globs, *_gate_command_files(root, gate)):
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if any(part in GENERATED_PATHS for part in relative.parts):
                continue
            if path.suffix in GENERATED_SUFFIXES or not path.is_file():
                continue
            digests[relative.as_posix()] = _file_digest(path)
    return digests


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _gate_command_files(root: Path, gate: str) -> list[str]:
    """Tokens of the gate command that name a file inside the repository.

    `./scripts/gate.sh` is as much the verification as the tests it runs, and no
    glob list will know its name.
    """
    try:
        tokens = shlex.split(gate)
    except ValueError:
        return []
    files = []
    for token in tokens:
        if not token or token.startswith(("-", "/")):
            continue
        relative = Path(token)
        if ".." in relative.parts or not (root / relative).is_file():
            continue
        files.append(relative.as_posix())
    return files


def gate_tampering(before: Mapping[str, str], after: Mapping[str, str], *,
                   sealed: tuple[str, ...] = GATE_SEALED_NAMES) -> list[str]:
    """Protected paths the implementation rewrote, removed, or newly planted.

    Adding a test module is legitimate. Adding a conftest.py is not: a fresh
    collection hook can skip or pass every existing test without touching a
    single frozen file, which is the same false green by another route.
    """
    problems = []
    for path, digest in sorted(before.items()):
        if path not in after:
            problems.append(f"{path} (deleted)")
        elif after[path] != digest:
            problems.append(f"{path} (rewritten)")
    for path in sorted(set(after) - set(before)):
        if Path(path).name in sealed:
            problems.append(f"{path} (added; rewires the tests that already exist)")
    return problems


def assert_verification_intact(baseline: Mapping[str, str], root: Path, gate: str,
                               protected: Sequence[str], *, when: str) -> None:
    problems = gate_tampering(baseline, gate_digests(root, gate, protected))
    if not problems:
        return
    raise DriveError(
        f"the verification changed {when}: "
        + ", ".join(problems[:5])
        + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
    )


def worktree_fingerprint(root: Path, *, ignored: tuple[str, ...] = GENERATED_PATHS) -> str:
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
          protected: Sequence[str], log=print, fingerprint=worktree_fingerprint) -> DriveOutcome:
    """One planning consultation per attempt; the gate ends the loop."""
    if max_iters < 1:
        raise DriveError("max_iters must be at least 1")

    # A gate that is already green cannot distinguish a working implementation
    # from one that changed nothing, so establish that it is red first.
    log(f"[0/{max_iters}] gate before any work: {gate}")
    if gate_runner()[0]:
        log(f"[0/{max_iters}] gate already passes — nothing to drive")
        return DriveOutcome(passed=True, attempts=(), already_satisfied=True)

    baseline = gate_digests(root, gate, protected)
    log(f"[0/{max_iters}] verification frozen: {len(baseline)} file(s) the implementation may not rewrite")

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
        assert_verification_intact(baseline, root, gate, protected,
                                   when="before the gate could run, so the gate was not run")

        log(f"[{iteration}/{max_iters}] gate: {gate}")
        passed, output = gate_runner()
        # The gate and whatever the implementation left running share the tree:
        # a check that only happens before the run cannot see a file swapped
        # underneath it while the tests were collecting.
        assert_verification_intact(baseline, root, gate, protected,
                                   when="while the gate was running, so its verdict is void")
        attempts.append(Attempt(iteration=iteration, plan_chars=len(plan_text),
                                gate_passed=passed, gate_log=output))
        log(f"[{iteration}/{max_iters}] gate {'PASS' if passed else 'FAIL'}")
        if passed:
            return DriveOutcome(passed=True, attempts=tuple(attempts))
        failure = output

    return DriveOutcome(passed=False, attempts=tuple(attempts))
