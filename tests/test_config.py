from __future__ import annotations

from pathlib import Path

import pytest

from lane.config import (
    DEFAULT_GATE_PROTECTED,
    ConfigError,
    assert_safe_pack,
    checked_root,
    find_config,
    load,
    safe_pack_snapshot,
    secret_markers_in,
    unsafe_pack_paths,
)


def test_defaults_apply_and_project_overrides_win(write_config):
    config = load(write_config(extra='force_answer_after = 600\nrequire_model = "GPT-9"\n'))
    project = config.project("demo")

    assert project.force_answer_after == 600
    assert project.require_model == "GPT-9"
    assert project.max_wait == 1200
    assert project.model == "pro"
    assert project.no_project is True


def test_safe_pack_snapshot_freezes_the_validated_bytes(write_config, project_root: Path):
    project = load(write_config()).project("demo")
    source = project_root / "src" / "app.py"
    original = source.read_bytes()

    with safe_pack_snapshot(project, project_root, project.include) as snapshot:
        source.write_text("changed = True\n", encoding="utf-8")
        assert (snapshot / "src" / "app.py").read_bytes() == original


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
        '[projects.demo]\n'
        f'root = "{project_root.as_posix()}"\ninclude = []\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty include"):
        load(path)


def test_an_engine_table_is_rejected(lane_repo: Path, project_root: Path):
    path = lane_repo / "lane.toml"
    path.write_text(
        '[engine]\nrepo = "o/r"\nsha = "' + "a" * 40 + '"\n\n[projects.demo]\n'
        f'root = "{project_root.as_posix()}"\ninclude = ["src/**/*.py"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"\[engine\] is gone"):
        load(path)


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


def test_a_nested_dotenv_is_caught_even_though_the_root_looks_clean(write_config, project_root: Path):
    (project_root / "src" / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    project = load(write_config()).project("demo")

    assert checked_root(project) == project_root, "the root scan cannot see it"
    with pytest.raises(ConfigError, match=r"src/\.env: secret-like name"):
        assert_safe_pack(project, project_root, ("src/**/*",))


def test_dotenv_variants_and_private_keys_are_caught(project_root: Path):
    (project_root / "src" / ".envrc").write_text("x\n", encoding="utf-8")
    (project_root / "src" / ".ENV").write_text("x\n", encoding="utf-8")
    (project_root / "src" / "id_ed25519").write_text("x\n", encoding="utf-8")
    (project_root / "src" / "server.pem").write_text("x\n", encoding="utf-8")

    problems = unsafe_pack_paths(project_root, ("src/*",))

    assert {problem.split(":")[0] for problem in problems} == {
        "src/.envrc", "src/.ENV", "src/id_ed25519", "src/server.pem",
    }


def test_private_artifacts_are_caught_at_any_depth(project_root: Path):
    private = project_root / "artifacts" / "private"
    private.mkdir(parents=True)
    (private / "memory.sqlite3").write_text("x\n", encoding="utf-8")

    assert unsafe_pack_paths(project_root, ("artifacts/**/*",)) == [
        "artifacts/private/memory.sqlite3: private artifact"
    ]


def test_a_symlink_out_of_the_root_is_refused(project_root: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (project_root / "src" / "link.py").symlink_to(outside)

    assert unsafe_pack_paths(project_root, ("src/*.py",)) == ["src/link.py: symlinked"]


def test_ordinary_sources_are_not_flagged(project_root: Path):
    assert unsafe_pack_paths(project_root, ("src/**/*.py",)) == []


def test_find_config_walks_upwards(lane_repo: Path, write_config):
    write_config()
    nested = lane_repo / "a" / "b"
    nested.mkdir(parents=True)

    assert find_config(nested) == lane_repo / "lane.toml"


def test_find_config_fails_when_absent(tmp_path: Path):
    with pytest.raises(ConfigError, match="no lane.toml"):
        find_config(tmp_path)


def test_gate_protected_defaults_to_the_verification_files(write_config):
    project = load(write_config()).project("demo")

    assert project.gate_protected == tuple(DEFAULT_GATE_PROTECTED)
    assert "tests/**/*" in project.gate_protected


def test_gate_protected_can_be_replaced_per_project(write_config):
    project = load(write_config(extra='gate_protected = ["spec/**/*", "vitest.config.ts"]\n')).project("demo")

    assert project.gate_protected == ("spec/**/*", "vitest.config.ts")


def test_gate_protected_must_be_a_list_of_globs(write_config):
    with pytest.raises(ConfigError, match="gate_protected must be a list of non-empty globs"):
        load(write_config(extra='gate_protected = "tests"\n'))
