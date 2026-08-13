"""Engine resolution and pinned-upstream sync.

The upstream engine is a DOM-driving script that breaks whenever the ChatGPT UI
moves, so local fixes are unavoidable. Keeping those fixes in a download cache
(the default upstream behaviour) loses them on every pin bump. This module owns
the alternative: fetch the pinned SHA, apply versioned patches, verify it
compiles, and write the result to vendor/pack_and_ask.py.

A produced engine is only trusted while a manifest proves which pin and which
patches produced it: after a failed pin bump the previous engine is still on
disk, and running it would silently review with the wrong tool.

One engine is exempt: the one an operator names in LANE_ENGINE. That override
exists to run an engine that is not vendored yet — a patch being written — so
verifying it against the manifest would remove its only purpose. What it must
not be is quiet. An env var set an hour ago and forgotten is the same accident
the manifest exists to prevent, arriving by a different door, so every run that
uses an override says so.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, EnginePin
from .locks import exclusive

VENDOR_DIR = "vendor"
ENGINE_NAME = "pack_and_ask.py"
MANIFEST_NAME = "engine.json"
PATCH_DIR = "patches"
UPSTREAM_CACHE = ".upstream"
SYNC_LOCK = ".engine-sync.lock"
OVERRIDE_ENV = "LANE_ENGINE"


class EngineError(Exception):
    """Engine missing, unbuildable, or not the one the pin describes."""


@dataclass(frozen=True)
class SyncResult:
    engine: Path
    upstream_bytes: int
    patches: tuple[str, ...]


def vendor_dir(repo_root: Path) -> Path:
    return repo_root / VENDOR_DIR


def engine_path(repo_root: Path) -> Path:
    return vendor_dir(repo_root) / ENGINE_NAME


def manifest_path(repo_root: Path) -> Path:
    return vendor_dir(repo_root) / MANIFEST_NAME


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve(repo_root: Path, *, override: str | None = None, pin: EnginePin | None = None) -> Path:
    """Return the engine to run. Fails closed — never silently downloads.

    An override skips the pin and the patch manifest by design; see the module
    docstring, and print `override_notice()` wherever this is called so the skip
    is visible. It still has to be a Python file that compiles, which is the same
    check a vendored engine passes before it is published.
    """
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise EngineError(f"{OVERRIDE_ENV} points at a missing file: {candidate}")
        _verify_compiles(candidate)
        return candidate
    candidate = engine_path(repo_root)
    if not candidate.is_file():
        raise EngineError(f"engine is not vendored yet: {candidate} — run `lane engine sync`")
    if pin is not None:
        _verify_manifest(repo_root, candidate, pin)
    return candidate


def override_notice(override: str | None) -> str | None:
    """What to tell the operator when an override is in play, if one is."""
    if not override:
        return None
    return (f"engine override {override} — unverified: no pin, no patch manifest checked. "
            f"unset {OVERRIDE_ENV} to use the vendored engine")


def _verify_manifest(repo_root: Path, engine: Path, pin: EnginePin) -> None:
    path = manifest_path(repo_root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EngineError(f"engine manifest is missing or unreadable ({error}) — run `lane engine sync`") from error
    if manifest.get("repo") != pin.repo or manifest.get("sha") != pin.sha:
        raise EngineError(
            f"vendored engine was built from {manifest.get('repo')}@{str(manifest.get('sha'))[:12]}, "
            f"not the configured pin — run `lane engine sync`"
        )
    if manifest.get("engine_sha256") != digest(engine):
        raise EngineError("vendored engine does not match its manifest — run `lane engine sync`")
    expected = {entry["name"]: entry["sha256"] for entry in manifest.get("patches", [])}
    actual = {path.name: digest(path) for path in patches(repo_root)}
    if expected != actual:
        raise EngineError("vendor patches changed since the engine was built — run `lane engine sync`")


def patches(repo_root: Path) -> list[Path]:
    directory = vendor_dir(repo_root) / PATCH_DIR
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.patch") if path.is_file())


def fetch_upstream(repo_root: Path, pin: EnginePin, *, refresh: bool = False) -> Path:
    """Download the pinned engine once and keep it for offline re-syncs."""
    cache = vendor_dir(repo_root) / UPSTREAM_CACHE / f"{pin.sha}.py"
    if cache.is_file() and cache.stat().st_size > 0 and not refresh:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(pin.raw_url, timeout=60) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError) as error:
        raise EngineError(f"cannot fetch {pin.raw_url}: {error}") from error
    if not payload.strip():
        raise EngineError(f"upstream engine is empty: {pin.raw_url}")
    with tempfile.NamedTemporaryFile(dir=cache.parent, prefix=".part-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        _verify_compiles(temporary)
        temporary.replace(cache)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return cache


def sync(repo_root: Path, pin: EnginePin, *, refresh: bool = False) -> SyncResult:
    vendor_dir(repo_root).mkdir(parents=True, exist_ok=True)
    # Concurrent syncs would otherwise share one staging path and publish each
    # other's bytes under their own pin.
    with exclusive(vendor_dir(repo_root) / SYNC_LOCK):
        upstream = fetch_upstream(repo_root, pin, refresh=refresh)
        target = engine_path(repo_root)
        applied: list[dict] = []
        with tempfile.TemporaryDirectory(dir=vendor_dir(repo_root), prefix=".sync-") as workspace:
            staged = Path(workspace) / ENGINE_NAME
            staged.write_bytes(upstream.read_bytes())
            for patch in patches(repo_root):
                _apply(staged, patch)
                applied.append({"name": patch.name, "sha256": digest(patch)})
            _verify_compiles(staged)
            engine_digest = digest(staged)
            staged.replace(target)
        manifest_path(repo_root).write_text(
            json.dumps(
                {
                    "repo": pin.repo,
                    "sha": pin.sha,
                    "upstream_sha256": digest(upstream),
                    "patches": applied,
                    "engine_sha256": engine_digest,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    return SyncResult(engine=target, upstream_bytes=upstream.stat().st_size,
                      patches=tuple(entry["name"] for entry in applied))


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
        source = path.read_text(encoding="utf-8")
        if not source.strip():
            raise EngineError(f"engine is empty: {path}")
        compile(source, str(path), "exec")
    except (OSError, SyntaxError) as error:
        raise EngineError(f"engine does not compile: {error}") from error


def as_config_error(error: EngineError) -> ConfigError:
    return ConfigError(str(error))
