"""Rich-text paste: staging, committing and restoring a transcript with HTML."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import QApplication

from ui_qt import clipboard as clipboard_module
from ui_qt.clipboard import (
    AUTO_PASTE_MARKER_FORMAT,
    ClipboardRestoreOutcome,
    TemporaryClipboard,
    system_html_renderer,
)
from ui_qt.ui_controller import UIController

HTML = "Thanks, <b>Dana Lee</b><br>Product designer"
PLAIN = "Thanks, Dana Lee\nProduct designer"
DOCUMENT = (
    '<html><head><meta charset="utf-8"></head><body>'
    f"<!--StartFragment-->{HTML}<!--EndFragment--></body></html>"
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


class FakeClipboard:
    """Stands in for QClipboard; never the system clipboard."""

    def __init__(self, text=None):
        self._mime_data = QMimeData()
        if text is not None:
            self._mime_data.setText(text)
        self.raise_on_read = False
        self.writes = []

    def mimeData(self):
        if self.raise_on_read:
            raise RuntimeError("clipboard read failed")
        return self._mime_data

    def setMimeData(self, mime_data):
        self.writes.append("mime")
        self._mime_data = mime_data

    def setText(self, text):
        self.writes.append("text")
        mime_data = QMimeData()
        mime_data.setText(text)
        self._mime_data = mime_data


@pytest.fixture
def renders(monkeypatch):
    """Record each pre-render with what the clipboard held at that moment."""
    calls = []
    holder = {}

    def recorder(kind):
        def render():
            mime_data = holder["clipboard"].mimeData()
            calls.append((kind, mime_data.text(), mime_data.html()))
        return render

    monkeypatch.setattr(clipboard_module, "system_text_renderer", lambda: recorder("text"))
    monkeypatch.setattr(clipboard_module, "system_html_renderer", lambda: recorder("html"))

    def install(clipboard):
        holder["clipboard"] = clipboard
        return clipboard

    return SimpleNamespace(calls=calls, install=install)


def test_staged_html_sits_beside_the_text_and_the_ownership_marker():
    clipboard = FakeClipboard("previous text")
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text(PLAIN, html=HTML)

    staged = clipboard.mimeData()
    assert stage.written and stage.lease is not None
    assert staged.text() == PLAIN
    assert staged.html() == DOCUMENT
    assert bytes(staged.data(AUTO_PASTE_MARKER_FORMAT)) == stage.lease.token
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.RESTORED
    assert clipboard.mimeData().text() == "previous text"
    assert not clipboard.mimeData().hasHtml()


def test_a_whole_html_document_is_staged_as_it_is():
    clipboard = FakeClipboard("previous text")
    document = "<html><body><p>Hi</p></body></html>"

    TemporaryClipboard(clipboard).stage_text("Hi", html=document)

    assert clipboard.mimeData().html() == document


def test_without_html_the_stage_is_plain_text_as_before():
    clipboard = FakeClipboard("previous text")

    stage = TemporaryClipboard(clipboard).stage_text("dictated", html="")

    assert clipboard.mimeData().text() == "dictated"
    assert not clipboard.mimeData().hasHtml()
    assert stage.lease.html == ""


@pytest.mark.parametrize("initial", [None, " \n "])
def test_a_blank_clipboard_keeps_the_html(initial):
    clipboard = FakeClipboard(initial)

    stage = TemporaryClipboard(clipboard).stage_text(PLAIN, html=HTML)

    assert stage.written and stage.lease is None
    assert (clipboard.mimeData().text(), clipboard.mimeData().html()) == (PLAIN, DOCUMENT)
    assert not clipboard.mimeData().hasFormat(AUTO_PASTE_MARKER_FORMAT)


def test_an_unreadable_clipboard_still_gets_the_html():
    clipboard = FakeClipboard("previous text")
    clipboard.raise_on_read = True

    stage = TemporaryClipboard(clipboard).stage_text(PLAIN, html=HTML)

    clipboard.raise_on_read = False
    assert stage.written and stage.restore_unavailable
    assert (clipboard.mimeData().text(), clipboard.mimeData().html()) == (PLAIN, DOCUMENT)


def test_a_failed_paste_leaves_text_and_html_without_the_marker():
    clipboard = FakeClipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text(PLAIN, html=HTML)

    assert temporary.commit_text(stage.lease, PLAIN)

    committed = clipboard.mimeData()
    assert (committed.text(), committed.html()) == (PLAIN, DOCUMENT)
    assert not committed.hasFormat(AUTO_PASTE_MARKER_FORMAT)
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.SKIPPED


def test_a_plain_commit_stays_a_plain_text_write():
    clipboard = FakeClipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated")
    clipboard.writes.clear()

    assert temporary.commit_text(stage.lease, "dictated")
    assert clipboard.writes == ["text"]


def test_rich_text_is_rendered_after_it_is_written_and_only_when_staged(renders):
    clipboard = renders.install(FakeClipboard("previous text"))
    temporary = TemporaryClipboard(clipboard)

    first = temporary.stage_text(PLAIN, html=HTML)
    temporary.restore_now(first.lease)
    temporary.stage_text("plain only")

    assert renders.calls == [
        ("text", PLAIN, DOCUMENT),
        ("html", PLAIN, DOCUMENT),
        ("text", "plain only", ""),
    ]


def test_a_failing_rich_text_render_does_not_fail_the_stage(monkeypatch):
    def broken():
        raise OSError("clipboard busy")

    monkeypatch.setattr(clipboard_module, "system_html_renderer", lambda: broken)
    clipboard = FakeClipboard("previous text")

    stage = TemporaryClipboard(clipboard).stage_text(PLAIN, html=HTML)

    assert stage.written and stage.lease is not None


def test_offscreen_platform_has_no_rich_text_renderer():
    assert QApplication.platformName() == "offscreen"
    assert system_html_renderer() is None


def test_the_ui_controller_forwards_html_and_keeps_the_one_argument_call():
    calls = []
    temporary = SimpleNamespace(stage_text=lambda *args, **kwargs: calls.append((args, kwargs)))
    ui = SimpleNamespace(_temporary_clipboard=temporary)

    UIController.stage_transcript_for_paste(ui, "plain")
    UIController.stage_transcript_for_paste(ui, PLAIN, html=HTML)

    assert calls == [(("plain",), {}), ((PLAIN,), {"html": HTML})]
