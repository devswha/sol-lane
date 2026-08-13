"""Read what a conversation actually contains, when the engine will not.

`lane harvest` asks the engine for a *verified* answer and fails closed: an
interrupted turn yields nothing, because a partial answer must never be filed as
a complete one. That is the right default and it stays.

It is not the whole story. Twice on 2026-08-13 a Pro turn stopped itself after
half an hour with no assistant message, while its reasoning panel still held the
two findings that produced the next two commits. The text was on screen; only the
lane had no way to take it.

So this module takes it, and marks it for what it is: unverified salvage, never a
response file. The distinction is in the filename and in the header of every file
written here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .review import CONVERSATION_RE

CDP_ENDPOINT = "http://127.0.0.1:9222"
# The engine closes its page in a finally block, so by the time anyone wants a
# salvage the conversation is almost never still open. Opening it is the tool.
OPEN_TIMEOUT_MS = 60000
SETTLE_SECONDS = 2.0
TURN_POLL_SECONDS = 1.0
TURN_POLL_TRIES = 30
ANY_TURN = "[data-message-author-role]"
# Collapsed thinking panels hold the reasoning; both locales ship a toggle.
EXPAND_LABELS = ("더 보기", "Show more")
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
STOP_BUTTON = 'button[data-testid="stop-button"]'
EXPAND_SETTLE_SECONDS = 0.3


class SalvageError(Exception):
    """Nothing could be read from that conversation. Maps to exit code 1."""


@dataclass(frozen=True)
class Reading:
    body: str
    assistant_turns: int
    streaming: bool


@dataclass(frozen=True)
class Salvaged:
    path: Path
    chars: int
    assistant_turns: int
    streaming: bool


def conversation_id(url: str) -> str:
    """The `/c/<id>` part, which identifies a page across reloads and titles."""
    match = CONVERSATION_RE.search(url)
    if match is None:
        raise SalvageError(f"not a conversation URL: {url}")
    return match.group(0).removeprefix("/c/")


def find_page(pages, url: str):
    """The open page showing *url*, or None when nothing has it open."""
    wanted = conversation_id(url)
    for page in pages:
        if wanted in getattr(page, "url", ""):
            return page
    return None


def open_conversation(context, url: str):
    """Open *url* in a new tab and wait for its turns to render."""
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=OPEN_TIMEOUT_MS)
    for _ in range(TURN_POLL_TRIES):
        if page.query_selector_all(ANY_TURN):
            break
        time.sleep(TURN_POLL_SECONDS)
    time.sleep(SETTLE_SECONDS)  # the last turn streams in after the first paint
    return page


def read_page(page) -> Reading:
    """Expand the collapsed panels, then take the text as it stands.

    Assistant turns are preferred; when there are none — the interrupted case —
    the whole page is taken, because that is where the reasoning narration lives.
    """
    for label in EXPAND_LABELS:
        for element in page.query_selector_all(f'button:has-text("{label}")'):
            try:
                element.click()
                time.sleep(EXPAND_SETTLE_SECONDS)
            except Exception:  # noqa: BLE001 - live page, best effort
                continue
    assistants = page.query_selector_all(ASSISTANT_SELECTOR)
    answers = "\n\n".join((element.inner_text() or "").strip() for element in assistants)
    body = answers.strip() or page.inner_text("body")
    return Reading(body=body, assistant_turns=len(assistants),
                   streaming=bool(page.query_selector(STOP_BUTTON)))


def render(url: str, reading: Reading) -> str:
    """The salvage file. Its header is the contract: this is not a harvest."""
    state = "still streaming" if reading.streaming else "not streaming"
    return (
        "# UNVERIFIED SALVAGE — not a harvested answer\n\n"
        f"- conversation: {url}\n"
        f"- read at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- assistant turns in the DOM: {reading.assistant_turns} ({state})\n"
        "- the engine did not certify this text; it may be partial, mid-stream, or\n"
        "  reasoning narration rather than an answer.\n\n"
        f"---\n\n{reading.body.strip()}\n"
    )


def write(url: str, reading: Reading, out_dir: Path) -> Salvaged:
    if not reading.body.strip():
        raise SalvageError(f"conversation {conversation_id(url)} holds no readable text")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"salvaged_{time.strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(render(url, reading), encoding="utf-8")
    return Salvaged(path=path, chars=len(reading.body.strip()),
                    assistant_turns=reading.assistant_turns, streaming=reading.streaming)


def salvage(url: str, out_dir: Path, *, pages=None, endpoint: str = CDP_ENDPOINT,
            reader=read_page) -> Salvaged:
    """Salvage *url* into *out_dir*, opening the conversation if it is closed.

    ``pages`` exists because playwright's pages are only usable inside the
    session that opened them: a caller (or a test) that already holds pages
    passes them in, everyone else gets a CDP connection opened here.
    """
    conversation_id(url)  # reject a non-conversation before touching the browser
    if pages is not None:
        page = find_page(pages, url)
        if page is None:
            raise SalvageError(f"conversation {conversation_id(url)} is not open in the browser")
        return write(url, reader(page), out_dir)

    from playwright.sync_api import sync_playwright  # CDP is optional at import time

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        contexts = browser.contexts
        if not contexts:
            raise SalvageError(f"no browser context on {endpoint}")
        page = find_page([page for context in contexts for page in context.pages], url)
        opened = page is None
        if opened:
            page = open_conversation(contexts[0], url)
        try:
            reading = reader(page)
        finally:
            if opened:
                page.close()
    return write(url, reading, out_dir)
