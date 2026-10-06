"""Scratchpad Transform through the real text_rewrite.rewrite_standalone.

Only the network client and the provider check are faked, so these pin the
(text, error) contract between the Scratchpad and the rewrite module.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication

from services import text_rewrite
from services.text_transforms import Transform
from ui_qt.widgets import scratchpad

POLISH = Transform("polish", "Polish", "Improve the flow.")


class FakeCleaner:
    """TranscriptCleanup's contract: cleanup() hands its input back on failure."""

    reply = "Polished words here."
    error = None

    def __init__(self, **kwargs):
        self.last_error = "not run"

    def configure(self, provider, model, reasoning=None):
        pass

    def is_available(self):
        return True

    def cleanup(self, text, system_prompt=None, timeout_s=None):
        self.last_error = self.error
        return text if self.error else self.reply


@pytest.fixture
def pad(monkeypatch):
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda settings: True)
    monkeypatch.setattr("services.transcript_cleanup.TranscriptCleanup", FakeCleaner)
    ui = SimpleNamespace(copy_to_clipboard=MagicMock(return_value=True))
    scratchpad.toggle(ui)
    window = ui._scratchpad
    yield window
    window.hide()
    window.deleteLater()


def _wait(pad, timeout=3.0):
    deadline = time.monotonic() + timeout
    while pad.editor.isReadOnly() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not pad.editor.isReadOnly()


def test_a_successful_rewrite_replaces_the_text(pad):
    pad.editor.setPlainText("rough words here")

    pad.apply_transform(POLISH)
    _wait(pad)

    assert pad.text() == "Polished words here."
    assert pad.status_label.text().startswith("Polish applied")


def test_a_provider_error_is_shown_and_the_text_kept(pad, monkeypatch):
    monkeypatch.setattr(FakeCleaner, "error", "HTTP 500 server error")
    pad.editor.setPlainText("rough words here")

    pad.apply_transform(POLISH)
    _wait(pad)

    assert pad.text() == "rough words here"
    assert pad.status_label.text() == "The AI model failed: HTTP 500 server error"


def test_without_a_provider_it_says_how_to_set_one_up(pad, monkeypatch):
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda settings: False)
    pad.editor.setPlainText("rough words here")

    pad.apply_transform(POLISH)
    _wait(pad)

    assert pad.text() == "rough words here"
    assert pad.status_label.text() == text_rewrite.NO_PROVIDER_MESSAGE
