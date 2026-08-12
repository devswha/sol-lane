from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_TEMPLATE = """
[engine]
repo = "fivetaku/insane-review"
sha = "{sha}"

[defaults]
force_answer_after = 0
max_wait = 1200

[projects.demo]
root = "{root}"
include = ["src/**/*.py"]
{extra}
"""


@pytest.fixture
def lane_repo(tmp_path: Path) -> Path:
    (tmp_path / "vendor" / "patches").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def write_config(lane_repo: Path, project_root: Path):
    def _write(*, extra: str = "", sha: str = "deadbeef" * 5, root: Path | None = None) -> Path:
        path = lane_repo / "lane.toml"
        path.write_text(
            CONFIG_TEMPLATE.format(sha=sha, root=(root or project_root).as_posix(), extra=extra),
            encoding="utf-8",
        )
        return path

    return _write
