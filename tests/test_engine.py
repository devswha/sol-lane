from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lane import engine as engine_module
from lane.config import EnginePin

PIN = EnginePin(repo="fivetaku/insane-review", sha="a" * 40)


def make_patch(original: str, changed: str, tmp_path: Path) -> str:
    left = tmp_path / "left.py"
    right = tmp_path / "right.py"
    left.write_text(original, encoding="utf-8")
    right.write_text(changed, encoding="utf-8")
    result = subprocess.run(["diff", "-u", str(left), str(right)], capture_output=True, text=True)
    assert result.returncode == 1, "fixture files must differ"
    return result.stdout


def stage_upstream(lane_repo: Path, source: str, monkeypatch) -> Path:
    cache = lane_repo / "vendor" / ".upstream" / f"{PIN.sha}.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(source, encoding="utf-8")
    monkeypatch.setattr(engine_module, "fetch_upstream", lambda *args, **kwargs: cache)
    return cache


def test_resolve_prefers_the_explicit_override(lane_repo: Path, tmp_path: Path):
    override = tmp_path / "custom.py"
    override.write_text("x = 1\n", encoding="utf-8")

    assert engine_module.resolve(lane_repo, override=str(override)) == override


def test_resolve_rejects_a_missing_override(lane_repo: Path, tmp_path: Path):
    with pytest.raises(engine_module.EngineError, match="missing file"):
        engine_module.resolve(lane_repo, override=str(tmp_path / "gone.py"))


def test_resolve_points_at_sync_when_not_vendored(lane_repo: Path):
    with pytest.raises(engine_module.EngineError, match="lane engine sync"):
        engine_module.resolve(lane_repo)


def test_sync_applies_patches_in_order(lane_repo: Path, tmp_path: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    patches = lane_repo / "vendor" / "patches"
    (patches / "0001-first.patch").write_text(make_patch("VALUE = 1\n", "VALUE = 2\n", tmp_path), encoding="utf-8")
    (patches / "0002-second.patch").write_text(make_patch("VALUE = 2\n", "VALUE = 3\n", tmp_path), encoding="utf-8")

    result = engine_module.sync(lane_repo, PIN)

    assert result.patches == ("0001-first.patch", "0002-second.patch")
    assert result.engine.read_text(encoding="utf-8") == "VALUE = 3\n"


def test_sync_fails_closed_when_a_patch_does_not_apply(lane_repo: Path, tmp_path: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    patch = lane_repo / "vendor" / "patches" / "0001-stale.patch"
    patch.write_text(make_patch("OTHER = 9\n", "OTHER = 10\n", tmp_path), encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="does not apply"):
        engine_module.sync(lane_repo, PIN)
    assert not engine_module.engine_path(lane_repo).exists()


def test_sync_rejects_a_patch_that_breaks_syntax(lane_repo: Path, tmp_path: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    patch = lane_repo / "vendor" / "patches" / "0001-broken.patch"
    patch.write_text(make_patch("VALUE = 1\n", "VALUE = (\n", tmp_path), encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="does not compile"):
        engine_module.sync(lane_repo, PIN)
    assert not engine_module.engine_path(lane_repo).exists()


def test_sync_without_patches_reproduces_upstream(lane_repo: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)

    result = engine_module.sync(lane_repo, PIN)

    assert result.patches == ()
    assert result.engine.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_sync_leaves_no_staging_artifacts_behind(lane_repo: Path, tmp_path: Path, monkeypatch):
    """patch leaves .orig/.rej next to its target, so staging happens in a
    throwaway directory rather than beside the published engine."""
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    patch = lane_repo / "vendor" / "patches" / "0001-first.patch"
    patch.write_text(make_patch("VALUE = 1\n", "VALUE = 2\n", tmp_path), encoding="utf-8")

    engine_module.sync(lane_repo, PIN)

    leftovers = [
        path.name
        for path in engine_module.vendor_dir(lane_repo).iterdir()
        if path.name not in {"pack_and_ask.py", "engine.json", "patches", ".upstream",
                             ".engine-sync.lock"}
    ]
    assert leftovers == []


def test_sync_records_a_manifest_that_resolve_verifies(lane_repo: Path, tmp_path: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    patch = lane_repo / "vendor" / "patches" / "0001-first.patch"
    patch.write_text(make_patch("VALUE = 1\n", "VALUE = 2\n", tmp_path), encoding="utf-8")

    engine_module.sync(lane_repo, PIN)

    assert engine_module.resolve(lane_repo, pin=PIN) == engine_module.engine_path(lane_repo)


def test_an_engine_left_over_from_another_pin_is_refused(lane_repo: Path, monkeypatch):
    """A failed pin bump leaves the previous engine on disk; running it would
    review with the wrong tool while reporting success."""
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    engine_module.sync(lane_repo, PIN)

    other = EnginePin(repo=PIN.repo, sha="b" * 40)
    with pytest.raises(engine_module.EngineError, match="not the configured pin"):
        engine_module.resolve(lane_repo, pin=other)


def test_a_tampered_engine_is_refused(lane_repo: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    engine_module.sync(lane_repo, PIN)
    engine_module.engine_path(lane_repo).write_text("VALUE = 99\n", encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="does not match its manifest"):
        engine_module.resolve(lane_repo, pin=PIN)


def test_changed_patches_invalidate_the_vendored_engine(lane_repo: Path, tmp_path: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)
    engine_module.sync(lane_repo, PIN)
    (lane_repo / "vendor" / "patches" / "0001-late.patch").write_text(
        make_patch("VALUE = 1\n", "VALUE = 3\n", tmp_path), encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="patches changed"):
        engine_module.resolve(lane_repo, pin=PIN)


def test_an_empty_upstream_is_refused(lane_repo: Path, monkeypatch):
    stage_upstream(lane_repo, "\n", monkeypatch)

    with pytest.raises(engine_module.EngineError, match="engine is empty"):
        engine_module.sync(lane_repo, PIN)


def test_an_override_that_does_not_compile_is_refused(lane_repo: Path, tmp_path: Path):
    """An override skips the manifest by design, but not the compile check that
    every vendored engine passes."""
    override = tmp_path / "broken.py"
    override.write_text("def oops(\n", encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="does not compile"):
        engine_module.resolve(lane_repo, override=str(override))


def test_an_override_announces_that_nothing_was_verified():
    notice = engine_module.override_notice("/tmp/wip-engine.py")

    assert "/tmp/wip-engine.py" in notice
    assert "unverified" in notice
    assert engine_module.OVERRIDE_ENV in notice


def test_no_override_means_no_notice():
    assert engine_module.override_notice(None) is None
    assert engine_module.override_notice("") is None
