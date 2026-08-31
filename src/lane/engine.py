"""Engine resolution for the vendored pack_and_ask.py.

The engine drives a logged-in browser over CDP, so it breaks whenever the
ChatGPT DOM moves. It used to be rebuilt from a pinned upstream SHA plus
vendor patches; upstream is no longer tracked. vendor/pack_and_ask.py is the
source of truth now — committed, reviewed here, and exported to consumers by
script. Nothing downloads anything: `resolve` fails closed unless the
committed file exists and compiles.

One engine is exempt: the one an operator names in LANE_ENGINE. That override
exists to run an engine that is not committed yet — a fix being written — so
requiring it to be the committed copy would remove its only purpose. What it
must not be is quiet. An env var set an hour ago and forgotten is the same
accident the committed engine exists to prevent, arriving by a different
door, so every run that uses an override says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import proc

VENDOR_DIR = "vendor"
ENGINE_NAME = "pack_and_ask.py"
OVERRIDE_ENV = "LANE_ENGINE"
ENGINE_SHA256 = "b3de28beb0f2b70ceccdf60ff0726805e35492533393894f2e667088e8abc7d8"


class EngineError(Exception):
    """Engine missing, empty, or not the Python it claims to be."""


def vendor_dir(repo_root: Path) -> Path:
    return repo_root / VENDOR_DIR


def engine_path(repo_root: Path) -> Path:
    return vendor_dir(repo_root) / ENGINE_NAME


def resolve(repo_root: Path, *, override: str | None = None) -> Path:
    """Return the engine to run. Fails closed — never downloads anything.

    An override skips the committed copy by design; see the module docstring,
    and print `override_notice()` wherever this is called so the skip is
    visible. Both paths must still be a Python file that compiles: with no
    manifest to check against, compiling what is about to drive a browser
    is the integrity check.
    """
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise EngineError(f"{OVERRIDE_ENV} points at a missing file: {candidate}")
        _verify_compiles(candidate)
        return candidate
    candidate = engine_path(repo_root)
    if not candidate.is_file():
        raise EngineError(
            f"vendored engine is missing: {candidate} — it is committed source; "
            "restore it with `git checkout -- vendor/pack_and_ask.py`"
        )
    _verify_compiles(candidate)
    _verify_committed_bytes(repo_root, candidate)
    return candidate


def override_notice(override: str | None) -> str | None:
    """What to tell the operator when an override is in play, if one is."""
    if not override:
        return None
    return (f"engine override {override} — unverified: not the committed engine. "
            f"unset {OVERRIDE_ENV} to use the vendored engine")


def _verify_compiles(path: Path) -> None:
    try:
        source = path.read_text(encoding="utf-8")
        if not source.strip():
            raise EngineError(f"engine is empty: {path}")
        compile(source, str(path), "exec")
    except (OSError, SyntaxError) as error:
        raise EngineError(f"engine does not compile: {error}") from error


def _verify_committed_bytes(repo_root: Path, path: Path) -> None:
    """Reject a dirty vendored engine when repository provenance is available."""
    current_digest = digest(path)
    if current_digest != ENGINE_SHA256:
        raise EngineError(
            "vendored engine differs from the build-pinned digest; update the engine "
            "and its reviewed digest together"
        )
    git_marker = repo_root / ".git"
    if not git_marker.exists():
        return
    try:
        relative = path.resolve(strict=True).relative_to(repo_root.resolve())
    except (OSError, ValueError) as error:
        raise EngineError(f"vendored engine escapes the repository: {path}") from error
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"HEAD:{relative.as_posix()}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        current = path.read_bytes()
    except (OSError, subprocess.SubprocessError) as error:
        raise EngineError(f"could not verify committed engine bytes: {error}") from error
    if result.returncode != 0:
        raise EngineError("vendored engine is not present in the current commit")
    if result.stdout != current:
        raise EngineError(
            "vendored engine differs from the current commit; commit it or use "
            f"{OVERRIDE_ENV} explicitly for development"
        )


def export(repo_root: Path, destination: Path) -> dict[str, str]:
    """Copy the committed engine to a consumer checkout and prove what left.

    The one consumer today is oh-my-gajae-code, whose plugin ships its own
    engine copy; this is the mechanical export that keeps the two from
    drifting. The copy is byte-exact and a sidecar provenance file records
    the digest, the exporting commit, and the time, so the receiving side
    can always answer "which engine is this?".
    """
    source = resolve(repo_root)
    commit, content = _committed_blob(repo_root, source)
    content_digest = hashlib.sha256(content).hexdigest()
    if content_digest != ENGINE_SHA256:
        raise EngineError("committed engine blob does not match the build-pinned digest")
    sidecar = destination.with_name(destination.name + ".provenance.json")
    _clear_publication_marker(sidecar)
    destination = proc.atomic_write_bytes(destination.parent, destination.name, content)
    provenance = {
        "publication_version": 1,
        "artifact": destination.name,
        "source": "sol-lane vendor/pack_and_ask.py",
        "sha256": content_digest,
        "source_commit": commit,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    proc.atomic_write_text(
        sidecar.parent,
        sidecar.name,
        json.dumps(provenance, indent=2) + "\n",
    )
    return provenance


def _clear_publication_marker(sidecar: Path) -> None:
    """Remove the commit marker before replacing bytes; absence means unpublished."""
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(sidecar.parent, flags)
    try:
        try:
            os.unlink(sidecar.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(directory_fd)


def _committed_blob(repo_root: Path, source: Path) -> tuple[str, bytes]:
    commit = _git_commit(repo_root)
    if commit.startswith("unknown"):
        raise EngineError("cannot export engine without committed Git provenance")
    try:
        relative = source.resolve(strict=True).relative_to(repo_root.resolve()).as_posix()
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{commit}:{relative}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise EngineError(f"could not read committed engine blob: {error}") from error
    if result.returncode != 0:
        raise EngineError("could not read committed engine blob")
    return commit, result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(repo_root: Path) -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                                capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown (not a git checkout or git failed)"
