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
import tempfile
from contextlib import contextmanager
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
# gjc -p can outlive its own answer: a tool-executing run spawns a browser
# warmup whose teardown was measured (2026-08-13) taking minutes past the final
# output, once past three. Unbounded, a gjc that never exits stalls the loop
# while it holds the drive lock. Same budget as the session-send path.
IMPLEMENT_TIMEOUT_SECONDS = 1800

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
    relative = Path(PLAN_RELPATH)
    return proc.atomic_write_text(
        root / relative.parent,
        relative.name,
        text if text.endswith("\n") else text + "\n",
    )


def implement_command(root: Path, plan: Path, *, first: bool,
                      session: str | None = None) -> list[str]:
    """Command that makes gjc execute the plan.

    `--continue` keeps context in a lane-owned session directory. A live SDK
    session is not an isolation boundary and is deliberately unsupported.
    """
    if session:
        raise DriveError("implementer session must be lane-owned")
    command = [
        "gjc", "-p", "--no-title", "--session-dir",
        str(proc.lane_state_path(root, Path(SESSION_RELPATH).name)),
    ]
    if not first:
        command.append("--continue")
    return [*command, f"@{plan}", IMPLEMENT_INSTRUCTION]


def implement(root: Path, plan: Path, *, first: bool, session: str | None = None) -> str:
    command = implement_command(root, plan, first=first, session=session)
    try:
        with _private_child_environment(root) as environment:
            state = proc.lane_state_path(root, Path(SESSION_RELPATH).name).parent
            sandboxed = proc.sandbox_command(
                command,
                root,
                Path(environment["HOME"]),
                state,
            )
            result = proc.run(sandboxed, cwd=root, env=environment,
                              timeout=IMPLEMENT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise DriveError(
            f"gjc did not finish within {IMPLEMENT_TIMEOUT_SECONDS}s and was killed; "
            "the worktree may hold a partial implementation"
        ) from error
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
    command, _ = gate_command(root, gate)
    with tempfile.TemporaryDirectory(prefix="lane-gate-") as private:
        if Path(command[0]).name == "uv" and len(command) > 1 and command[1] == "run":
            command = [command[0], "run", "--isolated", "--locked", *command[2:]]
            environment["UV_CACHE_DIR"] = "/mnt/sol-lane/home/uv-cache"
        sandboxed = proc.sandbox_command(
            command,
            root,
            Path(private) / "home",
            Path(private) / "state",
            writable_paths=(),
        )
        try:
            result = proc.run_tail(sandboxed, cwd=root, env=environment,
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
    redacted, _ = redact_secrets(result.output, environment)
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
    for pattern in globs:
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if any(part in GENERATED_PATHS for part in relative.parts):
                continue
            if path.suffix in GENERATED_SUFFIXES or not path.is_file():
                continue
            digests[relative.as_posix()] = _file_digest(path)
    # The gate's own executable is the verification, wherever it lives. Skipping
    # .venv keeps generated noise out of the protected globs; applied here it
    # would exempt `.venv/bin/pytest` from the freeze, and a gate rewritten to
    # exit 0 passes both integrity checks. (Sol Pro, reviewing this file.)
    _, dependencies = gate_command(root, gate)
    for name in dependencies:
        path = root / name
        if path.is_file():
            digests[name] = _file_digest(path)
    return digests


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def gate_command(root: Path, gate: str) -> tuple[list[str], tuple[str, ...]]:
    """Parse a deterministic gate argv and identify its repository files.

    Shell syntax is never interpreted. A path-like command or argument must
    resolve below *root*, so a gate script has a stable, freezeable identity.
    Nonexistent path-like arguments are retained as dependencies: creating one
    after the pre-flight gate is not evidence of a repair.
    """
    try:
        tokens = shlex.split(gate)
    except ValueError as error:
        raise DriveError(f"invalid gate argv: {error}") from error
    if not tokens:
        raise DriveError("gate argv must not be empty")

    base = root.resolve()
    files: set[str] = set()
    for index, token in enumerate(tokens):
        if not token or token.startswith("-"):
            continue
        candidate = Path(token)
        if candidate.is_absolute():
            if index == 0 and (not candidate.is_file() or not os.access(candidate, os.X_OK)):
                raise DriveError(f"gate executable is not runnable: {token}")
            continue
        local = (
            "/" in token
            or token.startswith(".")
            or (base / candidate).is_file()
        )
        if not local:
            continue
        raw_path = candidate if candidate.is_absolute() else base / candidate
        if raw_path.is_symlink():
            raise DriveError(f"gate path must not be a symlink: {token}")
        try:
            relative = raw_path.resolve(strict=False).relative_to(base)
        except ValueError as error:
            raise DriveError(f"gate path escapes the repository: {token}") from error
        if relative == Path("."):
            raise DriveError("gate path must name a file")
        files.add(relative.as_posix())
        if index == 0:
            script = base / relative
            if not script.is_file() or script.is_symlink():
                raise DriveError(f"repository gate script is not a regular file: {relative}")
            if not os.access(script, os.X_OK):
                raise DriveError(f"repository gate script is not executable: {relative}")
    return tokens, tuple(sorted(files))


def _gate_command_files(root: Path, gate: str) -> list[str]:
    """Compatibility-free internal view used by integrity checks."""
    return list(gate_command(root, gate)[1])


@contextmanager
def _private_child_environment(root: Path):
    """Provide private child state; this is not an OS sandbox.

    The implementer must edit the worktree, so environment cleanup cannot
    confine it. In particular, targeting an operator-owned SDK session is
    rejected above rather than being misrepresented as sandboxed execution.
    """
    with tempfile.TemporaryDirectory(prefix="sol-lane-child-") as home:
        private = Path(home)
        state = {
            "HOME": str(private),
            "TMPDIR": str(private / "tmp"),
            "XDG_CACHE_HOME": str(private / "cache"),
            "XDG_CONFIG_HOME": str(private / "config"),
            "XDG_DATA_HOME": str(private / "data"),
            "XDG_RUNTIME_DIR": str(private / "runtime"),
            "XDG_STATE_HOME": str(private / "state"),
        }
        for location in state.values():
            Path(location).mkdir(exist_ok=True)
        environment = proc.sanitized_env()
        environment.update(state)
        yield environment


def gate_tampering(before: Mapping[str, str], after: Mapping[str, str], *,
                   sealed: tuple[str, ...] = GATE_SEALED_NAMES,
                   sealed_paths: Sequence[str] = ()) -> list[str]:
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
        elif path in sealed_paths:
            # A gate command naming a file that did not exist made the pre-flight
            # gate red; creating it now is writing the verdict, not earning it.
            problems.append(f"{path} (added; the gate command runs it)")
    return problems


def assert_verification_intact(baseline: Mapping[str, str], root: Path, gate: str,
                               protected: Sequence[str], *, when: str) -> None:
    problems = gate_tampering(baseline, gate_digests(root, gate, protected),
                              sealed_paths=_gate_command_files(root, gate))
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
