from __future__ import annotations

from pathlib import Path

import pytest

from lane.config import ConfigError, checked_root, find_config, load, secret_markers_in


def test_defaults_apply_and_project_overrides_win(write_config):
    config = load(write_config(extra='force_answer_after = 600\nrequire_model = "GPT-9"\n'))
    project = config.project("demo")

    assert project.force_answer_after == 600
    assert project.require_model == "GPT-9"
    assert project.max_wait == 1200
    assert project.model == "pro"
    assert project.no_project is True


def test_unknown_project_lists_the_configured_ones(write_config):
    config = load(write_config())

    with pytest.raises(ConfigError, match="configured: demo"):
        config.project("nope")


def test_unknown_project_key_is_rejected(write_config):
    with pytest.raises(ConfigError, match="unknown keys: compress"):
        load(write_config(extra="compress = true\n"))


def test_force_answer_after_must_stay_below_max_wait(write_config):
    with pytest.raises(ConfigError, match="max_wait must exceed force_answer_after"):
        load(write_config(extra="force_answer_after = 1200\n"))


def test_empty_include_is_rejected(lane_repo: Path, project_root: Path):
    path = lane_repo / "lane.toml"
    path.write_text(
        '[engine]\nrepo = "r"\nsha = "s"\n\n[projects.demo]\n'
        f'root = "{project_root.as_posix()}"\ninclude = []\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty include"):
        load(path)


def test_engine_pin_builds_the_raw_url(write_config):
    config = load(write_config(sha="abc123"))

    assert config.engine.raw_url == (
        "https://raw.githubusercontent.com/fivetaku/insane-review/abc123/bin/pack_and_ask.py"
    )


def test_checked_root_refuses_a_root_holding_secrets(write_config, project_root: Path):
    (project_root / ".env").write_text("EA_LLM_API_KEY=secret\n", encoding="utf-8")
    project = load(write_config()).project("demo")

    with pytest.raises(ConfigError, match=r"holds secrets \(\.env\)"):
        checked_root(project)


def test_checked_root_refuses_a_missing_root(write_config, tmp_path: Path):
    project = load(write_config(root=tmp_path / "gone")).project("demo")

    with pytest.raises(ConfigError, match="root does not exist"):
        checked_root(project)


def test_checked_root_returns_a_clean_root(write_config, project_root: Path):
    project = load(write_config()).project("demo")

    assert checked_root(project) == project_root


def test_secret_markers_cover_dotenv_variants_and_private_artifacts(project_root: Path):
    (project_root / ".env.production").write_text("k=v\n", encoding="utf-8")
    (project_root / "artifacts" / "private").mkdir(parents=True)

    assert secret_markers_in(project_root) == ["artifacts/private", ".env.production"]


def test_find_config_walks_upwards(lane_repo: Path, write_config):
    write_config()
    nested = lane_repo / "a" / "b"
    nested.mkdir(parents=True)

    assert find_config(nested) == lane_repo / "lane.toml"


def test_find_config_fails_when_absent(tmp_path: Path):
    with pytest.raises(ConfigError, match="no lane.toml"):
        find_config(tmp_path)
