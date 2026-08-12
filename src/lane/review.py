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

from .config import Project

CDP_URL = "http://127.0.0.1:9222/json/version"
RESPONSE_GLOB = ".insane-review/response_*.md"
X11_SOCKETS = "/tmp/.X11-unix/X*"


@dataclass(frozen=True)
class ReviewOutcome:
    returncode: int
    response: Path | None


def engine_args(project: Project, prompt: str, *, include: tuple[str, ...] | None = None) -> list[str]:
    """Engine arguments for a correctness review.

    Deliberately never emits --compress or --remove-comments: they strip
    function bodies, which is exactly the evidence a review needs.
    """
    globs = include or project.include
    args = [
        "--target", ".",
        "--include", ",".join(globs),
        "--model", project.model,
        "--require-model", project.require_model,
        "--max-wait", str(project.max_wait),
        "--prompt", prompt,
    ]
    if project.force_answer_after:
        args += ["--force-answer-after", str(project.force_answer_after)]
    if project.no_project:
        args.append("--no-project")
    if project.delete_pack:
        args.append("--delete-pack")
    return args


def command(engine: Path, project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
            python: str | None = None) -> list[str]:
    return [python or sys.executable, str(engine), *engine_args(project, prompt, include=include)]


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
    result = subprocess.run(
        [python or sys.executable, str(engine), "--ensure-env"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=browser_env(),
    )
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


def run(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> ReviewOutcome:
    before = responses(root)
    result = subprocess.run(command(engine, project, prompt, include=include, python=python),
                            cwd=root, env=browser_env())
    return ReviewOutcome(returncode=result.returncode, response=newest_new_response(root, before))
