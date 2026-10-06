"""An open Scratchpad never stops OpenWhisper from quitting, and keeps its notes."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtWidgets import QApplication, QWidget

from config import config
from ui_qt.widgets import scratchpad
from ui_qt.widgets.scratchpad import ScratchpadWindow


class _MainWindow(QWidget):
    """Stands in for MainWindow after quit_application set _force_quit."""

    def closeEvent(self, event):
        event.accept()


@pytest.fixture
def ui():
    controller = SimpleNamespace(copy_to_clipboard=MagicMock(return_value=True))
    yield controller
    pad = getattr(controller, "_scratchpad", None)
    if pad is not None:
        pad.hide()
        pad.deleteLater()


def _saved_text():
    return Path(config.SCRATCHPAD_FILE).read_text(encoding="utf-8")


def test_closing_is_accepted_and_keeps_the_notes(ui):
    scratchpad.toggle(ui)
    pad = ui._scratchpad
    pad.editor.insertPlainText("Call the plumber")
    assert pad._autosave_timer.isActive()

    # QApplication::closeAllWindows stops at the first close() that returns False.
    assert pad.close() is True
    assert not pad.isVisible()
    assert _saved_text() == "Call the plumber"

    pad.present()
    assert pad.isVisible()
    assert pad.text() == "Call the plumber"


def test_closing_the_last_visible_window_does_not_quit(ui):
    """With the main window in the tray, Alt+F4 or the compositor's close only hides."""
    app = QApplication.instance()
    assert app.quitOnLastWindowClosed()
    scratchpad.toggle(ui)
    pad = ui._scratchpad
    QApplication.processEvents()

    close_timer = QTimer()
    close_timer.setSingleShot(True)
    close_timer.timeout.connect(pad.close)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.exit(5))
    close_timer.start(0)
    watchdog.start(300)
    assert app.exec() == 5
    assert not pad.isVisible()


def test_quit_is_not_vetoed_by_an_open_scratchpad():
    app = QApplication.instance()
    outcomes = []
    # closeAllWindows walks top-level widgets in pointer-hash order, so build
    # fresh windows in alternating order to put the Scratchpad first in some runs.
    for attempt in range(12):
        if attempt % 2:
            pad, main = ScratchpadWindow(), _MainWindow()
        else:
            main, pad = _MainWindow(), ScratchpadWindow()
        main.show()
        pad.present()
        pad.editor.setPlainText(f"note {attempt}")
        QApplication.processEvents()

        quit_timer = QTimer()
        quit_timer.setSingleShot(True)
        quit_timer.timeout.connect(QApplication.quit)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: app.exit(7))
        quit_timer.start(0)
        watchdog.start(1000)
        code = app.exec()
        watchdog.stop()

        outcomes.append((code, main.isVisible(), pad.isVisible(), _saved_text() == f"note {attempt}"))
        main.deleteLater()
        pad.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert outcomes == [(0, False, False, True)] * 12
