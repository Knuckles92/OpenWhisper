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
from ui_qt.utils.font_scale import apply_ui_font_scale
from ui_qt.utils.theme_manager import ThemeManager
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


def test_the_walkthrough_tab_keeps_its_size_as_its_settings_change(window):
    page = _open(window, TabbedContentWidget.TAB_WALKTHROUGH)
    page.write_up_switch.setChecked(False)
    _settle()
    size = window.size()

    page.write_up_switch.setChecked(True)
    page.auto_switch.setChecked(True)
    _settle()
    assert window.size() == size
    assert page.scroll_area.verticalScrollBar().maximum() == 0

    page.write_up_switch.setChecked(False)
    _settle()
    assert window.size() == size


def test_a_dragged_walkthrough_height_stays(window):
    """Its stage takes the height it is given, as an open transcript does."""
    page = _open(window, TabbedContentWidget.TAB_WALKTHROUGH)
    fitted = window.height()

    window.resize(window.width(), fitted + 120)
    _settle()
    page.write_up_switch.setChecked(not page.write_up_switch.isChecked())
    _settle()
    assert window.height() == fitted + 120
    assert page.stage.height() > 0


def test_an_open_transcript_keeps_the_height_the_user_gives_it(window):
    page = _open(window, TabbedContentWidget.TAB_QUICK_RECORD)
    collapsed = window.height()

    page.expand_transcription()
    _settle()
    assert window.height() > collapsed
    start = window.geometry().bottomLeft()
    window._begin_resize((0, 1), start)
    window._apply_resize_delta(start + QPoint(0, 900 - window.height()))
    window._finish_resize()
    _settle()
    assert window.height() == 900

    _open(window, TabbedContentWidget.TAB_UPLOAD_FILE)
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


def test_dragging_an_edge_of_a_maximized_window_leaves_maximized(window):
    """The platform places a maximized window, so a drag there fought it."""
    window.title_bar._toggle_maximize()
    _settle()
    assert window.isMaximized()

    geometry = window.geometry()
    start = geometry.topRight()
    window._begin_resize((1, 0), start)
    assert not window.isMaximized()
    assert not window.title_bar._is_maximized
    assert window.title_bar.maximize_btn.glyph == "maximize"
    assert window.geometry() == geometry

    window._apply_resize_delta(start - QPoint(100, 0))
    window._finish_resize()
    assert window.geometry().topLeft() == geometry.topLeft()
    assert window.width() == max(window.minimumWidth(), geometry.width() - 100)


def test_restoring_from_maximized_leaves_the_maximized_state(window):
    normal = window.geometry()
    window.title_bar._toggle_maximize()
    _settle()
    window.title_bar._toggle_maximize()
    _settle()

    assert not window.isMaximized()
    assert window.geometry().topLeft() == normal.topLeft()
    assert window.width() == normal.width()


def test_the_left_edge_stops_at_the_width_cap_without_sliding(window):
    right = window.geometry().right()
    start = window.geometry().topLeft()
    window._begin_resize((-1, 0), start)
    window._apply_resize_delta(start - QPoint(5000, 0))
    window._finish_resize()

    assert window.width() == window.maximumWidth()
    assert window.geometry().right() == right


def test_quick_record_fits_wrapped_text_and_shrinks_after_a_long_result(
    window, isolated_app_theme,
):
    apply_ui_font_scale(100, theme_manager=ThemeManager())
    page = window.quick_record_tab
    editor = page.transcript_text
    short = "A short recording."
    page.set_transcript(short)
    page.expand_transcription()
    page.set_transcription_stats(0.3, 3, 100_000)
    _settle()
    short_height = window.height()
    short_editor_height = editor.height()

    assert editor.verticalScrollBar().maximum() == 0
    assert editor.viewport().height() <= editor.document().size().height() + 1
    assert page.scroll_area.verticalScrollBar().maximum() == 0

    page.set_transcript("A longer result with enough text to wrap. " * 11)
    _settle()
    assert editor.height() > short_editor_height
    assert editor.verticalScrollBar().maximum() == 0
    assert editor.viewport().height() <= editor.document().size().height() + 1

    page.set_transcript("A much longer transcript. " * 250)
    _settle()
    assert window.height() > short_height
    assert editor.verticalScrollBar().maximum() > 0
    assert page.scroll_area.verticalScrollBar().maximum() == 0

    page.set_transcript(short)
    _settle()
    assert window.height() == short_height
    assert editor.height() == short_editor_height
    assert editor.verticalScrollBar().maximum() == 0
