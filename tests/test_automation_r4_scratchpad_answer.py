"""Review round 4, F5: an answer sent to the Scratchpad during a transform survives it.

A program's run, and the user's Scratchpad-output actions, add their answer
at the end of the Scratchpad (show_text) whenever it arrives. A transform
under way replaces only the text it read, so an answer added at either edge
of it meanwhile is kept.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication

from services.text_transforms import Transform
from ui_qt.widgets import scratchpad

SHOUT = Transform("shout", "Shout", "Uppercase it.")


class HeldRewrite:
    """text_rewrite.rewrite_standalone, uppercasing once released."""

    def __init__(self):
        self.release = threading.Event()
        self.read = []

    def __call__(self, text, instruction, settings):
        self.read.append(text)
        self.release.wait(5)
        return text.upper(), ""


@pytest.fixture
def pad(monkeypatch):
    rewrite = HeldRewrite()
    monkeypatch.setattr(scratchpad.text_rewrite, "rewrite_standalone", rewrite)
    ui = SimpleNamespace(copy_to_clipboard=MagicMock(return_value=True))
    scratchpad.toggle(ui)
    window = ui._scratchpad
    window.ui, window.rewrite = ui, rewrite
    yield window
    rewrite.release.set()
    window.hide()
    window.deleteLater()


def _finish(pad, timeout=3.0):
    pad.rewrite.release.set()
    deadline = time.monotonic() + timeout
    while pad.editor.isReadOnly() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not pad.editor.isReadOnly()


def _select(pad, start, end):
    cursor = pad.editor.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    pad.editor.setTextCursor(cursor)


def test_an_answer_added_during_a_whole_text_transform_is_kept(pad):
    pad.editor.setPlainText("my notes")

    pad.apply_transform(SHOUT)
    scratchpad.show_text(pad.ui, "the program's answer")
    _finish(pad)

    assert pad.rewrite.read == ["my notes"]
    assert pad.text() == "MY NOTES\n\nthe program's answer"
    # The caret lands after the transformed text, as before.
    assert pad.editor.textCursor().position() == len("MY NOTES")


def test_an_answer_added_after_a_selection_ending_the_text_is_kept(pad):
    pad.editor.setPlainText("keep this. shout this")
    _select(pad, len("keep this. shout this"), len("keep this. "))  # selected backwards

    pad.apply_transform(SHOUT)
    scratchpad.show_text(pad.ui, "an answer")
    _finish(pad)

    assert pad.rewrite.read == ["shout this"]
    assert pad.text() == "keep this. SHOUT THIS\n\nan answer"


def test_text_added_just_before_a_selection_is_kept(pad):
    pad.editor.setPlainText("one two three")
    _select(pad, len("one "), len("one two"))

    pad.apply_transform(SHOUT)
    cursor = QTextCursor(pad.editor.document())
    cursor.setPosition(len("one "))
    cursor.insertText("and ")
    _finish(pad)

    assert pad.text() == "one and TWO three"
