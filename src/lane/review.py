"""Build and run one Sol Pro review through the CDP engine."""

from __future__ import annotations

import glob
import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import locks, proc
from .config import Project, safe_pack_snapshot
from .salvage import SalvageError, conversation_id

class ReviewError(Exception):
    """The engine did not deliver a verified answer. Maps to exit code 1."""


CDP_URL = "http://127.0.0.1:9222/json/version"
RESPONSE_GLOB = ".insane-review/response_*.md"
FAILURE_GLOB = ".insane-review/failed_*.log"
# Enough of the engine's final words to localise a DOM break: the traceback or
# the Korean error line sits in the last lines, not the progress log above it.
FAILURE_TAIL_CHARS = 8000
# The engine persists the bound conversation URL the moment the message is sent,
# so a run that dies afterwards is recoverable without spending another one.
MANIFEST_GLOB = ".insane-review/manifest_*.json"
# Long enough to load the conversation and read a finished answer, short enough
# that probing a dead one is not a vigil.
HARVEST_WAIT_SECONDS = 300

# A refusal is a delivered page, not a delivered answer. Measured 2026-08-13: a
# question phrased as "find the most feasible way to break this guarantee" came
# back as 이 콘텐츠는 표시할 수 없습니다 / Trusted Access, and the lane filed it as a
# verified response with exit 0.
REFUSAL_MARKERS = (
    "이 콘텐츠는 표시할 수 없습니다",
    "Trusted Access",
    "사이버보안 관련 요청은",
    "I can't help with that",
    "I'm unable to help with that",
)
# How much of the prompt's head, whitespace-normalised, may not reappear as the
# answer. The same run also saved the user turn as the assistant's reply.
PROMPT_ECHO_CHARS = 200
# Below this the head is not evidence of anything: a short question is quoted by
# perfectly good answers.
MIN_ECHO_CHARS = 120
# What the engine's own header always carries. Anything else is content.
HEADER_MARKERS = ("- 모델:", "- 패킹:")
CONVERSATION_RE = re.compile(r"/c/[0-9a-f]{8}[0-9a-f-]{4,}", re.IGNORECASE)
COMPLETED_RESPONSE_RE = re.compile(r"^\[완료\] 응답 저장:\s*(.+?)\s*$", re.MULTILINE)
X11_SOCKETS = str(Path(tempfile.gettempdir()) / ".X11-unix" / "X*")
# Measured over five long runs: 78 KB reasoned 37m, 164 KB 31m, 290 KB 40m. Pack
# size does not set the latency — the question does — so this warning is a hint
# about egress volume, not a defence against a turn that dies past the half hour.
# force_answer_after is what bounds that; see lane.toml.
PACK_WARN_BYTES = 200_000


@dataclass(frozen=True)
class ReviewOutcome:
    returncode: int
    response: Path | None
    rejected: Path | None = None
    reason: str | None = None


def engine_args(project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
                council: bool = False, stream: bool = False) -> list[str]:
    """Engine arguments for a correctness review.

    Deliberately never emits --compress or --remove-comments: they strip
    function bodies, which is exactly the evidence a review needs.

    ``council`` switches to the stdout-only mode (progress goes to stderr and
    the prompt is positional), which is what a caller that wants the answer as
    a string needs.
    """
    globs = include or project.include
    args = [
        "--target", ".",
        "--include", ",".join(globs),
        "--model", project.model,
        "--require-model", project.require_model,
        "--max-wait", str(project.max_wait),
    ]
    if project.force_answer_after:
        args += ["--force-answer-after", str(project.force_answer_after)]
    if project.no_project:
        args.append("--no-project")
    if project.delete_pack:
        args.append("--delete-pack")
    if council:
        return [*args, "--council", prompt]
    if stream:
        # The engine's live-response chunks print as they arrive; run_relay
        # forwards them, so a long Pro turn is watchable instead of silent.
        args.append("--stream")
    return [*args, "--prompt", prompt]


def command(engine: Path, project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
            python: str | None = None, council: bool = False, stream: bool = False) -> list[str]:
    return [python or sys.executable, str(engine),
            *engine_args(project, prompt, include=include, council=council, stream=stream)]


def harvest_command(engine: Path, project: Project, source: str, *,
                    max_wait: int = HARVEST_WAIT_SECONDS, python: str | None = None) -> list[str]:
    """Read an answer out of a conversation that was already paid for.

    No packing, no prompt, no send: *source* is a conversation URL or the run
    manifest the engine wrote when it sent the message.

    The project's ``max_wait`` is deliberately not inherited. That budget exists
    for a message in flight; a harvest asks "is the answer there now", and a
    conversation that was interrupted would otherwise be watched for an hour.
    Pass a longer wait when the answer is known to still be generating.
    """
    return [python or sys.executable, str(engine),
            "--harvest", source,
            # The engine refuses --require-model without --model, even on a path
            # that selects nothing: harvest never opens the model switcher.
            "--model", project.model,
            "--require-model", project.require_model,
            "--max-wait", str(max_wait)]


def followup_command(engine: Path, project: Project, source: str, prompt: str, *,
                     max_wait: int | None = None, python: str | None = None) -> list[str]:
    """Ask a follow-up inside a conversation that already carries the context.

    Nothing is packed and no new chat is opened: the engine types into the bound
    conversation. The model is not re-verified here — selection happens when a
    chat is created, and this path deliberately never creates one — but the pair
    is still passed because the engine refuses --require-model without --model.
    """
    return [python or sys.executable, str(engine),
            "--continue-chat", source,
            "--model", project.model,
            "--require-model", project.require_model,
            "--max-wait", str(max_wait or project.max_wait),
            "--prompt", prompt]


def followup(engine: Path, project: Project, root: Path, source: str, prompt: str, *,
             max_wait: int | None = None, python: str | None = None) -> ReviewOutcome:
    """Send *prompt* into an existing conversation and verify what comes back."""
    environment = browser_env()
    environment["INSANE_REVIEW_OUT"] = str(_artifact_directory(root))
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        before_manifests = {
            path for path in root.glob(MANIFEST_GLOB) if _regular_file(path)
        }
        result = proc.run(followup_command(engine, project, source, prompt,
                                          max_wait=max_wait, python=python),
                          cwd=root, env=environment)
        response = response_from_successful_run(root, before, result, before_manifests)
    return _verified(result.returncode, response, prompt)


def newest_manifest(root: Path) -> Path | None:
    manifests = [path for path in root.glob(MANIFEST_GLOB) if _regular_file(path)]
    return max(manifests, key=lambda path: path.stat().st_mtime, default=None)


def conversation_of(manifest: Path) -> str | None:
    text = _read_regular_text(manifest)
    if text is None:
        return None
    try:
        url = json.loads(text).get("chat_url")
    except (AttributeError, ValueError):
        return None
    if not isinstance(url, str):
        return None
    try:
        conversation_id(url)
    except SalvageError:
        return None
    return url


def pack_bytes(root: Path, globs: tuple[str, ...]) -> tuple[int, int]:
    """Total bytes and file count the include globs will pull into the pack."""
    matched: set[Path] = set()
    for pattern in globs:
        matched.update(path for path in root.glob(pattern) if path.is_file())
    return sum(path.stat().st_size for path in matched), len(matched)


def cdp_up(url: str = CDP_URL, *, timeout: float = 3.0) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 80
    except ValueError:
        return False
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        return False
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    try:
        connection.request("GET", parsed.path or "/")
        response = connection.getresponse()
        if response.status != 200:
            return False
        json.loads(response.read())
    except (http.client.HTTPException, OSError, ValueError):
        return False
    finally:
        connection.close()
    return True


def browser_env(env: dict[str, str] | None = None, *, socket_glob: str = X11_SOCKETS) -> dict[str, str]:
    """Environment for launching the browser.

    A shell started outside the desktop session (ssh, cron, an agent) has no
    DISPLAY, and the engine's launcher then times out with no useful message.
    Point it at the running X display when one exists.
    """
    resolved = dict(os.environ if env is None else env)
    if resolved.get("DISPLAY"):
        return resolved
    sockets = sorted(glob.glob(socket_glob))
    if sockets:
        resolved["DISPLAY"] = ":" + Path(sockets[0]).name.lstrip("X")
    return resolved


def ensure_browser(engine: Path, *, python: str | None = None, timeout: float = 180.0) -> str:
    """Ask the engine to start the saved browser profile. Returns its STATUS line."""
    result = proc.run(
        [python or sys.executable, str(engine), "--ensure-env"],
        timeout=timeout,
        env=browser_env(),
        allow_descendants=True,
    )
    for line in reversed((result.stdout + result.stderr).splitlines()):
        if line.startswith("STATUS "):
            return line.strip()
    return "STATUS unknown"


def responses(root: Path) -> set[Path]:
    return {path for path in root.glob(RESPONSE_GLOB) if _regular_file(path)}


def newest_new_response(root: Path, before: set[Path]) -> Path | None:
    """The newest response this run produced, or None.

    A path appearing is not a delivery: an empty file, a directory, or an
    unreadable blob must not be reported as a verified answer.
    """
    created = [path for path in responses(root) - before if _is_answer(path)]
    if not created:
        return None
    return max(created, key=lambda path: path.stat().st_mtime)


def _is_answer(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return False
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return bool(stream.read().strip())
    except (OSError, UnicodeDecodeError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _read_regular_text(path: Path) -> str | None:
    """Read UTF-8 text from a regular file without following a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return stream.read()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def response_from_successful_run(root: Path, before: set[Path], result: proc.Completed,
                                 before_manifests: set[Path]) -> Path | None:
    """Accept only the new response path a successful engine explicitly reported."""
    if result.returncode != 0:
        return None
    reported = COMPLETED_RESPONSE_RE.findall(result.stdout)
    if len(reported) != 1:
        return None
    path = Path(reported[0])
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    expected_dir = (root / ".insane-review").resolve()
    if resolved.parent != expected_dir or not resolved.match("response_*.md"):
        return None
    if resolved in before or not _is_answer(resolved):
        return None
    manifest = resolved.with_name(
        resolved.name.replace("response_", "manifest_", 1).removesuffix(".md") + ".json"
    )
    if manifest in before_manifests or not _regular_file(manifest) or conversation_of(manifest) is None:
        return None
    return resolved


def answer_body(text: str) -> str:
    """The answer under the engine's metadata header.

    Only a real header is stripped. Splitting on the first rule regardless meant a
    refusal followed by `---` was discarded *as* the header, leaving a footer to
    pass verification. (Sol Pro, reviewing this file 2026-08-13.)
    """
    head, separator, body = text.partition("\n---\n")
    if separator and head.startswith("#") and any(marker in head for marker in HEADER_MARKERS):
        return body.strip()
    return text.strip()


def rejection_reason(body: str, prompt: str) -> str | None:
    """Why this text is not an answer, if it is not.

    The lane's promise is that a saved response was verified. A refusal page and
    an echo of the question both arrive as a non-empty file with exit 0, so the
    file existing is not the evidence — this is.
    """
    normalised = " ".join(body.split())
    if not normalised:
        return "the saved answer is empty"
    refusal = refusal_in(body)
    if refusal is not None:
        return refusal
    head = " ".join(prompt.split())[:PROMPT_ECHO_CHARS]
    if len(head) >= MIN_ECHO_CHARS and head in normalised:
        return "the saved text is the prompt echoed back, not an answer"
    return None


def refusal_in(text: str) -> str | None:
    """The refusal marker this text carries, if any."""
    for marker in REFUSAL_MARKERS:
        if marker in text:
            return f"the model refused: {marker}"
    return None


def reject(path: Path, reason: str) -> Path:
    """Move a non-answer out of the response namespace and say why."""
    directory = path.parent
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as error:
        raise ReviewError(f"refusing to reject unsafe response artifact: {path}") from error
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReviewError(f"refusing to reject unsafe response artifact: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            text = stream.read()
        target = path.with_name(path.name.replace("response_", "rejected_", 1))
        if target == path:
            target = path.with_name(f"rejected_{path.name}")
        descriptor = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(f"# REJECTED — {reason}\n\n{text}")
        os.unlink(path.name, dir_fd=directory_fd)
        return target
    except OSError as error:
        raise ReviewError(f"refusing to mutate rejection artifact for {path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def ask(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> str:
    """Pack the project, ask Sol Pro, and return the answer as text."""
    globs = include or project.include
    # Re-checked on every call: a drive iteration can create a secret file
    # between the start of the loop and the next planning request.
    council = command(engine, project, prompt, include=include, python=python, council=True)
    try:
        with safe_pack_snapshot(project, root, globs) as snapshot:
            environment = browser_env()
            environment["INSANE_REVIEW_OUT"] = str(_artifact_directory(root))
            # One browser: another lane process must not drive it at the same time.
            with locks.exclusive(locks.browser_lock_path()):
                result = proc.run(
                    council,
                    cwd=snapshot,
                    env=environment,
                    timeout=project.max_wait + 300,
                )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReviewError(f"engine could not run: {error}") from error
    if result.returncode != 0:
        raise ReviewError(f"engine exited {result.returncode}: {result.detail()}")
    answer = result.stdout.strip()
    if not answer:
        raise ReviewError("engine returned an empty answer (fail-closed)")
    reason = rejection_reason(answer, prompt)
    if reason is not None:
        raise ReviewError(f"engine delivered a page but not an answer — {reason}")
    return answer


def harvest(engine: Path, project: Project, root: Path, source: str, *,
            max_wait: int = HARVEST_WAIT_SECONDS, python: str | None = None) -> ReviewOutcome:
    """Recover the answer from an existing conversation. Sends nothing.

    Nothing is packed, so there is no egress to guard here; the browser lock is
    still required because the harvest drives the same single browser.
    """
    environment = browser_env()
    environment["INSANE_REVIEW_OUT"] = str(_artifact_directory(root))
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        before_manifests = {
            path for path in root.glob(MANIFEST_GLOB) if _regular_file(path)
        }
        result = proc.run(harvest_command(engine, project, source, max_wait=max_wait, python=python),
                          cwd=root, env=environment)
        response = response_from_successful_run(root, before, result, before_manifests)
    # A harvest has no prompt to compare against — the conversation is whatever it
    # is — but a refusal page is still not an answer.
    return _verified(result.returncode, response, "")


def run(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None,
        stream: bool = False) -> ReviewOutcome:
    globs = include or project.include
    with safe_pack_snapshot(project, root, globs) as snapshot:
        environment = browser_env()
        environment["INSANE_REVIEW_OUT"] = str(_artifact_directory(root))
        # Held across the harvest too: otherwise a concurrent run's response file
        # can be picked up as the answer to this prompt.
        with locks.exclusive(locks.browser_lock_path()):
            before = responses(root)
            before_manifests = {
                path for path in root.glob(MANIFEST_GLOB) if _regular_file(path)
            }
            result = proc.run_relay(
                command(engine, project, prompt, include=include, python=python, stream=stream),
                cwd=snapshot,
                env=environment,
                timeout=project.max_wait + 300,
            )
            response = response_from_successful_run(root, before, result, before_manifests)
    outcome = _verified(result.returncode, response, prompt)
    if outcome.response is None:
        persist_failure(root, result, outcome)
    return outcome


def _artifact_directory(root: Path) -> Path:
    directory = root / ".insane-review"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        mode = directory.lstat().st_mode
    except OSError as error:
        raise ReviewError(f"cannot open review artifact directory: {error}") from error
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise ReviewError("review artifact directory must be a real directory")
    return directory.resolve()


def persist_failure(root: Path, result: proc.Completed, outcome: ReviewOutcome) -> Path | None:
    """Keep the engine's last words when a run fails closed.

    The evidence feeds `lane repair`: a DOM break is diagnosed from the error
    tail, which otherwise exists only on the console of the run that died.
    """
    text = result.stdout[-FAILURE_TAIL_CHARS:]
    if not text.strip():
        return None
    directory = root / ".insane-review"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = uuid.uuid4().hex[:8]
    name = f"failed_{stamp}_{tag}.log"
    header = (f"# failed review run {stamp}_{tag}\n"
              f"# exit {result.returncode}, reason: {outcome.reason or 'no verified response'}\n")
    try:
        path = proc.atomic_write_text(directory, name, header + text)
        receipt = {
            "version": 1,
            "path": str(path.resolve()),
            "sha256": hashlib.sha256((header + text).encode()).hexdigest(),
        }
        receipt_path = proc.trusted_state_path(root, "latest-failure.json")
        proc.atomic_write_text(
            receipt_path.parent,
            receipt_path.name,
            json.dumps(receipt, sort_keys=True) + "\n",
        )
    except OSError:
        return None
    return path


def _verified(returncode: int, response: Path | None, prompt: str) -> ReviewOutcome:
    """A saved file only counts once its contents are an answer to *prompt*."""
    if returncode != 0:
        return ReviewOutcome(returncode=returncode, response=None,
                             reason="engine exited unsuccessfully")
    if response is None:
        return ReviewOutcome(returncode=returncode, response=None)
    if not _is_answer(response):
        return ReviewOutcome(returncode=returncode, response=None,
                             reason="response artifact is unsafe or unreadable")
    try:
        descriptor = os.open(response, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ReviewOutcome(returncode=returncode, response=None,
                             reason="response artifact is unsafe or unreadable")
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            text = stream.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)
    # The whole file is checked for a refusal as well as the body: a marker must
    # not become invisible by landing where a header would be.
    reason = rejection_reason(answer_body(text), prompt) or refusal_in(text)
    if reason is None:
        return ReviewOutcome(returncode=returncode, response=response)
    return ReviewOutcome(returncode=returncode, response=None,
                         rejected=reject(response, reason), reason=reason)
