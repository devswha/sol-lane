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

from pathlib import Path

from .config import ConfigError

VENDOR_DIR = "vendor"
ENGINE_NAME = "pack_and_ask.py"
OVERRIDE_ENV = "LANE_ENGINE"


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
