"""The main window fits its height to whichever tab is open."""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from config import config
from services.settings import settings_manager
from ui_qt.main_window import MainWindow
from ui_qt.widgets.tabbed_content import TabbedContentWidget

TABS = (
    TabbedContentWidget.TAB_QUICK_RECORD,
    TabbedContentWidget.TAB_UPLOAD_FILE,
    TabbedContentWidget.TAB_WALKTHROUGH,
    TabbedContentWidget.TAB_MEETING_MODE,
)


@pytest.fixture
def window(_session_qt_application):
    with patch.object(settings_manager, "load_all_settings", return_value={}), \
            patch.object(settings_manager, "get", side_effect=lambda key, default=None: default), \
            patch.object(settings_manager, "save_setting"), \
            patch("ui_qt.widgets.tabbed_content.meeting_mode_supported", return_value=True), \
            patch("ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"):
        window = MainWindow()
        window._available_screen_rect = lambda: QRect(0, 0, 1920, 1080)
        window._max_usable_height = lambda: 1080
        window.show()
        _settle()
        yield window
        window._force_quit = True
        window.close()
        QApplication.processEvents()


def _settle():
    # Two window animations and the transcript card's own can run back to back.
    QTest.qWait(800)
    QApplication.processEvents()


def _open(window, tab):
    window.tabbed_content.set_current_index(tab)
    _settle()
    return window.tabbed_content.stack.currentWidget()


def _blank_below(page) -> int:
    """Empty height between the page's lowest visible control and its bottom."""
    viewport = page.scroll_area.viewport()
    lowest = max(
        child.mapTo(viewport, QPoint(0, child.height())).y()
        for child in page.scroll_area.widget().findChildren(QWidget)
        if child.isVisible() and not child.findChildren(QWidget)
    )
    return viewport.height() - lowest


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_fits_the_window_to_its_page(window, tab):
    page = _open(window, tab)

    assert page.scroll_area.verticalScrollBar().maximum() == 0
    # Only the card padding and page margin are left below the last control.
    assert _blank_below(page) < 48
    # A short page lets the window go no shorter than the page itself.
    assert window.minimumHeight() == min(window.height(), config.MAIN_WINDOW_MIN_HEIGHT)


def test_showing_the_window_again_fits_it_at_once(window):
    """A tall saved height (an open transcript's) is not kept as blank space."""
    fitted = window.height()
    window.hide()
    window.resize(window.width(), 900)
    window.show()
    QApplication.processEvents()

    assert window.height() == fitted


def test_the_walkthrough_tab_follows_its_prompt_coming_and_going(window):
    page = _open(window, TabbedContentWidget.TAB_WALKTHROUGH)
    page.write_up_switch.setChecked(False)
    _settle()
    without_prompt = window.height()

    page.write_up_switch.setChecked(True)
    _settle()
    assert window.height() > without_prompt
    assert page.scroll_area.verticalScrollBar().maximum() == 0

    page.write_up_switch.setChecked(False)
    _settle()
    assert window.height() == without_prompt


def test_a_dragged_height_stays_until_the_page_changes(window):
    page = _open(window, TabbedContentWidget.TAB_WALKTHROUGH)
    page.write_up_switch.setChecked(False)
    _settle()
    fitted = window.height()

    window.resize(window.width(), fitted + 120)
    _settle()
    assert window.height() == fitted + 120

    page.write_up_switch.setChecked(True)
    _settle()
    page.write_up_switch.setChecked(False)
    _settle()
    assert window.height() == fitted


def test_an_open_transcript_keeps_the_height_the_user_gives_it(window):
    page = _open(window, TabbedContentWidget.TAB_QUICK_RECORD)
    collapsed = window.height()

    page.expand_transcription()
    _settle()
    assert window.height() > collapsed
    window.resize(window.width(), 900)
    _settle()
    assert window.height() == 900

    _open(window, TabbedContentWidget.TAB_WALKTHROUGH)
    assert window.height() < 900
    _open(window, TabbedContentWidget.TAB_QUICK_RECORD)
    assert window.height() == 900

    page.transcription_card.set_collapsed(True)
    _settle()
    assert window.height() == collapsed
    assert _blank_below(page) < 48

    page.expand_transcription()
    _settle()
    assert window.height() == 900
