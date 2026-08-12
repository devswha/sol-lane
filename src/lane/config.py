"""lane.toml loading, merging, and safety validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "lane.toml"

# A packed context leaves this machine. A root holding any of these is the
# wrong root — the lane refuses it instead of trusting a downstream filter.
SECRET_MARKERS = (".env", "id_rsa", "id_ed25519", "artifacts/private")


class ConfigError(Exception):
    """Malformed configuration or an unsafe root. Maps to exit code 2."""


@dataclass(frozen=True)
class EnginePin:
    repo: str
    sha: str

    @property
    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.sha}/bin/pack_and_ask.py"


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    include: tuple[str, ...]
    model: str
    require_model: str
    force_answer_after: int
    max_wait: int
    no_project: bool
    delete_pack: bool


@dataclass(frozen=True)
class Config:
    path: Path
    engine: EnginePin
    projects: dict[str, Project]

    def project(self, name: str) -> Project:
        try:
            return self.projects[name]
        except KeyError:
            known = ", ".join(sorted(self.projects)) or "none"
            raise ConfigError(f"unknown project {name!r}; configured: {known}") from None


_DEFAULTS: dict[str, object] = {
    "model": "pro",
    "require_model": "GPT-5.6",
    "force_answer_after": 0,
    "max_wait": 1200,
    "no_project": True,
    "delete_pack": True,
}


def find_config(start: Path) -> Path:
    """Return the nearest lane.toml at or above *start*."""
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} found at or above {start}")


def secret_markers_in(root: Path) -> list[str]:
    """Return the secret markers present under *root*, newest concern first."""
    found = []
    for marker in SECRET_MARKERS:
        if (root / marker).exists():
            found.append(marker)
    found.extend(sorted(path.name for path in root.glob(".env.*")))
    return found


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error

    engine_table = raw.get("engine")
    if not isinstance(engine_table, dict) or not engine_table.get("repo") or not engine_table.get("sha"):
        raise ConfigError("[engine] requires both repo and sha")
    engine = EnginePin(repo=str(engine_table["repo"]), sha=str(engine_table["sha"]))

    defaults = dict(_DEFAULTS)
    for key, value in (raw.get("defaults") or {}).items():
        if key not in _DEFAULTS:
            raise ConfigError(f"[defaults] has unknown key {key!r}")
        defaults[key] = value

    project_tables = raw.get("projects") or {}
    if not project_tables:
        raise ConfigError("no [projects.<name>] entries configured")

    projects = {}
    for name, table in project_tables.items():
        projects[name] = _project(name, table, defaults)
    return Config(path=path, engine=engine, projects=projects)


def _project(name: str, table: object, defaults: dict[str, object]) -> Project:
    if not isinstance(table, dict):
        raise ConfigError(f"[projects.{name}] must be a table")
    unknown = set(table) - {"root", "include", *_DEFAULTS}
    if unknown:
        raise ConfigError(f"[projects.{name}] has unknown keys: {', '.join(sorted(unknown))}")

    root_value = table.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise ConfigError(f"[projects.{name}] requires a root path")
    root = Path(root_value).expanduser()

    include = table.get("include", [])
    if not isinstance(include, list) or not include or not all(isinstance(item, str) and item for item in include):
        raise ConfigError(f"[projects.{name}] requires a non-empty include list of globs")

    merged = {**defaults, **{key: table[key] for key in _DEFAULTS if key in table}}
    force_answer_after = _non_negative_int(merged["force_answer_after"], name, "force_answer_after")
    max_wait = _non_negative_int(merged["max_wait"], name, "max_wait")
    if max_wait <= 0:
        raise ConfigError(f"[projects.{name}] max_wait must be positive")
    if force_answer_after and force_answer_after >= max_wait:
        raise ConfigError(f"[projects.{name}] max_wait must exceed force_answer_after")

    return Project(
        name=name,
        root=root,
        include=tuple(include),
        model=str(merged["model"]),
        require_model=str(merged["require_model"]),
        force_answer_after=force_answer_after,
        max_wait=max_wait,
        no_project=bool(merged["no_project"]),
        delete_pack=bool(merged["delete_pack"]),
    )


def _non_negative_int(value: object, project: str, key: str) -> int:
    if type(value) is not int or value < 0:
        raise ConfigError(f"[projects.{project}] {key} must be a non-negative integer")
    return value


def checked_root(project: Project) -> Path:
    """Return the project root after existence and secret-exposure checks."""
    root = project.root
    if not root.is_dir():
        raise ConfigError(f"[projects.{project.name}] root does not exist: {root}")
    markers = secret_markers_in(root)
    if markers:
        raise ConfigError(
            f"[projects.{project.name}] root {root} holds secrets ({', '.join(markers)}); "
            "point the lane at a sanitized worktree instead"
        )
    return root
