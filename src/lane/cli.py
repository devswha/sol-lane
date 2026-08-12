"""lane — one entry point for the Sol Pro review lane.

Exit codes are part of the contract so the lane can be scripted:
    0  success (a response was harvested)
    1  delivery failed (fail-closed engine stop, browser down, empty answer)
    2  configuration or engine problem
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import engine as engine_module
from . import paste as paste_module
from . import review as review_module
from .config import Config, ConfigError, checked_root, find_config, load, secret_markers_in

EXIT_OK = 0
EXIT_DELIVERY = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lane", description="Sol Pro review lane")
    parser.add_argument("--config", help="path to lane.toml (default: nearest one at or above cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="pack a project and get a Sol Pro review")
    review.add_argument("project")
    review.add_argument("prompt")
    review.add_argument("--include", help="comma-separated globs overriding the configured set")
    review.add_argument("--paste", action="store_true", help="skip CDP; bundle for manual paste instead")
    review.add_argument("--dry-run", action="store_true", help="print the command instead of running it")

    sub.add_parser("projects", help="list configured projects")
    sub.add_parser("doctor", help="check engine, browser, and project roots")

    engine = sub.add_parser("engine", help="manage the pinned upstream engine")
    engine_sub = engine.add_subparsers(dest="engine_command", required=True)
    sync = engine_sub.add_parser("sync", help="fetch the pinned engine and apply vendor patches")
    sync.add_argument("--refresh", action="store_true", help="re-download even when cached")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        if args.command == "projects":
            return _projects(config)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "engine":
            return _engine_sync(config, refresh=args.refresh)
        return _review(config, args)
    except (ConfigError, engine_module.EngineError) as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except paste_module.PasteError as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_DELIVERY


def _load_config(explicit: str | None) -> Config:
    path = Path(explicit).expanduser() if explicit else find_config(Path.cwd())
    return load(path)


def _repo_root(config: Config) -> Path:
    return config.path.parent


def _projects(config: Config) -> int:
    for name in sorted(config.projects):
        project = config.projects[name]
        state = "ok" if project.root.is_dir() else "missing"
        print(f"{name:12} {state:8} {project.root}  [{len(project.include)} globs]")
    return EXIT_OK


def _doctor(config: Config) -> int:
    root = _repo_root(config)
    problems = 0

    try:
        engine_path = engine_module.resolve(root, override=os.environ.get("LANE_ENGINE"))
        print(f"engine     ok       {engine_path}")
    except engine_module.EngineError as error:
        print(f"engine     missing  {error}")
        problems += 1

    print(f"browser    {'up' if review_module.cdp_up() else 'down':8} CDP {review_module.CDP_URL}")
    print(f"codexpro   {'ok' if shutil.which('codexpro') else 'missing':8} paste lane")
    clipboard = paste_module.clipboard_command()
    print(f"clipboard  {clipboard[0] if clipboard else 'missing':8} paste lane")

    for name in sorted(config.projects):
        project = config.projects[name]
        if not project.root.is_dir():
            print(f"root:{name:6} missing  {project.root}")
            problems += 1
            continue
        markers = secret_markers_in(project.root)
        if markers:
            print(f"root:{name:6} unsafe   {project.root} holds {', '.join(markers)}")
            problems += 1
        else:
            print(f"root:{name:6} ok       {project.root}")
    return EXIT_OK if problems == 0 else EXIT_CONFIG


def _engine_sync(config: Config, *, refresh: bool) -> int:
    result = engine_module.sync(_repo_root(config), config.engine, refresh=refresh)
    applied = ", ".join(result.patches) if result.patches else "none"
    print(f"engine     {result.engine}")
    print(f"upstream   {config.engine.repo}@{config.engine.sha[:12]} ({result.upstream_bytes} bytes)")
    print(f"patches    {applied}")
    return EXIT_OK


def _review(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    include = tuple(glob.strip() for glob in args.include.split(",") if glob.strip()) if args.include else None

    if args.paste:
        if args.dry_run:
            print(" ".join(paste_module.bundle_command(project, root, include=include)))
            return EXIT_OK
        outcome = paste_module.bundle(project, root, include=include)
        print(f"bundle     {outcome.bundle}")
        if outcome.copied_with:
            print(f"clipboard  copied with {outcome.copied_with} — paste into a Pro chat")
        else:
            print("clipboard  unavailable — paste the file above by hand")
        print("harvest    codexpro pro-apply --root "
              f"{root} --stdin   # after Pro answers")
        return EXIT_OK

    engine_path = engine_module.resolve(_repo_root(config), override=os.environ.get("LANE_ENGINE"))
    if args.dry_run:
        print(" ".join(review_module.command(engine_path, project, args.prompt, include=include)))
        return EXIT_OK

    if not review_module.cdp_up():
        status = review_module.ensure_browser(engine_path)
        if not review_module.cdp_up():
            print(f"lane: no CDP browser on 9222 after --ensure-env ({status}); "
                  "start the dedicated profile or use --paste", file=sys.stderr)
            return EXIT_DELIVERY

    outcome = review_module.run(engine_path, project, root, args.prompt, include=include)
    if outcome.returncode != 0 or outcome.response is None:
        print("lane: review did not produce a verified response (fail-closed)", file=sys.stderr)
        return EXIT_DELIVERY
    print(f"response   {outcome.response}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
