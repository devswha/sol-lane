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

from . import drive as drive_module
from . import engine as engine_module
from . import locks, proc
from . import paste as paste_module
from . import review as review_module
from . import salvage as salvage_module
from . import serve as serve_module
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

    drive = sub.add_parser("drive", help="Pro plans, gjc implements, the local gate decides")
    drive.add_argument("project")
    drive.add_argument("intent")
    drive.add_argument("--include", help="comma-separated globs overriding the configured set")
    drive.add_argument("--max-iters", type=int, default=2, help="planning attempts before giving up")
    drive.add_argument("--session", help="send into this existing gjc SDK session instead of a lane-owned one")
    drive.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")

    harvest = sub.add_parser("harvest", help="recover an answer from a conversation already paid for")
    harvest.add_argument("project")
    harvest.add_argument("source", nargs="?",
                         help="conversation URL or manifest path (default: this project's newest run)")
    harvest.add_argument("--max-wait", type=int, default=review_module.HARVEST_WAIT_SECONDS,
                         help="seconds to wait for the answer (raise it when Pro is still thinking)")
    harvest.add_argument("--dry-run", action="store_true", help="print the command instead of running it")

    salvage = sub.add_parser(
        "salvage", help="take whatever an interrupted conversation still shows (unverified)")
    salvage.add_argument("project")
    salvage.add_argument("source", nargs="?",
                         help="conversation URL or manifest path (default: this project's newest run)")

    sub.add_parser("projects", help="list configured projects")
    sub.add_parser("doctor", help="check engine, browser, and project roots")

    serve = sub.add_parser("serve", help="expose Sol Pro as a local OpenAI-compatible endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8799)
    serve.add_argument("--max-wait", type=int, help="override the configured max_wait")
    serve.add_argument("--force-answer-after", type=int, help="override the configured force_answer_after")

    engine = sub.add_parser("engine", help="manage the pinned upstream engine")
    engine_sub = engine.add_subparsers(dest="engine_command", required=True)
    sync = engine_sub.add_parser("sync", help="fetch the pinned engine and apply vendor patches")
    sync.add_argument("--refresh", action="store_true", help="re-download even when cached")
    return parser


def main(argv: list[str] | None = None) -> int:
    proc.exit_on_sigterm()
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        if args.command == "projects":
            return _projects(config)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "engine":
            return _engine_sync(config, refresh=args.refresh)
        if args.command == "serve":
            return _serve(config, args)
        if args.command == "drive":
            return _drive(config, args)
        if args.command == "harvest":
            return _harvest(config, args)
        if args.command == "salvage":
            return _salvage(config, args)
        return _review(config, args)
    except (ConfigError, engine_module.EngineError) as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except (paste_module.PasteError, review_module.ReviewError, drive_module.DriveError,
            salvage_module.SalvageError, locks.LockBusy) as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_DELIVERY


def _load_config(explicit: str | None) -> Config:
    path = Path(explicit).expanduser() if explicit else find_config(Path.cwd())
    return load(path)


def _repo_root(config: Config) -> Path:
    return config.path.parent


def _engine(config: Config) -> Path:
    return engine_module.resolve(_repo_root(config), override=os.environ.get("LANE_ENGINE"),
                                 pin=config.engine)


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
        engine_path = engine_module.resolve(root, override=os.environ.get("LANE_ENGINE"),
                                            pin=config.engine)
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


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _serve(config: Config, args: argparse.Namespace) -> int:
    engine_path = _engine(config)
    token = os.environ.get("SOL_PRO_LOCAL_KEY", "").strip() or None
    if token is None and args.host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"refusing to bind {args.host} without SOL_PRO_LOCAL_KEY: every request "
            "spends a subscription message"
        )
    defaults = config.defaults
    settings = serve_module.ServeSettings(
        engine=engine_path,
        model=str(defaults["model"]),
        require_model=str(defaults["require_model"]),
        max_wait=args.max_wait if args.max_wait is not None else int(defaults["max_wait"]),
        force_answer_after=(
            args.force_answer_after
            if args.force_answer_after is not None
            else int(defaults["force_answer_after"])
        ),
        token=token,
    )
    if not review_module.cdp_up():
        status = review_module.ensure_browser(engine_path)
        if not review_module.cdp_up():
            print(f"lane: no CDP browser on 9222 after --ensure-env ({status})", file=sys.stderr)
            return EXIT_DELIVERY
    serve_module.serve(settings, host=args.host, port=args.port)
    return EXIT_OK


def _drive(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    if not project.gate:
        raise ConfigError(f"[projects.{project.name}] needs a gate command for `lane drive`")
    include = _globs(args.include)
    engine_path = _engine(config)

    if args.dry_run:
        plan = root / drive_module.PLAN_RELPATH
        print(" ".join(review_module.command(engine_path, project, "<plan request>",
                                             include=include, council=True)))
        print(" ".join(drive_module.implement_command(root, plan, first=True, session=args.session)))
        print(project.gate)
        frozen = drive_module.gate_digests(root, project.gate, project.gate_protected)
        print(f"# frozen verification: {len(frozen)} file(s)")
        return EXIT_OK

    # One plan file, one gjc session directory, one worktree: two drives in the
    # same root would execute each other's plans.
    with locks.exclusive(root / ".ai-bridge" / "drive.lock", timeout=0):
        outcome = _drive_loop(config, args, project, root, include, engine_path)
    if outcome.already_satisfied:
        print("verdict    gate already passes; no work was requested and no message was spent")
        return EXIT_OK
    print(f"verdict    {'PASS' if outcome.passed else 'FAIL'} after {outcome.iterations} attempt(s)")
    if not outcome.passed:
        print(f"plan       {root / drive_module.PLAN_RELPATH}")
        return EXIT_DELIVERY
    return EXIT_OK


def _drive_loop(config: Config, args, project, root: Path, include, engine_path: Path):
    return drive_module.drive(
        root,
        args.intent,
        project.gate,
        max_iters=args.max_iters,
        planner=lambda prompt: review_module.ask(engine_path, project, root, prompt, include=include),
        implementer=lambda plan, first: drive_module.implement(root, plan, first=first, session=args.session),
        gate_runner=lambda: drive_module.run_gate(root, project.gate,
                                                  timeout=project.gate_timeout),
        protected=project.gate_protected,
    )


def _conversation_source(project, root: Path, explicit: str | None) -> str:
    """A conversation URL, from the argument or from this project's newest run."""
    if explicit:
        return explicit
    manifest = review_module.newest_manifest(root)
    if manifest is None:
        raise review_module.ReviewError(
            f"no run manifest under {root / '.insane-review'} for {project.name}")
    return str(manifest)


def _salvage(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    source = _conversation_source(project, root, args.source)
    url = source
    if not source.startswith("http"):
        found = review_module.conversation_of(Path(source))
        if found is None:
            raise salvage_module.SalvageError(f"no conversation URL in {source}")
        url = found

    outcome = salvage_module.salvage(url, root / ".insane-review")
    print(f"salvaged   {outcome.path}")
    print(f"           {outcome.chars} chars, {outcome.assistant_turns} assistant turn(s)"
          f"{', still streaming' if outcome.streaming else ''}")
    if outcome.assistant_turns == 0:
        print("           no assistant message: this is reasoning narration, not an answer",
              file=sys.stderr)
    return EXIT_OK


def _report_pack_size(root: Path, globs: tuple[str, ...]) -> None:
    total, count = review_module.pack_bytes(root, globs)
    print(f"pack       {count} files, {total // 1024} KB")
    if total > review_module.PACK_WARN_BYTES:
        print(f"           over {review_module.PACK_WARN_BYTES // 1024} KB — Pro reasons far longer on "
              "large packs; narrow --include or split the review", file=sys.stderr)


def _globs(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(glob.strip() for glob in value.split(",") if glob.strip())


def _review(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    include = _globs(args.include)

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

    engine_path = _engine(config)
    if args.dry_run:
        print(" ".join(review_module.command(engine_path, project, args.prompt, include=include)))
        return EXIT_OK

    _report_pack_size(root, include or project.include)

    if not review_module.cdp_up():
        status = review_module.ensure_browser(engine_path)
        if not review_module.cdp_up():
            print(f"lane: no CDP browser on 9222 after --ensure-env ({status}); "
                  "start the dedicated profile or use --paste", file=sys.stderr)
            return EXIT_DELIVERY

    outcome = review_module.run(engine_path, project, root, args.prompt, include=include)
    if outcome.returncode != 0 or outcome.response is None:
        print("lane: review did not produce a verified response (fail-closed)", file=sys.stderr)
        _print_harvest_hint(root, project.name)
        return EXIT_DELIVERY
    print(f"response   {outcome.response}")
    return EXIT_OK


def _print_harvest_hint(root: Path, project: str) -> None:
    """A spent message is not a lost one: name the retry that costs nothing."""
    manifest = review_module.newest_manifest(root)
    conversation = review_module.conversation_of(manifest) if manifest else None
    if conversation is None:
        return
    print(f"chat       {conversation}", file=sys.stderr)
    print(f"retry      lane harvest {project}   # no new message is sent", file=sys.stderr)
    print(f"salvage    lane salvage {project}   # unverified: whatever the page still shows",
          file=sys.stderr)


def _harvest(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    engine_path = _engine(config)

    source = _conversation_source(project, root, args.source)

    if args.dry_run:
        print(" ".join(review_module.harvest_command(engine_path, project, source,
                                                     max_wait=args.max_wait)))
        return EXIT_OK

    outcome = review_module.harvest(engine_path, project, root, source, max_wait=args.max_wait)
    if outcome.returncode != 0 or outcome.response is None:
        print("lane: nothing to harvest from that conversation yet", file=sys.stderr)
        return EXIT_DELIVERY
    print(f"response   {outcome.response}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
