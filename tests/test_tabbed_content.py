import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.tabbed_content import TabbedContentWidget


@pytest.fixture
def make_tabs(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "ui_qt.widgets.tabbed_content.meeting_mode_supported", lambda: True
    )
    widgets = []

    def create():
        tabs = TabbedContentWidget()
        for title in ("Quick Record", "Upload File", "Walkthrough", "Meeting Mode"):
            tabs.add_tab(QWidget(), title)
        tabs.sync_stack_with_tab_bar()
        tabs.resize(720, 240)
        tabs.show()
        app.processEvents()
        assert QTest.qWaitForWindowExposed(tabs)
        tabs.activateWindow()
        app.processEvents()
        widgets.append(tabs)
        return tabs

    yield create
    for tabs in widgets:
        tabs.close()
        tabs.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


TABS = list(range(4))
SAVED = TabbedContentWidget.SAVED_TAB_IDS


@pytest.mark.parametrize("selected_tab", TABS)
def test_user_tab_selection_is_saved_and_restored(make_tabs, selected_tab):
    settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, SAVED[(selected_tab + 1) % 4])
    tabs = make_tabs()

    saved = QSignalSpy(tabs._tab_save_timer.timeout)
    QTest.mouseClick(
        tabs.tab_bar,
        Qt.MouseButton.LeftButton,
        pos=tabs.tab_bar.tabRect(selected_tab).center(),
    )
    assert tabs.current_index() == selected_tab
    assert saved or saved.wait(2000)

    assert settings_manager.get(SettingsKey.LAST_TAB_INDEX) == SAVED[selected_tab]
    reopened = make_tabs()
    assert reopened.current_index() == selected_tab
    assert reopened.stack.currentIndex() == selected_tab


@pytest.mark.parametrize("previous_tab", TABS)
@pytest.mark.parametrize("source_tab", TABS)
def test_recording_selects_source_without_visiting_other_tabs(
    make_tabs, previous_tab, source_tab
):
    settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, SAVED[previous_tab])
    tabs = make_tabs()
    changes = []
    tabs.tab_changed.connect(changes.append)

    tabs.set_recording_state(True, source_tab)
    assert changes == ([] if previous_tab == source_tab else [source_tab])
    assert tabs.current_index() == source_tab
    assert tabs.stack.currentIndex() == source_tab
    assert [tabs.tab_bar.isTabEnabled(i) for i in TABS] == [i == source_tab for i in TABS]

    tabs.set_recording_state(False, -1)
    tabs.flush_pending_tab_selection()
    assert tabs.current_index() == source_tab
    assert all(tabs.tab_bar.isTabEnabled(i) for i in TABS)
    assert settings_manager.get(SettingsKey.LAST_TAB_INDEX) == SAVED[source_tab]


@pytest.mark.parametrize(
    ("saved", "tab"),
    [
        (0, TabbedContentWidget.TAB_QUICK_RECORD),
        (1, TabbedContentWidget.TAB_UPLOAD_FILE),
        (2, TabbedContentWidget.TAB_MEETING_MODE),
        (3, TabbedContentWidget.TAB_WALKTHROUGH),
    ],
)
def test_a_tab_saved_before_walkthrough_joined_still_opens(make_tabs, saved, tab):
    """Meeting Mode was saved as 2 when it was the third tab; it is fourth now."""
    settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, saved)
    assert make_tabs().current_index() == tab


def test_meeting_mode_stays_last_for_the_themes_last_tab_rules(make_tabs):
    tabs = make_tabs()
    assert TabbedContentWidget.TAB_MEETING_MODE == tabs.tab_bar.count() - 1
    assert tabs.tab_bar.tabText(TabbedContentWidget.TAB_MEETING_MODE) == "Meeting Mode"
    assert tabs.tab_bar.tabText(TabbedContentWidget.TAB_WALKTHROUGH) == "Walkthrough"
