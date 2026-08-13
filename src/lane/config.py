"""lane.toml loading, merging, and safety validation."""

from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatch
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "lane.toml"

# A packed context leaves this machine. A root holding any of these is the
# wrong root — the lane refuses it instead of trusting a downstream filter.
SECRET_MARKERS = (".env", "id_rsa", "id_ed25519", "artifacts/private")

# Checking the root's top level is a fast smell test, not a guarantee: the pack
# is built from include globs, which can reach a nested .env or follow a symlink
# out of the tree. Every file that is actually going to be packed is matched
# against these, case-insensitively, on every path component.
SECRET_NAME_PATTERNS = (
    ".env", ".env.*", ".env-*", ".envrc", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.pem", "*.key", "*.p12", "*.pfx", "credentials.json", "secrets.*",
)
SECRET_DIR_PREFIXES = ("artifacts/private",)

# `lane drive` freezes these before it lets an implementer near the worktree:
# the gate decides the verdict, so editing the gate is the cheapest way to fake
# one. Repos that verify differently override this per project.
DEFAULT_GATE_PROTECTED = [
    "tests/**/*", "**/conftest.py", "pyproject.toml", "pytest.ini", ".pytest.ini",
    "setup.cfg", "setup.py", "tox.ini", "noxfile.py", "Makefile",
    # `uv run pytest` resolves the interpreter and the dependency set from these,
    # so they decide what the gate actually executes.
    "uv.lock", "uv.toml", ".python-version",
]

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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
    gate: str | None
    model: str
    require_model: str
    force_answer_after: int
    max_wait: int
    no_project: bool
    delete_pack: bool
    gate_protected: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    path: Path
    engine: EnginePin
    defaults: dict[str, object]
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
    "gate_protected": DEFAULT_GATE_PROTECTED,
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


def _matches_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch(lowered, pattern) for pattern in SECRET_NAME_PATTERNS)


def unsafe_pack_paths(root: Path, globs: tuple[str, ...]) -> list[str]:
    """Reasons the include globs must not be packed, as `path: reason` lines.

    The pack is what actually leaves the machine, so this — not the root scan —
    is the load-bearing check.
    """
    resolved_root = root.resolve()
    problems: set[str] = set()
    for pattern in globs:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if _has_symlink_component(root, relative):
                problems.add(f"{relative}: symlinked")
                continue
            if not path.resolve().is_relative_to(resolved_root):
                problems.add(f"{relative}: resolves outside the root")
                continue
            posix = relative.as_posix().lower()
            if any(posix.startswith(prefix) for prefix in SECRET_DIR_PREFIXES):
                problems.add(f"{relative}: private artifact")
            elif any(_matches_secret_name(part) for part in relative.parts):
                problems.add(f"{relative}: secret-like name")
    return sorted(problems)


def _has_symlink_component(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def assert_safe_pack(project: Project, root: Path, globs: tuple[str, ...]) -> None:
    """Fail closed before anything is packed and sent."""
    problems = unsafe_pack_paths(root, globs)
    if problems:
        raise ConfigError(
            f"[projects.{project.name}] refusing to pack: " + "; ".join(problems[:5])
            + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
        )


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error

    engine_table = raw.get("engine")
    if not isinstance(engine_table, dict) or not engine_table.get("repo") or not engine_table.get("sha"):
        raise ConfigError("[engine] requires both repo and sha")
    repo, sha = str(engine_table["repo"]), str(engine_table["sha"])
    if not _REPO_PATTERN.match(repo):
        raise ConfigError("[engine] repo must be owner/name")
    if not _SHA_PATTERN.match(sha):
        # A branch name is a moving target and a path separator escapes the
        # cache directory; a pin has to be a pin.
        raise ConfigError("[engine] sha must be a full 40-character commit hash")
    engine = EnginePin(repo=repo, sha=sha)

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
    return Config(path=path, engine=engine, defaults=defaults, projects=projects)


def _project(name: str, table: object, defaults: dict[str, object]) -> Project:
    if not isinstance(table, dict):
        raise ConfigError(f"[projects.{name}] must be a table")
    unknown = set(table) - {"root", "include", "gate", *_DEFAULTS}
    if unknown:
        raise ConfigError(f"[projects.{name}] has unknown keys: {', '.join(sorted(unknown))}")

    root_value = table.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise ConfigError(f"[projects.{name}] requires a root path")
    root = Path(root_value).expanduser()

    include = table.get("include", [])
    if not isinstance(include, list) or not include or not all(isinstance(item, str) and item for item in include):
        raise ConfigError(f"[projects.{name}] requires a non-empty include list of globs")

    gate = table.get("gate")
    if gate is not None and (not isinstance(gate, str) or not gate.strip()):
        raise ConfigError(f"[projects.{name}] gate must be a non-empty shell command")

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
        gate=gate.strip() if isinstance(gate, str) else None,
        model=str(merged["model"]),
        require_model=str(merged["require_model"]),
        force_answer_after=force_answer_after,
        max_wait=max_wait,
        no_project=bool(merged["no_project"]),
        delete_pack=bool(merged["delete_pack"]),
        gate_protected=_glob_tuple(merged["gate_protected"], name, "gate_protected"),
    )


def _glob_tuple(value: object, project: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"[projects.{project}] {key} must be a list of non-empty globs")
    return tuple(item.strip() for item in value)


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
