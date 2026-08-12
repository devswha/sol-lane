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


def test_sync_leaves_no_staging_file_behind(lane_repo: Path, monkeypatch):
    stage_upstream(lane_repo, "VALUE = 1\n", monkeypatch)

    engine_module.sync(lane_repo, PIN)

    assert list(engine_module.vendor_dir(lane_repo).glob("*.staged")) == []
