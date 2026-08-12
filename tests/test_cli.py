from __future__ import annotations

from pathlib import Path

from lane import cli
from lane import engine as engine_module


def vendored_engine(lane_repo: Path) -> Path:
    path = engine_module.engine_path(lane_repo)
    path.write_text("x = 1\n", encoding="utf-8")
    return path


def test_projects_lists_configured_roots(write_config, capsys, project_root: Path):
    config = write_config()

    assert cli.main(["--config", str(config), "projects"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "demo" in out and str(project_root) in out and "ok" in out


def test_review_dry_run_prints_the_verified_engine_command(write_config, lane_repo: Path, capsys):
    config = write_config()
    engine = vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "왜 느린가", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert str(engine) in out
    assert "--require-model GPT-5.6" in out
    assert "왜 느린가" in out


def test_review_without_a_vendored_engine_is_a_config_error(write_config, capsys):
    config = write_config()

    assert cli.main(["--config", str(config), "review", "demo", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "lane engine sync" in capsys.readouterr().err


def test_review_refuses_an_unknown_project(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "ghost", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "unknown project" in capsys.readouterr().err


def test_review_refuses_a_root_that_holds_secrets(write_config, lane_repo: Path, project_root: Path, capsys):
    (project_root / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "p", "--dry-run"]) == cli.EXIT_CONFIG
    assert "holds secrets" in capsys.readouterr().err


def test_include_override_reaches_the_command(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    cli.main(["--config", str(config), "review", "demo", "p", "--include", "docs/**.md, x.py", "--dry-run"])

    assert "--include docs/**.md,x.py" in capsys.readouterr().out


def test_paste_dry_run_prints_the_codexpro_bundle_command(write_config, lane_repo: Path, capsys, project_root: Path):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "review", "demo", "p", "--paste", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("codexpro pro-bundle --root ")
    assert str(project_root) in out
    assert "--glob src/**/*.py" in out


def test_doctor_reports_config_problems_with_exit_two(write_config, capsys, tmp_path: Path):
    config = write_config(root=tmp_path / "gone")

    assert cli.main(["--config", str(config), "doctor"]) == cli.EXIT_CONFIG
    out = capsys.readouterr().out
    assert "engine     missing" in out
    assert "root:demo   missing" in out


def test_doctor_is_clean_when_engine_and_root_are_fine(write_config, lane_repo: Path, capsys):
    config = write_config()
    vendored_engine(lane_repo)

    assert cli.main(["--config", str(config), "doctor"]) == cli.EXIT_OK
    assert "root:demo   ok" in capsys.readouterr().out


def test_missing_config_is_a_config_error(tmp_path: Path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "projects"]) == cli.EXIT_CONFIG
    assert "cannot read" in capsys.readouterr().err
