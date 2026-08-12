"""Engine resolution and pinned-upstream sync.

The upstream engine is a DOM-driving script that breaks whenever the ChatGPT UI
moves, so local fixes are unavoidable. Keeping those fixes in a download cache
(the default upstream behaviour) loses them on every pin bump. This module owns
the alternative: fetch the pinned SHA, apply versioned patches, verify it
compiles, and write the result to vendor/pack_and_ask.py.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, EnginePin

VENDOR_DIR = "vendor"
ENGINE_NAME = "pack_and_ask.py"
PATCH_DIR = "patches"
UPSTREAM_CACHE = ".upstream"


class EngineError(Exception):
    """Engine missing or unbuildable. Maps to exit code 2."""


@dataclass(frozen=True)
class SyncResult:
    engine: Path
    upstream_bytes: int
    patches: tuple[str, ...]


def vendor_dir(repo_root: Path) -> Path:
    return repo_root / VENDOR_DIR


def engine_path(repo_root: Path) -> Path:
    return vendor_dir(repo_root) / ENGINE_NAME


def resolve(repo_root: Path, *, override: str | None = None) -> Path:
    """Return the engine to run. Fails closed — never silently downloads."""
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise EngineError(f"LANE_ENGINE points at a missing file: {candidate}")
        return candidate
    candidate = engine_path(repo_root)
    if not candidate.is_file():
        raise EngineError(f"engine is not vendored yet: {candidate} — run `lane engine sync`")
    return candidate


def patches(repo_root: Path) -> list[Path]:
    directory = vendor_dir(repo_root) / PATCH_DIR
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.patch") if path.is_file())


def fetch_upstream(repo_root: Path, pin: EnginePin, *, refresh: bool = False) -> Path:
    """Download the pinned engine once and keep it for offline re-syncs."""
    cache = vendor_dir(repo_root) / UPSTREAM_CACHE / f"{pin.sha}.py"
    if cache.is_file() and not refresh:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(pin.raw_url, timeout=60) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError) as error:
        raise EngineError(f"cannot fetch {pin.raw_url}: {error}") from error
    if not payload:
        raise EngineError(f"upstream engine is empty: {pin.raw_url}")
    temporary = cache.with_suffix(".part")
    temporary.write_bytes(payload)
    _verify_compiles(temporary)
    temporary.replace(cache)
    return cache


def sync(repo_root: Path, pin: EnginePin, *, refresh: bool = False) -> SyncResult:
    upstream = fetch_upstream(repo_root, pin, refresh=refresh)
    target = engine_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(".staged")
    staged.write_bytes(upstream.read_bytes())
    applied = []
    try:
        for patch in patches(repo_root):
            _apply(staged, patch)
            applied.append(patch.name)
        _verify_compiles(staged)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)
    return SyncResult(engine=target, upstream_bytes=upstream.stat().st_size, patches=tuple(applied))


def _apply(target: Path, patch: Path) -> None:
    result = subprocess.run(
        ["patch", "--forward", "--silent", str(target), str(patch)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise EngineError(
            f"patch {patch.name} does not apply to the pinned upstream engine: "
            f"{detail[0] if detail else 'unknown failure'}"
        )


def _verify_compiles(path: Path) -> None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError) as error:
        raise EngineError(f"engine does not compile: {error}") from error


def as_config_error(error: EngineError) -> ConfigError:
    return ConfigError(str(error))
