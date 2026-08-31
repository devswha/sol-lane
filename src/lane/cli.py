"""lane — one entry point for the Sol Pro review lane.

Exit codes are part of the contract so the lane can be scripted:
    0  success (a response was harvested)
    1  delivery failed (fail-closed engine stop, browser down, empty answer)
    2  configuration or engine problem
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
from pathlib import Path

from . import drive as drive_module
from . import engine as engine_module
from . import locks, proc
from . import paste as paste_module
from . import repair as repair_module
from . import review as review_module
from . import salvage as salvage_module
from . import serve as serve_module
from .config import Config, ConfigError, adhoc_config, adhoc_project, checked_root, find_config, load, secret_markers_in

EXIT_OK = 0
EXIT_DELIVERY = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lane", description="Sol Pro review lane")
    parser.add_argument("--config", help="path to lane.toml (default: nearest one at or above cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="pack a project and get a Sol Pro review")
    review.add_argument("project", nargs="?",
                        help="configured project name, or omit with --root for a one-shot review")
    review.add_argument("prompt")
    review.add_argument("--root", help="ad-hoc: review this worktree without registering a project "
                                       "(requires --include)")
    review.add_argument("--include", help="comma-separated globs overriding the configured set")
    review.add_argument("--paste", action="store_true", help="skip CDP; bundle for manual paste instead")
    review.add_argument("--stream", action="store_true",
                        help="relay the engine's live response chunks while Pro is thinking")
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

    followup = sub.add_parser(
        "followup", help="ask again inside a conversation that already has the context")
    followup.add_argument("project")
    followup.add_argument("prompt")
    followup.add_argument("source", nargs="?",
                          help="conversation URL or manifest path (default: this project's newest run)")
    followup.add_argument("--max-wait", type=int, help="override the configured max_wait")
    followup.add_argument("--dry-run", action="store_true", help="print the command instead of running it")

    salvage = sub.add_parser(
        "salvage", help="take whatever an interrupted conversation still shows (unverified)")
    salvage.add_argument("project")
    salvage.add_argument("source", nargs="?",
                         help="conversation URL or manifest path (default: this project's newest run)")

    sub.add_parser("projects", help="list configured projects")
    sub.add_parser("doctor", help="check engine, browser, and project roots")
    repair = sub.add_parser(
        "repair", help="hand the newest engine failure to a gjc repair session")
    repair.add_argument("project", nargs="?",
                        help="look for evidence under this project (default: all projects)")
    repair.add_argument("--evidence", help="explicit failure log path instead of the newest one")
    repair.add_argument("--dry-run", action="store_true", help="print the brief and command, change nothing")


    engine = sub.add_parser("engine", help="manage the vendored engine")
    engine_sub = engine.add_subparsers(dest="engine_command", required=True)
    engine_export = engine_sub.add_parser(
        "export", help="copy the committed engine to a consumer checkout with provenance")
    engine_export.add_argument("destination", help="path the engine copy is written to")
    serve = sub.add_parser(
        "serve",
        help="expose Sol Pro on loopback; put TLS/authentication at a reverse proxy for remote access",
    )
    serve.add_argument("--host", default="127.0.0.1",
                       help="loopback bind address only (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8799)
    serve.add_argument("--max-wait", type=int, help="override the configured max_wait")
    serve.add_argument("--force-answer-after", type=int, help="override the configured force_answer_after")

    return parser


def main(argv: list[str] | None = None) -> int:
    proc.exit_on_sigterm()
    try:
        return _dispatch(argv)
    except BrokenPipeError:
        # `lane doctor | head` closes the pipe mid-write. Python would otherwise
        # print a traceback at shutdown for what the caller asked for. Pointing
        # stdout at /dev/null silences that; when stdout is not a real file
        # descriptor there is nothing to silence.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, io.UnsupportedOperation):
            pass
        return EXIT_OK


def _dispatch(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config, adhoc_ok=args.command == "review" and bool(args.root))
        if args.command == "projects":
            return _projects(config)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "repair":
            return _repair(config, args)
        if args.command == "engine":
            return _engine_export(config, args)
        if args.command == "serve":
            return _serve(config, args)
        if args.command == "drive":
            return _drive(config, args)
        if args.command == "harvest":
            return _harvest(config, args)
        if args.command == "salvage":
            return _salvage(config, args)
        if args.command == "followup":
            return _followup(config, args)
        return _review(config, args)
    except (ConfigError, engine_module.EngineError) as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except (paste_module.PasteError, review_module.ReviewError, drive_module.DriveError,
            repair_module.RepairError, salvage_module.SalvageError, locks.LockBusy) as error:
        print(f"lane: {error}", file=sys.stderr)
        return EXIT_DELIVERY


def _load_config(explicit: str | None, *, adhoc_ok: bool = False) -> Config:
    """The nearest lane.toml — or, for `review --root`, built-in defaults.

    Ad-hoc mode must work from any directory (omg calls it with --root from
    foreign repos), so when no lane.toml exists anywhere above the cwd the
    fallback is a defaults-only Config anchored at this repository, whose
    committed engine is the one that runs. Non-adhoc commands keep failing
    loudly on a missing config.
    """
    if explicit:
        return load(Path(explicit).expanduser())
    try:
        return load(find_config(Path.cwd()))
    except ConfigError:
        if adhoc_ok:
            return adhoc_config()
        raise


def _repo_root(config: Config) -> Path:
    return config.path.parent


def _engine(config: Config) -> Path:
    override = os.environ.get(engine_module.OVERRIDE_ENV)
    notice = engine_module.override_notice(override)
    if notice:
        # Loud on every run: a forgotten env var is how you end up reviewing
        # with an engine nobody committed or reviewed.
        print(f"lane: {notice}", file=sys.stderr)
    return engine_module.resolve(_repo_root(config), override=override)


def _projects(config: Config) -> int:
    for name in sorted(config.projects):
        project = config.projects[name]
        state = "ok" if project.root.is_dir() else "missing"
        print(f"{name:12} {state:8} {project.root}  [{len(project.include)} globs]")
    return EXIT_OK


def _doctor(config: Config) -> int:
    root = _repo_root(config)
    problems = 0

    override = os.environ.get(engine_module.OVERRIDE_ENV)
    try:
        engine_path = engine_module.resolve(root, override=override)
        state = "override" if override else "ok"
        print(f"engine     {state:8} {engine_path}")
        notice = engine_module.override_notice(override)
        if notice:
            print(f"           {notice}")
            problems += 1
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


def _engine_export(config: Config, args: argparse.Namespace) -> int:
    destination = Path(args.destination).expanduser()
    provenance = engine_module.export(_repo_root(config), destination)
    print(f"engine     {destination}")
    print(f"sha256     {provenance['sha256'][:16]}…  from {provenance['source_commit'][:12]}")
    print(f"provenance {destination.with_name(destination.name + '.provenance.json')}")
    return EXIT_OK


def _serve(config: Config, args: argparse.Namespace) -> int:
    engine_path = _engine(config)
    token = os.environ.get("SOL_PRO_LOCAL_KEY", "").strip() or None
    if not serve_module.is_loopback_host(args.host):
        raise ConfigError(
            f"refusing plaintext non-loopback bind {args.host}: terminate TLS and enforce "
            "authentication at a reverse proxy, then bind lane serve to loopback"
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


def _followup(config: Config, args: argparse.Namespace) -> int:
    project = config.project(args.project)
    root = checked_root(project)
    engine_path = _engine(config)
    source = _conversation_source(project, root, args.source)

    if args.dry_run:
        print(" ".join(review_module.followup_command(engine_path, project, source, args.prompt,
                                                     max_wait=args.max_wait)))
        return EXIT_OK

    outcome = review_module.followup(engine_path, project, root, source, args.prompt,
                                     max_wait=args.max_wait)
    if outcome.returncode != 0 or outcome.response is None:
        print("lane: the follow-up did not produce a verified response (fail-closed)", file=sys.stderr)
        if outcome.reason:
            print(f"reason     {outcome.reason}", file=sys.stderr)
        return EXIT_DELIVERY
    print(f"response   {outcome.response}")
    return EXIT_OK


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
    if args.root:
        if args.project:
            raise ConfigError("--root and a project name are mutually exclusive")
        include = _globs(args.include)
        if not include:
            raise ConfigError("--root needs --include: an ad-hoc pack is exactly what you name, "
                              "not the whole tree")
        project = adhoc_project(Path(args.root).expanduser().resolve(), config.defaults)
    else:
        if not args.project:
            raise ConfigError("either a configured project name or --root is required")
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
        print(" ".join(review_module.command(engine_path, project, args.prompt, include=include,
                                             stream=args.stream)))
        return EXIT_OK

    _report_pack_size(root, include or project.include)

    if not review_module.cdp_up():
        status = review_module.ensure_browser(engine_path)
        if not review_module.cdp_up():
            print(f"lane: no CDP browser on 9222 after --ensure-env ({status}); "
                  "start the dedicated profile or use --paste", file=sys.stderr)
            return EXIT_DELIVERY

    manifest_before = review_module.newest_manifest(root)
    stamp_before = manifest_before.stat().st_mtime_ns if manifest_before else None
    outcome = review_module.run(engine_path, project, root, args.prompt, include=include,
                                stream=args.stream)
    if outcome.returncode != 0 or outcome.response is None:
        print("lane: review did not produce a verified response (fail-closed)", file=sys.stderr)
        if outcome.reason:
            print(f"reason     {outcome.reason}", file=sys.stderr)
        if outcome.rejected:
            print(f"rejected   {outcome.rejected}", file=sys.stderr)
        _print_harvest_hint(root, project.name,
                            manifest_before=manifest_before, stamp_before=stamp_before)
        return EXIT_DELIVERY
    print(f"response   {outcome.response}")
    return EXIT_OK


def _print_harvest_hint(root: Path, project: str, *,
                        manifest_before: Path | None, stamp_before: int | None) -> None:
    """A spent message is not a lost one: name the retry that costs nothing.

    The gate is a change, not a clock. The engine persists the bound
    conversation the moment the message goes out, so a newest manifest that is
    new or was rewritten during the run means a message is on the table. A
    manifest that already existed unchanged belongs to an older conversation —
    offering it here would harvest that run's answer and file it as the
    response to this prompt (measured 2026-08-27: a fail-before-send run
    pointed at an 08-13 conversation).
    """
    manifest = review_module.newest_manifest(root)
    if manifest is None:
        return
    if manifest == manifest_before and manifest.stat().st_mtime_ns == stamp_before:
        return
    conversation = review_module.conversation_of(manifest)
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


def _repair(config: Config, args: argparse.Namespace) -> int:
    """A dead engine is a repair brief, not a console scrollback hunt."""
    repo_root = _repo_root(config)
    engine_path = _engine(config)

    if args.evidence:
        evidence = Path(args.evidence).expanduser()
        if not evidence.is_file():
            raise ConfigError(f"evidence log not found: {evidence}")
        project_name = args.project or "(explicit evidence)"
    else:
        roots = []
        if args.project:
            project = config.project(args.project)
            roots.append(checked_root(project))
        else:
            for name in sorted(config.projects):
                project = config.projects[name]
                if project.root.expanduser().is_dir():
                    roots.append(project.root.expanduser())
        evidence = repair_module.newest_failure(roots)
        if evidence is None:
            where = f"under {', '.join(str(root) for root in roots)}" if roots else "for any project"
            print(f"lane: no failure evidence found {where} — a failed `lane review` writes "
                  ".insane-review/failed_*.log; run one, or pass --evidence <log>", file=sys.stderr)
            return EXIT_CONFIG
        project_name = args.project or "(newest across projects)"

    brief_text = repair_module.build_brief(evidence, engine_path, project_name=project_name)

    if args.dry_run:
        print(brief_text)
        print("---")
        placeholder = repo_root / repair_module.BRIEF_RELPATH
        print(" ".join(repair_module.repair_command(repo_root, placeholder)))
        return EXIT_OK

    with locks.exclusive(repo_root / repair_module.REPAIR_LOCK, timeout=0):
        brief = repair_module.write_brief(repo_root, brief_text)
        print(f"brief      {brief}")
        print(f"evidence   {evidence}")
        outcome = repair_module.run_repair(repo_root, brief)
    if outcome.returncode != 0:
        print(f"lane: repair session exited {outcome.returncode}", file=sys.stderr)
        print("brief      still on disk — re-run with --evidence to retry with the same evidence",
              file=sys.stderr)
        return EXIT_DELIVERY
    print(outcome.report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
