"""An action's answer in the Scratchpad: kept with the saved notes, and no focus stolen."""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from tests.fakes.settings import InMemorySettings


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pad_ui(qapp, tmp_path, monkeypatch):
    from config import config
    from ui_qt.widgets import scratchpad

    notes = tmp_path / "scratchpad.txt"
    notes.write_text("my saved notes from yesterday", encoding="utf-8")
    monkeypatch.setattr(config, "SCRATCHPAD_FILE", str(notes))
    monkeypatch.setattr(scratchpad, "settings_manager", InMemorySettings({}))
    activations = []
    monkeypatch.setattr(scratchpad.ScratchpadWindow, "activateWindow",
                        lambda self: activations.append(self))
    # Whether each show asked not to activate: Qt reads the attribute then.
    quiet_shows = []
    real_show = scratchpad.ScratchpadWindow.show

    def show(self):
        quiet_shows.append(self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        real_show(self)

    monkeypatch.setattr(scratchpad.ScratchpadWindow, "show", show)
    ui = SimpleNamespace(copy_to_clipboard=lambda text, **kw: True)
    yield SimpleNamespace(ui=ui, notes=notes, activations=activations,
                          quiet_shows=quiet_shows, module=scratchpad)
    pad = getattr(ui, "_scratchpad", None)
    if pad is not None:
        pad._autosave_timer.stop()
        pad.hide()
        pad.deleteLater()


def test_the_first_answer_of_a_session_survives_loading_the_saved_notes(pad_ui):
    pad_ui.module.show_text(pad_ui.ui, "The capital of Peru is Lima.")
    pad = pad_ui.ui._scratchpad

    assert pad.text() == "my saved notes from yesterday\n\nThe capital of Peru is Lima."
    pad.flush()
    pad._saver.write_now(pad.text())
    assert "Lima" in pad_ui.notes.read_text(encoding="utf-8")


def test_an_answer_shows_the_pad_without_taking_focus(pad_ui):
    pad_ui.module.show_text(pad_ui.ui, "Lima")
    pad = pad_ui.ui._scratchpad

    assert pad.isVisible()
    assert pad_ui.activations == []
    assert pad_ui.quiet_shows == [True]


def test_opening_the_pad_after_an_answer_shows_it_normally(pad_ui):
    pad_ui.module.show_text(pad_ui.ui, "Lima")
    pad = pad_ui.ui._scratchpad
    pad.hide()
    pad_ui.module.toggle(pad_ui.ui)

    assert pad_ui.quiet_shows == [True, False]
    assert pad_ui.activations == [pad]


def test_opening_the_pad_still_focuses_it(pad_ui):
    pad_ui.module.toggle(pad_ui.ui)
    pad = pad_ui.ui._scratchpad

    assert pad.isVisible() and pad_ui.activations == [pad]
    pad_ui.module.show_text(pad_ui.ui, "Lima")
    assert pad.text().endswith("\n\nLima")
    assert pad_ui.activations == [pad]
