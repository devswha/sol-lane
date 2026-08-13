from __future__ import annotations

from pathlib import Path

import pytest

from lane import salvage as salvage_module

URL = "https://chatgpt.com/c/6a7d8344-5f60-83ea-a8c0-3d2c6a08ef07"


class FakeElement:
    def __init__(self, text: str = ""):
        self.text = text
        self.clicks = 0

    def inner_text(self) -> str:
        return self.text

    def click(self) -> None:
        self.clicks += 1


class FakePage:
    """Enough of a playwright page to exercise the reader without a browser."""

    def __init__(self, url: str, *, assistants=(), body: str = "", expander_label: str | None = None,
                 streaming=False):
        self.url = url
        self._assistants = [FakeElement(text) for text in assistants]
        self._body = body
        # A real page carries one locale's toggle, not every locale's.
        self._expander_label = expander_label
        self.expanders = [FakeElement()] if expander_label else []
        self._streaming = streaming

    def query_selector_all(self, selector: str):
        if selector == salvage_module.ANY_TURN:
            return self._assistants or [FakeElement("user turn")]
        if selector == salvage_module.ASSISTANT_SELECTOR:
            return self._assistants
        if self._expander_label and f'"{self._expander_label}"' in selector:
            return self.expanders
        return []

    def query_selector(self, selector: str):
        return object() if selector == salvage_module.STOP_BUTTON and self._streaming else None

    def inner_text(self, _selector: str) -> str:
        return self._body


def test_conversation_id_identifies_the_page_not_the_string():
    assert salvage_module.conversation_id(URL) == "6a7d8344-5f60-83ea-a8c0-3d2c6a08ef07"


def test_a_non_conversation_url_is_refused():
    with pytest.raises(salvage_module.SalvageError, match="not a conversation URL"):
        salvage_module.conversation_id("https://chatgpt.com/")


def test_find_page_matches_on_the_conversation_id():
    wanted = FakePage(URL + "?model=gpt")
    pages = [FakePage("https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444"), wanted]

    assert salvage_module.find_page(pages, URL) is wanted


def test_find_page_reports_nothing_rather_than_failing():
    """Closed is the normal case: the engine closes its page when the run ends."""
    assert salvage_module.find_page([], URL) is None


def test_the_answer_is_preferred_over_the_whole_page():
    page = FakePage(URL, assistants=("the audit says X",), body="sidebar noise\nthe audit says X")

    reading = salvage_module.read_page(page)

    assert reading.body == "the audit says X"
    assert reading.assistant_turns == 1


def test_an_interrupted_turn_falls_back_to_the_page_text():
    """The case this module exists for: zero assistant nodes, reasoning on screen."""
    page = FakePage(URL, assistants=(), body="현재 두 우회를 확인했습니다", expander_label="더 보기")

    reading = salvage_module.read_page(page)

    assert reading.body == "현재 두 우회를 확인했습니다"
    assert reading.assistant_turns == 0
    assert all(element.clicks == 1 for element in page.expanders), "collapsed panels are expanded first"


def test_streaming_is_reported_from_the_stop_button():
    assert salvage_module.read_page(FakePage(URL, body="x", streaming=True)).streaming is True
    assert salvage_module.read_page(FakePage(URL, body="x")).streaming is False


def test_the_file_says_it_is_not_a_harvest(tmp_path: Path):
    reading = salvage_module.Reading(body="partial findings", assistant_turns=0, streaming=False)

    result = salvage_module.write(URL, reading, tmp_path)

    text = result.path.read_text(encoding="utf-8")
    assert result.path.name.startswith("salvaged_"), "a salvage is never a response_*.md"
    assert "UNVERIFIED SALVAGE" in text
    assert "did not certify" in text
    assert URL in text
    assert text.rstrip().endswith("partial findings")
    assert result.chars == len("partial findings")


def test_an_empty_conversation_writes_nothing(tmp_path: Path):
    reading = salvage_module.Reading(body="   \n ", assistant_turns=0, streaming=False)

    with pytest.raises(salvage_module.SalvageError, match="no readable text"):
        salvage_module.write(URL, reading, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_salvage_reads_pages_it_is_handed_without_a_browser(tmp_path: Path):
    pages = [FakePage(URL, assistants=("P1: the gate can be replaced",))]

    result = salvage_module.salvage(URL, tmp_path, pages=pages)

    assert result.assistant_turns == 1
    assert "P1: the gate can be replaced" in result.path.read_text(encoding="utf-8")


class FakeContext:
    """A context that can open a page, like the browser does when the run ended."""

    def __init__(self, page):
        self._page = page
        self.opened = []

    def new_page(self):
        self.opened.append(self._page)
        return self._page


def test_a_closed_conversation_is_opened_and_closed_again(monkeypatch):
    page = FakePage(URL, assistants=("the answer",))
    page.goto_calls = []
    page.closed = False
    page.goto = lambda url, **kwargs: page.goto_calls.append(url)
    page.close = lambda: setattr(page, "closed", True)
    context = FakeContext(page)

    opened = salvage_module.open_conversation(context, URL)

    assert opened is page
    assert page.goto_calls == [URL]


def test_salvage_refuses_a_url_that_is_not_a_conversation(tmp_path: Path):
    with pytest.raises(salvage_module.SalvageError, match="not a conversation URL"):
        salvage_module.salvage("https://chatgpt.com/", tmp_path, pages=[])


def test_salvage_over_handed_pages_still_requires_the_page(tmp_path: Path):
    with pytest.raises(salvage_module.SalvageError, match="not open in the browser"):
        salvage_module.salvage(URL, tmp_path, pages=[])
