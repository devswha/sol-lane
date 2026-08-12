"""CDP-free delivery: bundle context with codexpro, hand it to the clipboard.

Used when the browser lane is unavailable or when Pro is reached by hand. The
model still cannot be called; only the packing and the plan write-back are
automated.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Project

BUNDLE_RELPATH = ".ai-bridge/pro-context.md"
# pro-bundle --copy shells out to pbcopy, which does not exist off macOS.
CLIPBOARD_COMMANDS = (
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
    ("pbcopy",),
)


class PasteError(Exception):
    """codexpro missing or bundling failed. Maps to exit code 1."""


@dataclass(frozen=True)
class PasteOutcome:
    bundle: Path
    copied_with: str | None


def bundle_command(project: Project, root: Path, *, include: tuple[str, ...] | None = None) -> list[str]:
    args = ["codexpro", "pro-bundle", "--root", str(root)]
    for glob in include or project.include:
        args += ["--glob", glob]
    return args


def clipboard_command() -> tuple[str, ...] | None:
    for candidate in CLIPBOARD_COMMANDS:
        if shutil.which(candidate[0]):
            return candidate
    return None


def bundle(project: Project, root: Path, *, include: tuple[str, ...] | None = None) -> PasteOutcome:
    if not shutil.which("codexpro"):
        raise PasteError("codexpro is not installed; `npm install -g codexpro`")

    path = root / BUNDLE_RELPATH
    # A leftover bundle would let a silently failing run copy stale context.
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise PasteError(f"cannot clear the previous bundle at {path}: {error}") from error

    result = subprocess.run(bundle_command(project, root, include=include), cwd=root,
                            capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise PasteError(f"pro-bundle failed: {detail[-1] if detail else 'unknown failure'}")

    if not path.is_file() or not path.read_text(encoding="utf-8", errors="replace").strip():
        raise PasteError(f"pro-bundle reported success but wrote no usable {BUNDLE_RELPATH}")

    command = clipboard_command()
    if command is None:
        return PasteOutcome(bundle=path, copied_with=None)
    copy = subprocess.run(command, input=path.read_bytes())
    return PasteOutcome(bundle=path, copied_with=command[0] if copy.returncode == 0 else None)
