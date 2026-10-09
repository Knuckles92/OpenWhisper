"""The History calendar from the main window: opening it, keeping it current, opening records."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtGui import QKeySequence

from tests.test_history_calendar_dialog import FakeSource, september
from ui_qt.dialogs.history_calendar_dialog import HistoryCalendarDialog
from ui_qt.main_window import MainWindow


@pytest.fixture
def window():
    with patch("ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"), \
            patch.object(HistoryCalendarDialog, "_run", lambda self, work: work()), \
            patch("ui_qt.dialogs.history_calendar_dialog.CalendarSource",
                  side_effect=lambda: FakeSource(september())):
        main = MainWindow()
        yield main
        main._force_quit = True
        main.close()


def test_the_sidebar_and_menu_open_one_calendar(window):
    window.history_sidebar.calendar_btn.click()
    calendar = window._history_calendar
    assert isinstance(calendar, HistoryCalendarDialog)
    assert calendar.isVisible()
    window.open_history_calendar()
    assert window._history_calendar is calendar

    actions = {action.text(): action for menu in window.title_bar.menu_bar.actions()
               if menu.menu() for action in menu.menu().actions()}
    assert actions["History Calendar"].shortcut() == QKeySequence("Ctrl+Shift+D")


def test_past_meetings_header_opens_the_calendar_too(window):
    window.history_sidebar.meetings_content_widget.calendar_btn.click()
    assert window._history_calendar is not None


def test_history_changes_reach_an_open_calendar(window):
    window.open_history_calendar()
    calendar = window._history_calendar
    # The deleted handler resets the status line 2 s later from a bare
    # singleShot lambda, which would fire after this window is gone.
    with patch.object(calendar, "refresh_if_visible") as refresh, \
            patch("ui_qt.main_window.QTimer.singleShot"):
        window.refresh_history()
        window.refresh_past_meetings()
        window._on_history_entry_deleted("x")
    assert refresh.call_count == 3


def test_records_open_above_the_calendar(window):
    window.open_history_calendar()
    calendar = window._history_calendar
    entry = SimpleNamespace(id="entry-1234", stored_on="jed")
    with patch("ui_qt.main_window.HistoryEntryDialog") as entry_dialog:
        calendar.entry_requested.emit(entry)
    entry_dialog.assert_called_once_with(entry, parent=calendar)

    # A host-kept entry is deleted there, whichever list opened it.
    delete = entry_dialog.return_value.delete_requested.connect.call_args.args[0]
    with patch.object(window.history_sidebar, "delete_remote_entry") as remote_delete:
        delete("entry-1234")
    remote_delete.assert_called_once_with("entry-1234")

    meetings = []
    window.past_meeting_requested.connect(meetings.append)
    calendar.meeting_requested.emit("m1")
    assert meetings == ["m1"]


def test_an_edit_in_the_entry_dialog_refreshes_both_lists(window):
    window.open_history_calendar()
    calendar = window._history_calendar
    with patch("ui_qt.main_window.HistoryEntryDialog") as entry_dialog:
        calendar.entry_requested.emit(SimpleNamespace(id="entry-1234"))
    changed = entry_dialog.return_value.version_changed.connect.call_args.args[0]
    with patch.object(window.history_sidebar, "refresh") as sidebar_refresh, \
            patch.object(calendar, "refresh_if_visible") as calendar_refresh:
        changed("entry-1234")
    sidebar_refresh.assert_called_once()
    calendar_refresh.assert_called_once()
