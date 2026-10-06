"""A broken language module is reported, never a silent no-op."""
import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import services
from ui_qt.ui_controller import UIController
from ui_qt.widgets import language_menu


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def broken(monkeypatch):
    """Make ``services.<name>`` fail to import, as a broken dependency would."""
    def install(name):
        monkeypatch.delattr(services, name, raising=False)
        monkeypatch.setitem(sys.modules, f"services.{name}", None)
    return install


def test_a_broken_language_module_is_logged_and_reported(broken, caplog):
    ui = UIController.__new__(UIController)
    ui.overlay = MagicMock()
    ui.set_status = MagicMock()
    broken("dictation_language")

    with caplog.at_level(logging.ERROR, logger=language_menu.__name__):
        ui.cycle_dictation_language()

    ui.set_status.assert_called_once_with(language_menu._FAILED)
    ui.overlay.set_language.assert_not_called()
    assert language_menu._FAILED in caplog.text


def test_the_overlay_chip_never_falls_back_to_raw_codes(broken):
    from ui_qt.overlays.waveform_overlay import WaveformOverlay

    overlay = WaveformOverlay.__new__(WaveformOverlay)
    overlay._language = "de"
    assert (overlay._language_text(), overlay._language_name()) == ("DE", "German")
    broken("dictation_language")
    with pytest.raises(ImportError):
        overlay._language_name()
