"""Settings → API & MCP: the live API, MCP Events and Claude Code channel cards."""

import json
import os
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QScrollArea, QVBoxLayout, QWidget

from services.agent_mcp import events_ext, setup
from services.agent_mcp.runtime import DEFAULT_PORT, ServerStatus
from services.automation import events
from services.remote_asr.client import RemoteRequestError
from services.settings import SettingsKey, SettingsManager, SettingsView
from tests.test_mcp_host_tabs import FakeRemoteService, make_view, tick
from tests.test_mcp_settings_page import TOKEN, FakeServer
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_mcp
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import MCP
from ui_qt.widgets.engine_field import EngineStatus

PERMISSIONS = (
    SettingsKey.API_LIVE_EVENTS,
    SettingsKey.API_RUN_ACTIONS,
    SettingsKey.API_CONTROL_RECORDING,
    SettingsKey.API_TEXT_FILTERS,
)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def bus():
    """A private event bus, so listener counts are this test's own."""
    replacement = events.EventBus()
    previous = events.set_bus(replacement)
    yield replacement
    replacement.close()
    events.set_bus(previous)


@pytest.fixture(autouse=True)
def no_subscriptions(monkeypatch):
    """MCP Events with nothing saved; tests that need subscriptions patch over this."""
    monkeypatch.setattr(events_ext, "subscription_count", lambda: 0)
    monkeypatch.setattr(events_ext, "clear_subscriptions", lambda: 0)


@pytest.fixture
def view(tmp_path):
    view = make_view(tmp_path)
    yield view
    view.close()
    view.deleteLater()
    QApplication.processEvents()


def running(view, port=DEFAULT_PORT):
    view.local.server.current = ServerStatus("running", "Ready", port)
    view.live.refresh_live()


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QTest.qWait(10)


class TestLiveApi:
    def test_permissions_default_to_reading_only_and_persist(self, view):
        live = view.live
        checks = live.api_permission_checks
        assert list(checks) == list(PERMISSIONS)
        assert checks[SettingsKey.API_LIVE_EVENTS].isChecked()
        assert not any(checks[key].isChecked() for key in PERMISSIONS[1:])
        for key in PERMISSIONS[1:]:
            checks[key].click()
            assert view.settings.get(key) is True
        checks[SettingsKey.API_LIVE_EVENTS].click()
        assert view.settings.get(SettingsKey.API_LIVE_EVENTS) is False
        assert not live.notice.isVisible()

    def test_clicking_a_permission_row_toggles_it(self, view):
        from PyQt6.QtCore import Qt

        check = view.live.api_permission_checks[SettingsKey.API_CONTROL_RECORDING]
        row = check.parentWidget()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
        assert view.settings.get(SettingsKey.API_CONTROL_RECORDING) is True

    def test_a_failed_save_says_so_and_shows_what_is_saved(self, view, monkeypatch):
        def fail(_mutator):
            raise OSError("read only")

        monkeypatch.setattr(view.settings, "mutate_settings", fail)
        check = view.live.api_permission_checks[SettingsKey.API_TEXT_FILTERS]
        check.click()
        assert not check.isChecked()
        assert view.live.notice.text() == "Couldn't save that permission. Try again."

    def test_saved_choices_are_reread_when_shown(self, view):
        view.settings.save_setting(SettingsKey.API_RUN_ACTIONS, True)
        view.live.refresh()
        assert view.live.api_permission_checks[SettingsKey.API_RUN_ACTIONS].isChecked()

    def test_address_and_endpoints_need_mcp_running(self, view, monkeypatch):
        live = view.live
        assert not live.api_url.isEnabled()
        assert "Turn on MCP" in live.api_url.text()
        assert not any(button.isEnabled() for button in live.endpoint_buttons)
        assert live.listeners_label.text() == "Off while MCP is off"
        running(view, 9123)
        assert live.api_url.isEnabled() and live.api_url.text() == "http://127.0.0.1:9123"
        live.copy_api_url.click()
        assert QApplication.clipboard().text() == "http://127.0.0.1:9123"
        paths = [path for _method, path, _what in settings_mcp.LIVE_ENDPOINTS]
        assert {"/v1/events", "/v1/actions", "/v1/recording/start", "/v1/filters"} <= set(paths)
        live.endpoint_buttons[paths.index("/v1/events")].click()
        assert QApplication.clipboard().text() == "http://127.0.0.1:9123/v1/events"
        live.copy_example.click()
        example = QApplication.clipboard().text()
        assert example == f'curl -N -H "Authorization: Bearer {TOKEN}" http://127.0.0.1:9123/v1/events'
        # The token is copied, never shown.
        assert TOKEN not in live.api_url.text()
        assert all(TOKEN not in button.text() for button in live.endpoint_buttons)

    def test_listening_programs_are_counted_live_from_the_page_timer(self, view, bus):
        live = view.live
        running(view)
        assert live.listeners_label.text() == "No programs listening yet"
        assert live.listeners_dot.status() == EngineStatus.UNKNOWN
        first = bus.subscribe(lambda event: None, label=settings_mcp.SSE_LISTENER_LABEL)
        bus.subscribe(lambda event: None, label=settings_mcp.SSE_LISTENER_LABEL)
        bus.subscribe(lambda event: None, label="webhooks")  # OpenWhisper's own delivery
        view.local.timer.timeout.emit()
        assert live.listeners_label.text() == "2 programs listening"
        assert live.listeners_dot.status() == EngineStatus.READY
        first.close()
        view.local.timer.timeout.emit()
        assert live.listeners_label.text() == "1 program listening"


class TestMcpEvents:
    def test_switch_persists_and_tells_the_app(self, view):
        changed = []
        view.settings_changed.connect(changed.append)
        switch = view.live.mcp_events_switch
        assert not switch.isChecked()
        assert view.live.subscriptions_label.text() == "Off. Agents can't subscribe."
        switch.click()
        assert view.settings.get(SettingsKey.MCP_EVENTS_ENABLED) is True
        assert changed == ["mcp_events"]
        assert view.live.subscriptions_label.text() == "No subscriptions yet"
        switch.click()
        assert view.settings.get(SettingsKey.MCP_EVENTS_ENABLED) is False
        assert changed == ["mcp_events", "mcp_events"]

    def test_a_failed_save_does_not_tell_the_app(self, view, monkeypatch):
        changed = []
        view.settings_changed.connect(changed.append)

        def fail(_mutator):
            raise OSError("read only")

        monkeypatch.setattr(view.settings, "mutate_settings", fail)
        view.live.mcp_events_switch.click()
        assert not view.live.mcp_events_switch.isChecked()
        assert changed == []
        assert "Couldn't save MCP Events" in view.live.notice.text()

    def test_subscription_count_and_clear(self, view, monkeypatch):
        count = {"value": 3}
        cleared = []
        monkeypatch.setattr(events_ext, "subscription_count", lambda: count["value"])

        def clear():
            cleared.append(True)
            gone, count["value"] = count["value"], 0
            return gone

        monkeypatch.setattr(events_ext, "clear_subscriptions", clear)
        live = view.live
        live.refresh_live()
        assert live.subscriptions_label.text() == "3 subscriptions"
        assert not live.clear_subscriptions.isHidden()
        live.clear_subscriptions.click()
        assert not live.clear_subscriptions.isEnabled()
        wait_for(lambda: live.notice.text() == "Cleared 3 subscriptions.")
        assert cleared == [True]
        assert live.clear_subscriptions.isHidden()

    def test_clear_errors_are_reported(self, view, monkeypatch):
        monkeypatch.setattr(events_ext, "subscription_count", lambda: 1)

        def clear():
            raise RuntimeError("store locked")

        monkeypatch.setattr(events_ext, "clear_subscriptions", clear)
        view.live.refresh_live()
        view.live.clear_subscriptions.click()
        wait_for(lambda: "store locked" in view.live.notice.text())
        assert view.live.clear_subscriptions.isEnabled()


class TestChannel:
    def test_setup_snippets_copy_exactly(self, view):
        live = view.live
        assert json.loads(live.channel_config.toPlainText()) == json.loads(setup.claude_channel_config())
        live.copy_channel_config.click()
        assert QApplication.clipboard().text() == setup.claude_channel_config()
        live.copy_channel_command.click()
        assert QApplication.clipboard().text() == setup.claude_channel_command()
        assert live.channel_command.text() == setup.claude_channel_command()
        # The entry is a preview that opens on request.
        assert live.channel_preview.body.isHidden()
        live.channel_preview.toggle.click()
        assert not live.channel_preview.body.isHidden()

    def test_the_card_keeps_the_entry_to_projects_started_with_the_flag(self, view):
        scope = view.live.channel_scope.text()
        assert "user-wide" in scope and "can't receive messages" in scope
        assert "--dangerously-load-development-channels" in setup.claude_channel_command()

    def test_connected_sessions_are_counted(self, view, bus):
        live = view.live
        assert live.sessions_label.text() == "Needs MCP on"
        running(view)
        assert live.sessions_label.text() == "No sessions connected"
        bus.subscribe(lambda event: None, label=settings_mcp.CHANNEL_LISTENER_LABEL)
        live.refresh_live()
        # "Connected", not "listening": one started without the flag counts too.
        assert live.sessions_label.text() == "1 session connected"
        assert live.sessions_dot.status() == EngineStatus.READY
        # A channel session is not a plain event-stream program.
        assert live.listeners_label.text() == "No programs listening yet"


def test_host_tab_hides_this_computers_live_cards_and_says_where_they_are(tmp_path):
    service = FakeRemoteService()
    service.error = RemoteRequestError("Not allowed", code="forbidden")
    view = make_view(tmp_path, service)
    try:
        view.resize(700, 900)
        view.show()
        QApplication.processEvents()
        assert view.live.isVisible()
        assert not view.host_note.isVisible()
        view.tabs.buttons[1].click()
        tick(view)
        assert not view.live.isVisible()
        assert view.host_note.isVisible()
        assert "jed" in view.host_note.text() and "This computer" in view.host_note.text()
        view.tabs.buttons[0].click()
        QApplication.processEvents()
        assert view.live.isVisible() and not view.host_note.isVisible()
    finally:
        view.close()
        view.deleteLater()
        QApplication.processEvents()


def test_the_host_page_keeps_its_own_controls_without_live_cards(tmp_path):
    view = make_view(tmp_path, FakeRemoteService())
    try:
        view.resize(700, 900)
        view.show()
        view.tabs.buttons[1].click()
        tick(view)
        assert view.host_page is not None and view.host_page.isVisible()
        assert not view.host_page.findChildren(settings_mcp.LiveAutomationSection)
        assert view.host_layout.indexOf(view.host_page) < view.host_layout.indexOf(view.host_note)
        assert view.host_note.isVisible()
    finally:
        view.close()
        view.deleteLater()
        QApplication.processEvents()


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
@pytest.mark.parametrize("font_percent", [100, 130])
def test_live_cards_fit_and_split_into_columns_below_the_dialog_width(
    tmp_path, monkeypatch, ui_mode, font_percent
):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_scale, previous_palette, previous_sheet = (
        current_ui_font_scale_percent(), current_palette(), app.styleSheet())
    manager = ThemeManager()
    view = make_view(tmp_path, local_running=True)
    view.live.endpoints.toggle.click()
    view.live.channel_preview.toggle.click()
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(view)
    layout.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(wrapper)
    try:
        apply_ui_font_scale(font_percent, app=app, theme_manager=manager)
        scroll.show()
        live = view.live
        # The Settings dialog's page is about 983 px wide at its default size.
        for width in (360, 480, 720, 983, 1300):
            scroll.resize(width, 900)
            for _ in range(8):
                app.processEvents()
            assert scroll.horizontalScrollBar().maximum() == 0
            wide = live.width() >= round(740 * font_percent / 100)
            if width >= 983:
                assert wide, (width, live.width())
            if wide:
                assert live.mcp_events_card.x() > live.live_api_card.x()
                assert live.channel_card.y() > live.mcp_events_card.y()
            else:
                assert live.mcp_events_card.y() > live.live_api_card.y()
                assert live.channel_card.y() > live.mcp_events_card.y()
            for button in live.findChildren(QAbstractButton):
                if not button.isVisible():
                    continue
                position = button.mapTo(live, QPoint(0, 0))
                assert position.x() >= 0
                assert position.x() + button.width() <= live.width() + 1, button.text()
                assert button.height() >= min(button.sizeHint().height(), 34) - 1, button.text()
    finally:
        scroll.close()
        scroll.deleteLater()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setStyleSheet(previous_sheet)
        view.deleteLater()


class BoundFakeServer(FakeServer):
    """Stands in for the process-wide runtime, which Settings binds to its store."""

    def bind_settings(self, settings):
        self.settings = settings


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build():
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stack.enter_context(patch.object(settings_mcp, "runtime", BoundFakeServer()))
        controls = dict(settings_metadata.CONTROL_DESTINATIONS)
        fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)

        def restore():
            settings_metadata.CONTROL_DESTINATIONS.clear()
            settings_metadata.CONTROL_DESTINATIONS.update(controls)
            settings_metadata.PAGE_SEARCH_FIELDS.clear()
            settings_metadata.PAGE_SEARCH_FIELDS.update(fields)

        stack.callback(restore)
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        changes = []
        dialog.on_settings_changed = changes.append
        return dialog, store, changes

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def test_settings_page_wires_mcp_events_to_the_app(make_dialog):
    dialog, store, changes = make_dialog()
    try:
        assert MCP not in dialog._built_pages
        dialog.select_destination(MCP)
        assert dialog.rail.name(MCP) == "API & MCP"
        dialog.mcp_view.live.mcp_events_switch.click()
        assert store.get(SettingsKey.MCP_EVENTS_ENABLED) is True
        assert changes == ["mcp_events"]
        dialog.mcp_view.live.api_permission_checks[SettingsKey.API_RUN_ACTIONS].click()
        assert store.get(SettingsKey.API_RUN_ACTIONS) is True
        assert changes == ["mcp_events"]
    finally:
        dialog.close()
