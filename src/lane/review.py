"""Build and run one Sol Pro review through the CDP engine."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import Project

class ReviewError(Exception):
    """The engine did not deliver a verified answer. Maps to exit code 1."""


CDP_URL = "http://127.0.0.1:9222/json/version"
RESPONSE_GLOB = ".insane-review/response_*.md"
X11_SOCKETS = "/tmp/.X11-unix/X*"


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
    created = responses(root) - before
    if not created:
        return None
    return max(created, key=lambda path: path.stat().st_mtime)


def ask(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> str:
    """Pack the project, ask Sol Pro, and return the answer as text."""
    council = command(engine, project, prompt, include=include, python=python, council=True)
    try:
        result = proc.run(council, cwd=root, env=browser_env(), timeout=project.max_wait + 300)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReviewError(f"engine could not run: {error}") from error
    if result.returncode != 0:
        raise ReviewError(f"engine exited {result.returncode}: {result.detail()}")
    answer = result.stdout.strip()
    if not answer:
        raise ReviewError("engine returned an empty answer (fail-closed)")
    return answer


def run(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> ReviewOutcome:
    before = responses(root)
    result = proc.run(command(engine, project, prompt, include=include, python=python),
                      cwd=root, env=browser_env(), capture=False)
    return ReviewOutcome(returncode=result.returncode, response=newest_new_response(root, before))
