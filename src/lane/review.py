"""Build and run one Sol Pro review through the CDP engine."""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import locks, proc
from .config import Project, assert_safe_pack

class ReviewError(Exception):
    """The engine did not deliver a verified answer. Maps to exit code 1."""


CDP_URL = "http://127.0.0.1:9222/json/version"
RESPONSE_GLOB = ".insane-review/response_*.md"
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
X11_SOCKETS = "/tmp/.X11-unix/X*"
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
                council: bool = False) -> list[str]:
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
    return [*args, "--prompt", prompt]


def command(engine: Path, project: Project, prompt: str, *, include: tuple[str, ...] | None = None,
            python: str | None = None, council: bool = False) -> list[str]:
    return [python or sys.executable, str(engine),
            *engine_args(project, prompt, include=include, council=council)]


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
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        result = proc.run(followup_command(engine, project, source, prompt,
                                          max_wait=max_wait, python=python),
                          cwd=root, env=browser_env(), capture=False)
        response = newest_new_response(root, before)
    return _verified(result.returncode, response, prompt)


def newest_manifest(root: Path) -> Path | None:
    manifests = [path for path in root.glob(MANIFEST_GLOB) if path.is_file()]
    return max(manifests, key=lambda path: path.stat().st_mtime, default=None)


def conversation_of(manifest: Path) -> str | None:
    try:
        url = json.loads(manifest.read_text(encoding="utf-8")).get("chat_url")
    except (OSError, ValueError):
        return None
    return url if isinstance(url, str) and CONVERSATION_RE.search(url) else None


def pack_bytes(root: Path, globs: tuple[str, ...]) -> tuple[int, int]:
    """Total bytes and file count the include globs will pull into the pack."""
    matched: set[Path] = set()
    for pattern in globs:
        matched.update(path for path in root.glob(pattern) if path.is_file())
    return sum(path.stat().st_size for path in matched), len(matched)


def cdp_up(url: str = CDP_URL, *, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
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
    result = proc.run([python or sys.executable, str(engine), "--ensure-env"],
                      timeout=timeout, env=browser_env())
    for line in reversed((result.stdout + result.stderr).splitlines()):
        if line.startswith("STATUS "):
            return line.strip()
    return "STATUS unknown"


def responses(root: Path) -> set[Path]:
    return set(root.glob(RESPONSE_GLOB))


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
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


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
    target = path.with_name(path.name.replace("response_", "rejected_", 1))
    if target == path:
        target = path.with_name(f"rejected_{path.name}")
    text = path.read_text(encoding="utf-8")
    target.write_text(f"# REJECTED — {reason}\n\n{text}", encoding="utf-8")
    path.unlink()
    return target


def ask(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> str:
    """Pack the project, ask Sol Pro, and return the answer as text."""
    globs = include or project.include
    # Re-checked on every call: a drive iteration can create a secret file
    # between the start of the loop and the next planning request.
    assert_safe_pack(project, root, globs)
    council = command(engine, project, prompt, include=include, python=python, council=True)
    try:
        # One browser: another lane process must not drive it at the same time.
        with locks.exclusive(locks.browser_lock_path()):
            result = proc.run(council, cwd=root, env=browser_env(), timeout=project.max_wait + 300)
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
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        result = proc.run(harvest_command(engine, project, source, max_wait=max_wait, python=python),
                          cwd=root, env=browser_env(), capture=False)
        response = newest_new_response(root, before)
    # A harvest has no prompt to compare against — the conversation is whatever it
    # is — but a refusal page is still not an answer.
    return _verified(result.returncode, response, "")


def run(engine: Path, project: Project, root: Path, prompt: str, *,
        include: tuple[str, ...] | None = None, python: str | None = None) -> ReviewOutcome:
    assert_safe_pack(project, root, include or project.include)
    # Held across the harvest too: otherwise a concurrent run's response file
    # can be picked up as the answer to this prompt.
    with locks.exclusive(locks.browser_lock_path()):
        before = responses(root)
        result = proc.run(command(engine, project, prompt, include=include, python=python),
                          cwd=root, env=browser_env(), capture=False)
        response = newest_new_response(root, before)
    return _verified(result.returncode, response, prompt)


def _verified(returncode: int, response: Path | None, prompt: str) -> ReviewOutcome:
    """A saved file only counts once its contents are an answer to *prompt*."""
    if response is None:
        return ReviewOutcome(returncode=returncode, response=None)
    text = response.read_text(encoding="utf-8")
    # The whole file is checked for a refusal as well as the body: a marker must
    # not become invisible by landing where a header would be.
    reason = rejection_reason(answer_body(text), prompt) or refusal_in(text)
    if reason is None:
        return ReviewOutcome(returncode=returncode, response=response)
    return ReviewOutcome(returncode=returncode, response=None,
                         rejected=reject(response, reason), reason=reason)
