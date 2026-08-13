"""Build and run one Sol Pro review through the CDP engine."""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import locks, proc
from .config import Project, assert_safe_pack

class ReviewError(Exception):
    """The engine did not deliver a verified answer. Maps to exit code 1."""


CDP_URL = "http://127.0.0.1:9222/json/version"
RESPONSE_GLOB = ".insane-review/response_*.md"
# The engine persists the bound conversation URL the moment the message is sent,
# so a run that dies afterwards is recoverable without spending another one.
MANIFEST_GLOB = ".insane-review/manifest_*.json"
# Long enough to load the conversation and read a finished answer, short enough
# that probing a dead one is not a vigil.
HARVEST_WAIT_SECONDS = 300
CONVERSATION_RE = re.compile(r"/c/[0-9a-f]{8}[0-9a-f-]{4,}", re.IGNORECASE)
X11_SOCKETS = "/tmp/.X11-unix/X*"
# Measured over five long runs: 78 KB reasoned 37m, 164 KB 31m, 290 KB 40m. Pack
# size does not set the latency — the question does — so this warning is a hint
# about egress volume, not a defence against a turn that dies past the half hour.
# force_answer_after is what bounds that; see lane.toml.
PACK_WARN_BYTES = 200_000


@dataclass(frozen=True)
class ReviewOutcome:
    returncode: int
    response: Path | None


def engine_args(project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
                council: bool = False) -> list[str]:
    """Engine arguments for a correctness review.

    Deliberately never emits --compress or --remove-comments: they strip
    function bodies, which is exactly the evidence a review needs.

    ``council`` switches to the stdout-only mode (progress goes to stderr and
    the prompt is positional), which is what a caller that wants the answer as
    a string needs.
    """
    globs = include or project.include
    args = [
        "--target", ".",
        "--include", ",".join(globs),
        "--model", project.model,
        "--require-model", project.require_model,
        "--max-wait", str(project.max_wait),
    ]
    if project.force_answer_after:
        args += ["--force-answer-after", str(project.force_answer_after)]
    if project.no_project:
        args.append("--no-project")
    if project.delete_pack:
        args.append("--delete-pack")
    if council:
        return [*args, "--council", prompt]
    return [*args, "--prompt", prompt]


def command(engine: Path, project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
            python: str | None = None, council: bool = False) -> list[str]:
    return [python or sys.executable, str(engine),
            *engine_args(project, prompt, include=include, council=council)]


def harvest_command(engine: Path, project: Project, source: str, *,
                    max_wait: int = HARVEST_WAIT_SECONDS, python: str | None = None) -> list[str]:
    """Read an answer out of a conversation that was already paid for.

    No packing, no prompt, no send: *source* is a conversation URL or the run
    manifest the engine wrote when it sent the message.

    The project's ``max_wait`` is deliberately not inherited. That budget exists
    for a message in flight; a harvest asks "is the answer there now", and a
    conversation that was interrupted would otherwise be watched for an hour.
    Pass a longer wait when the answer is known to still be generating.
    """
    return [python or sys.executable, str(engine),
            "--harvest", source,
            # The engine refuses --require-model without --model, even on a path
            # that selects nothing: harvest never opens the model switcher.
            "--model", project.model,
            "--require-model", project.require_model,
            "--max-wait", str(max_wait)]


def newest_manifest(root: Path) -> Path | None:
    manifests = [path for path in root.glob(MANIFEST_GLOB) if path.is_file()]
    return max(manifests, key=lambda path: path.stat().st_mtime, default=None)


def conversation_of(manifest: Path) -> str | None:
    try:
        url = json.loads(manifest.read_text(encoding="utf-8")).get("chat_url")
    except (OSError, ValueError):
        return None
    return url if isinstance(url, str) and CONVERSATION_RE.search(url) else None


def pack_bytes(root: Path, globs: tuple[str, ...]) -> tuple[int, int]:
    """Total bytes and file count the include globs will pull into the pack."""
    matched: set[Path] = set()
    for pattern in globs:
        matched.update(path for path in root.glob(pattern) if path.is_file())
    return sum(path.stat().st_size for path in matched), len(matched)


def cdp_up(url: str = CDP_URL, *, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def browser_env(env: dict[str, str] | None = None, *, socket_glob: str = X11_SOCKETS) -> dict[str, str]:
    """Environment for launching the browser.

    A shell started outside the desktop session (ssh, cron, an agent) has no
    DISPLAY, and the engine's launcher then times out with no useful message.
    Point it at the running X display when one exists.
    """
    resolved = dict(os.environ if env is None else env)
    if resolved.get("DISPLAY"):
        return resolved
    sockets = sorted(glob.glob(socket_glob))
    if sockets:
        resolved["DISPLAY"] = ":" + Path(sockets[0]).name.lstrip("X")
    return resolved


def ensure_browser(engine: Path, *, python: str | None = None, timeout: float = 180.0) -> str:
    """Ask the engine to start the saved browser profile. Returns its STATUS line."""
    result = proc.run([python or sys.executable, str(engine), "--ensure-env"],
                      timeout=timeout, env=browser_env())
    for line in reversed((result.stdout + result.stderr).splitlines()):
        if line.startswith("STATUS "):
            return line.strip()
    return "STATUS unknown"


def responses(root: Path) -> set[Path]:
    return set(root.glob(RESPONSE_GLOB))


def newest_new_response(root: Path, before: set[Path]) -> Path | None:
    """The newest response this run produced, or None.

    A path appearing is not a delivery: an empty file, a directory, or an
    unreadable blob must not be reported as a verified answer.
    """
    created = [path for path in responses(root) - before if _is_answer(path)]
    if not created:
        return None
    return max(created, key=lambda path: path.stat().st_mtime)


def _is_answer(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def ask(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> str:
    """Pack the project, ask Sol Pro, and return the answer as text."""
    globs = include or project.include
    # Re-checked on every call: a drive iteration can create a secret file
    # between the start of the loop and the next planning request.
    assert_safe_pack(project, root, globs)
    council = command(engine, project, prompt, include=include, python=python, council=True)
    try:
        # One browser: another lane process must not drive it at the same time.
        with locks.exclusive(locks.browser_lock_path()):
            result = proc.run(council, cwd=root, env=browser_env(), timeout=project.max_wait + 300)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReviewError(f"engine could not run: {error}") from error
    if result.returncode != 0:
        raise ReviewError(f"engine exited {result.returncode}: {result.detail()}")
    answer = result.stdout.strip()
    if not answer:
        raise ReviewError("engine returned an empty answer (fail-closed)")
    return answer


def harvest(engine: Path, project: Project, root: Path, source: str, *,
            max_wait: int = HARVEST_WAIT_SECONDS, python: str | None = None) -> ReviewOutcome:
    """Recover the answer from an existing conversation. Sends nothing.

    Nothing is packed, so there is no egress to guard here; the browser lock is
    still required because the harvest drives the same single browser.
    """
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        result = proc.run(harvest_command(engine, project, source, max_wait=max_wait, python=python),
                          cwd=root, env=browser_env(), capture=False)
        response = newest_new_response(root, before)
    return ReviewOutcome(returncode=result.returncode, response=response)


def run(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> ReviewOutcome:
    assert_safe_pack(project, root, include or project.include)
    # Held across the harvest too: otherwise a concurrent run's response file
    # can be picked up as the answer to this prompt.
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        result = proc.run(command(engine, project, prompt, include=include, python=python),
                          cwd=root, env=browser_env(), capture=False)
        response = newest_new_response(root, before)
    return ReviewOutcome(returncode=result.returncode, response=response)
