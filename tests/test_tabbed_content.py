import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
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
        for title in ("Quick Record", "Upload File", "Meeting Mode"):
            tabs.add_tab(QWidget(), title)
        tabs.sync_stack_with_tab_bar()
        tabs.resize(720, 240)
        tabs.show()
        app.processEvents()
        widgets.append(tabs)
        return tabs

    yield create
    for tabs in widgets:
        tabs.close()
        tabs.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("selected_tab", [0, 1, 2])
def test_user_tab_selection_is_saved_and_restored(make_tabs, selected_tab):
    settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, (selected_tab + 1) % 3)
    tabs = make_tabs()

    QTest.mouseClick(
        tabs.tab_bar,
        Qt.MouseButton.LeftButton,
        pos=tabs.tab_bar.tabRect(selected_tab).center(),
    )
    QTest.qWait(300)

    assert settings_manager.get(SettingsKey.LAST_TAB_INDEX) == selected_tab
    reopened = make_tabs()
    assert reopened.current_index() == selected_tab
    assert reopened.stack.currentIndex() == selected_tab


@pytest.mark.parametrize("previous_tab", [0, 1, 2])
@pytest.mark.parametrize("source_tab", [0, 1, 2])
def test_recording_selects_source_without_visiting_other_tabs(
    make_tabs, previous_tab, source_tab
):
    settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, previous_tab)
    tabs = make_tabs()
    changes = []
    tabs.tab_changed.connect(changes.append)

    tabs.set_recording_state(True, source_tab)
    assert changes == ([] if previous_tab == source_tab else [source_tab])
    assert tabs.current_index() == source_tab
    assert tabs.stack.currentIndex() == source_tab
    assert [tabs.tab_bar.isTabEnabled(i) for i in range(3)] == [
        i == source_tab for i in range(3)
    ]

    tabs.set_recording_state(False, -1)
    tabs.flush_pending_tab_selection()
    assert tabs.current_index() == source_tab
    assert all(tabs.tab_bar.isTabEnabled(i) for i in range(3))
    assert settings_manager.get(SettingsKey.LAST_TAB_INDEX) == source_tab
