from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lane import engine as engine_module


def vendored(lane_repo: Path, source: str = "x = 1\n") -> Path:
    path = engine_module.engine_path(lane_repo)
    path.write_text(source, encoding="utf-8")
    engine_module.ENGINE_SHA256 = engine_module.digest(path)
    return path


def test_resolve_returns_the_committed_engine(lane_repo: Path):
    engine = vendored(lane_repo)

    assert engine_module.resolve(lane_repo) == engine


def test_a_missing_engine_names_the_restore_command(lane_repo: Path):
    with pytest.raises(engine_module.EngineError, match="git checkout"):
        engine_module.resolve(lane_repo)


def test_a_dirty_committed_engine_is_refused(lane_repo: Path, monkeypatch):
    vendored(lane_repo, "x = 2\n")
    (lane_repo / ".git").mkdir()
    monkeypatch.setattr(
        engine_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"x = 1\n"),
    )

    with pytest.raises(engine_module.EngineError, match="differs from the current commit"):
        engine_module.resolve(lane_repo)


def test_an_engine_that_does_not_compile_is_refused(lane_repo: Path):
    vendored(lane_repo, "def broken(:\n")

    with pytest.raises(engine_module.EngineError, match="does not compile"):
        engine_module.resolve(lane_repo)


def test_an_empty_engine_is_refused(lane_repo: Path):
    vendored(lane_repo, "   \n")

    with pytest.raises(engine_module.EngineError, match="empty"):
        engine_module.resolve(lane_repo)


def test_resolve_prefers_the_explicit_override(lane_repo: Path, tmp_path: Path):
    override = tmp_path / "custom.py"
    override.write_text("y = 2\n", encoding="utf-8")
    vendored(lane_repo)

    assert engine_module.resolve(lane_repo, override=str(override)) == override


def test_resolve_rejects_a_missing_override(lane_repo: Path, tmp_path: Path):
    with pytest.raises(engine_module.EngineError, match="missing file"):
        engine_module.resolve(lane_repo, override=str(tmp_path / "gone.py"))


def test_an_override_that_does_not_compile_is_refused(lane_repo: Path, tmp_path: Path):
    """An override skips the committed copy by design, but not the compile
    check: whatever drives the browser has to be Python first."""
    override = tmp_path / "wip.py"
    override.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(engine_module.EngineError, match="does not compile"):
        engine_module.resolve(lane_repo, override=str(override))


def test_an_override_announces_that_nothing_was_verified():
    notice = engine_module.override_notice("/tmp/wip-engine.py")

    assert notice is not None
    assert "/tmp/wip-engine.py" in notice
    assert "unverified" in notice
    assert engine_module.OVERRIDE_ENV in notice


def test_no_override_means_no_notice():
    assert engine_module.override_notice(None) is None
    assert engine_module.override_notice("") is None


def test_real_committed_engine_exports_one_verified_blob(tmp_path: Path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    source = engine_module.engine_path(repo_root)
    monkeypatch.setattr(engine_module, "ENGINE_SHA256", engine_module.digest(source))
    destination = tmp_path / "consumer" / "pack_and_ask.py"

    provenance = engine_module.export(repo_root, destination)

    assert destination.read_bytes() == source.read_bytes()
    assert provenance["sha256"] == engine_module.digest(source)
    assert len(provenance["source_commit"]) == 40
    assert destination.with_name("pack_and_ask.py.provenance.json").is_file()
