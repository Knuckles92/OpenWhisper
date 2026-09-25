"""Catch transient startup windows, even if reparenting immediately hides them."""

import pytest
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import SettingsKey, settings_manager
from ui_qt.main_window import MainWindow


@pytest.mark.parametrize("last_tab", [0, 1, 2])
@pytest.mark.parametrize("insights_enabled", [False, True])
@pytest.mark.parametrize("platform_warning", [False, True])
def test_startup_only_shows_the_requested_main_window(
    monkeypatch, last_tab, insights_enabled, platform_warning
):
    settings_manager.update_settings(
        {
            SettingsKey.LAST_TAB_INDEX: last_tab,
            SettingsKey.MEETING_CLOUD_LAST_ENABLED: insights_enabled,
            SettingsKey.MEETING_MODE_INTRO_SEEN: True,
        }
    )
    monkeypatch.setattr(
        "ui_qt.widgets.tabbed_content.meeting_mode_supported", lambda: True
    )
    monkeypatch.setattr(
        "ui_qt.widgets.meeting_mode_tab.meeting_audio_shows_platform_warning",
        lambda: platform_warning,
    )
    app = QApplication.instance()
    shown = []

    class ShowObserver(QObject):
        def eventFilter(self, obj, event):
            if (
                event.type() == QEvent.Type.Show
                and isinstance(obj, QWidget)
                and obj.isWindow()
            ):
                shown.append(obj)
            return False

    observer = ShowObserver()
    app.installEventFilter(observer)
    window = None
    try:
        window = MainWindow()
        app.processEvents()
        # Looking only at topLevelWidgets() after construction misses the
        # flash: the brief panel has already been adopted by its layout.
        assert not shown, [
            (type(widget).__name__, widget.objectName()) for widget in shown
        ]
        assert not window.isVisible()

        window.show()
        app.processEvents()
        assert shown == [window]
        assert window.tabbed_content.current_index() == last_tab
        tab = window.meeting_mode_tab
        assert tab.brief_panel.isVisibleTo(tab) == insights_enabled
        assert tab.platform_hint.isVisibleTo(tab) == platform_warning

        # The ownership fix must preserve normal toggling inside the page.
        tab.cloud_checkbox.setChecked(not insights_enabled)
        app.processEvents()
        assert tab.brief_panel.isVisibleTo(tab) != insights_enabled
        assert shown == [window]
    finally:
        app.removeEventFilter(observer)
        if window is not None:
            window._force_quit = True
            window.close()
