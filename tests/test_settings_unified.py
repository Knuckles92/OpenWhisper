"""The unified Settings window: routing, Home, the chapter rail, and search.

Settings hosts what used to be three windows. These tests pin the parts that
only exist because of that: legacy destination names, the Home landing page,
the Downloads rail value, and the Ctrl+K palette across every page.
"""
import builtins
import gc
import os
import tempfile
import threading
import weakref
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services import credentials
from services.hf_access import CachedModelInfo
from services.settings import (
    HuggingFaceAccessPolicy,
    MeetingSpeakerIdBackend,
    SettingsKey,
    SettingsManager,
)
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import (
    CLEANUP,
    CLEANUP_RULES,
    BASIC_APP,
    BASIC_DICTATION,
    BASIC_MEETINGS,
    DOWNLOADS,
    GENERAL,
    HOOKS,
    MEETING_FAST,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    MCP,
    OVERVIEW,
    RECORDING,
    RUNTIME,
    VOICE_MODEL,
    resolve_destination,
)
from ui_qt.dialogs.settings_search import (
    HELP,
    MODEL,
    SETTING,
    SearchEntry,
    highlight,
    match_entries,
)
from ui_qt.ui_controller import UIController
from tests.test_rule_composer import _FakeRecorder

BASE_REPO = "Systran/faster-whisper-base"


def _cached(repo_id, size_bytes):
    return CachedModelInfo(
        repo_id=repo_id,
        size_bytes=size_bytes,
        path=f"/hub/models--{repo_id.replace('/', '--')}",
        revision_hashes=("abc",),
    )


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_dialog():
    """Build Settings against a throwaway store with a fixed model cache."""
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, cached=None, *, background_cache_scan=False):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({**(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(
            patch.object(settings_dialog_module.history_manager, "set_retention")
        )
        for module in (models_module, downloads_module):
            stack.enter_context(
                patch.object(module, "scan_cached_models", return_value=cached or {})
            )
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=background_cache_scan
        )
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


class TestRouting:
    @pytest.mark.usefixtures("isolated_app_theme")
    def test_page_transitions_keep_sidebar_rows_over_their_click_targets(self, make_dialog):
        from ui_qt.utils.font_scale import apply_ui_font_scale
        from ui_qt.utils.theme_manager import ThemeManager

        app = QApplication.instance()
        apply_ui_font_scale(115, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog()
        dialog.show()
        try:
            QTest.qWait(50)
            for key in ("advanced", VOICE_MODEL, "meeting_dashboard", OVERVIEW,
                        "cleanup_rules", "backup", RECORDING, VOICE_MODEL,
                        "meeting_dashboard", "meeting_after", "api_keys", OVERVIEW):
                dialog.select_destination(key)
                QTest.qWait(50)
                rail = dialog.rail
                for destination, row in rail._rows.items():
                    if rail._items[destination].isHidden():
                        continue
                    expected = rail.visualItemRect(rail._items[destination])
                    assert row.geometry().top() == expected.top(), (key, destination, row.geometry(), expected)
                    assert row.geometry().bottom() == expected.bottom(), (key, destination, row.geometry(), expected)
        finally:
            dialog.close()

    @pytest.mark.usefixtures("isolated_app_theme")
    @pytest.mark.parametrize("scale", [100, 115, 130])
    @pytest.mark.parametrize("target", ["name_label", "value_label", "icon_label", "row"])
    def test_scrolled_sidebar_clicks_open_the_visible_destination(self, make_dialog, scale, target):
        from ui_qt.utils.font_scale import apply_ui_font_scale
        from ui_qt.utils.theme_manager import ThemeManager

        app = QApplication.instance()
        apply_ui_font_scale(scale, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog()
        dialog.show()
        dialog.resize(1430, 850)
        try:
            # The App chapter's pages show once one of them is current.
            dialog.select_destination("general")
            app.processEvents()
            rail = dialog.rail
            point = rail.viewport().rect().center()
            # One wheel event scrolls at most a page, and at large fonts the
            # rail is more than one page taller than its viewport.
            for _ in range(3):
                QApplication.sendEvent(rail.viewport(), QWheelEvent(
                    QPointF(point), QPointF(rail.viewport().mapToGlobal(point)),
                    QPoint(), QPoint(0, -2400), Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
                ))
                app.processEvents()
            for key in ("hotkeys", "api_keys", "advanced"):
                row = rail._rows[key]
                label = row if target == "row" else getattr(row, target)
                point = label.mapTo(rail.viewport(), label.rect().center())
                assert rail.viewport().rect().contains(point)
                assert rail.itemAt(point) is rail._items[key]
                QTest.mouseClick(dialog.windowHandle(), Qt.MouseButton.LeftButton,
                                 pos=label.mapTo(dialog, label.rect().center()))
                app.processEvents()
                assert rail.current_key() == key
                assert dialog.stack.currentWidget() is dialog._page_scrolls[key]
                assert dialog.page_title.text() == dialog._headings[key][0]
        finally:
            dialog.close()

    def test_window_opens_on_home(self, make_dialog):
        dialog, _store = make_dialog()
        assert dialog.rail.current_key() == OVERVIEW
        assert dialog.page_title.text() == "Home"
        assert dialog.rail.name(OVERVIEW) == "Home"
        assert dialog.rail.value(OVERVIEW) == ""

    def test_show_home_returns_to_the_top_of_home(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        try:
            dialog.select_destination(MCP)
            dialog.show_home()
            assert dialog.rail.current_key() == OVERVIEW
            assert dialog._page_scrolls[OVERVIEW].verticalScrollBar().value() == 0
        finally:
            dialog.close()

    def test_refresh_rebuilds_home_once(self, make_dialog):
        dialog, _store = make_dialog()
        with patch.object(dialog, "_refresh_home", wraps=dialog._refresh_home) as home:
            dialog.refresh()
        # Model and download refreshes each ask for a rail redraw; Home
        # behind it is rebuilt once, after they all finish.
        assert home.call_count == 1

    def test_opening_settings_keeps_runtime_imports_off_the_ui_thread(
        self, make_dialog, monkeypatch
    ):
        started = threading.Event()
        release = threading.Event()
        probes = []
        ui_imports = []
        original_import = builtins.__import__

        def import_checked(name, *args, **kwargs):
            if (
                name.split(".", 1)[0] in {"faster_whisper", "ctranslate2", "torch"}
                and threading.current_thread() is threading.main_thread()
            ):
                ui_imports.append(name)
                raise ImportError("Speech runtimes must stay off the UI thread")
            return original_import(name, *args, **kwargs)

        def slow_runtime(backend, device):
            probes.append(threading.current_thread())
            started.set()
            assert release.wait(5)
            return "asr-nvidia-cpu", "cpu"

        monkeypatch.setattr(builtins, "__import__", import_checked)
        monkeypatch.setattr("services.local_asr.catalog.resolve_runtime", slow_runtime)
        try:
            dialog, _store = make_dialog(
                {SettingsKey.SELECTED_MODEL: "parakeet"}, background_cache_scan=True
            )
            # The hidden engine page no longer probes during first open.
            assert not started.is_set()
            dialog.select_destination(VOICE_MODEL)
            assert started.wait(2)
            dialog.show()
            QApplication.instance().processEvents()
            dialog.select_destination(GENERAL)
            dialog.select_destination(VOICE_MODEL)
            dialog.refresh()
            assert dialog.isVisible()
            assert not ui_imports
            assert len(probes) == 1
            assert probes[0] is not threading.main_thread()
            assert "Parakeet" in dialog.models.engine_inventory_label.text()
        finally:
            release.set()
            for probe in probes:
                probe.join(2)
        QApplication.instance().processEvents()
        assert "NVIDIA Speech CPU" in dialog.models.engine_inventory_label.text()
        dialog.close()

    def test_runtime_result_cannot_overwrite_a_new_device_selection(self, make_dialog):
        with patch.object(models_module, "_check_engine_runtime"):
            dialog, store = make_dialog(
                {SettingsKey.SELECTED_MODEL: "parakeet"}, background_cache_scan=True
            )
            models = dialog.models
            dialog.ensure_page(VOICE_MODEL)
            auto = models._engine_runtime_pending[("parakeet", "auto")]
            store.save_setting("local_asr_devices", {"parakeet": "cpu"})
            models._refresh_engine_inventory()
            cpu = models._engine_runtime_pending[("parakeet", "cpu")]
            current = models.engine_inventory_label.text()

            models._on_engine_runtime_checked(("parakeet", "auto"), " · Old runtime.", auto)
            assert models.engine_inventory_label.text() == current

            # A result for another Settings window's request is not this one's.
            models._on_engine_runtime_checked(("parakeet", "cpu"), " · Theirs.", cpu + 1)
            assert models.engine_inventory_label.text() == current

            store.save_setting(SettingsKey.SELECTED_MODEL, "api")
            models.refresh_engine_selection()
            models._on_engine_runtime_checked(("parakeet", "cpu"), " · Late result.", cpu)
            assert models.engine_inventory_label.text() == ""

            # Kept for when the engine is chosen again.
            store.save_setting(SettingsKey.SELECTED_MODEL, "parakeet")
            models.refresh_engine_selection()
            assert models.engine_inventory_label.text().endswith(" · Late result.")

    def test_legacy_model_manager_names_land_on_a_destination(
        self, make_dialog, subtests
    ):
        aliases = [
        ("ondemand", VOICE_MODEL),
        ("text", CLEANUP),
        ("meeting", MEETING_VOICE),
        ("runtime", RUNTIME),
        ("downloads", DOWNLOADS),
        ("library", DOWNLOADS),
        ("voice", DOWNLOADS),
        ("engine_downloads", DOWNLOADS),
        (GENERAL, GENERAL),
        ]
        dialog, _store = make_dialog()
        for alias, destination in aliases:
            with subtests.test(alias=alias, destination=destination):
                dialog.select_destination(OVERVIEW)
                assert dialog.rail.current_key() == OVERVIEW
                assert resolve_destination(alias) == destination
                dialog.select_destination(alias)
                assert dialog.rail.current_key() == destination

    def test_voice_page_opens_downloads_filtered_to_its_engine(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.SELECTED_MODEL: "parakeet"})
        dialog.select_destination(VOICE_MODEL)

        dialog.models.speech_download_button.click()

        assert dialog.rail.current_key() == DOWNLOADS
        assert dialog.downloads.backend_filter_combo.currentData() == "parakeet"

    def test_cleanup_profiles_model_link_opens_the_cleanup_model(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.cleanup_profiles_panel.model_requested.emit()
        assert dialog.rail.current_key() == CLEANUP

    def test_hf_policy_persists_from_the_downloads_page(self, make_dialog):
        dialog, store = make_dialog()
        combo = dialog.hf_policy_combo
        combo.setCurrentIndex(combo.findData(HuggingFaceAccessPolicy.NEVER))
        assert store.load_hf_access_policy() == HuggingFaceAccessPolicy.NEVER


class TestHome:
    def test_home_opens_without_building_pages_or_asking_for_microphones(self, make_dialog):
        dialog, store = make_dialog()
        assert dialog.rail.current_key() == OVERVIEW
        assert dialog.stack.currentWidget() is dialog._page_scrolls[OVERVIEW]
        assert dialog._built_pages == {OVERVIEW}
        combo = dialog.home.controls[SettingsKey.AUDIO_INPUT_PRIORITY]
        assert [combo.itemText(i) for i in range(combo.count())] == ["System default"]

    def test_basic_tab_keys_open_their_home_cards(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        try:
            for key, card in ((BASIC_MEETINGS, "meetings"), (BASIC_APP, "app"),
                              (BASIC_DICTATION, "dictation")):
                dialog.select_destination(RECORDING)
                dialog.select_destination(key)
                QApplication.processEvents()
                assert dialog.rail.current_key() == OVERVIEW
                assert dialog.home.cards[card].property("searchHit") is True
                dialog._clear_search_flash()
        finally:
            dialog.close()

    def test_home_and_page_changes_share_the_same_store_and_callbacks(self, make_dialog):
        dialog, store = make_dialog({
            SettingsKey.STREAMING_ENABLED: False,
            SettingsKey.STREAMING_OVERLAY_ENABLED: True,
        })
        page = dialog.home
        dialog.on_streaming_settings_changed = MagicMock()
        page.controls[SettingsKey.STREAMING_ENABLED].click()
        assert store.get(SettingsKey.STREAMING_ENABLED) is True
        assert SettingsKey.STREAMING_OVERLAY_ENABLED not in store.load_all_settings()
        dialog.on_streaming_settings_changed.assert_called_once_with()
        dialog.select_destination(RECORDING)
        assert dialog.streaming_enabled_check.isChecked()
        dialog.streaming_enabled_check.setChecked(False)
        dialog.show_home()
        assert not page.controls[SettingsKey.STREAMING_ENABLED].isChecked()

    def test_microphone_reads_devices_once_its_menu_opens(self, make_dialog):
        from services.audio_devices import InputDevice
        from ui_qt.dialogs.settings_microphones import SYSTEM_DEFAULT, entry_token

        dialog, store = make_dialog()
        dialog.on_audio_device_changed = MagicMock()
        combo = dialog.home.controls[SettingsKey.AUDIO_INPUT_PRIORITY]
        assert RECORDING not in dialog._built_pages
        with patch.object(settings_dialog_module.AudioRecorder, "get_input_devices", return_value=[]):
            dialog.home._load_microphones()
        assert RECORDING in dialog._built_pages
        desk = InputDevice(index=11, name="Desk microphone", hostapi="MME", default_api=True)
        dialog._apply_audio_devices(dialog._audio_device_generation, "", [desk], "")
        combo.setCurrentIndex(combo.findData(entry_token(desk.key)))
        assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [desk.key]
        # The pre-ranking index follows along for a downgrade.
        assert store.get(SettingsKey.AUDIO_INPUT_DEVICE) == 11
        dialog.on_audio_device_changed.assert_called_once_with([desk.key])
        assert dialog.audio_device_combo.currentData() == entry_token(desk.key)
        combo.setCurrentIndex(combo.findData(SYSTEM_DEFAULT))
        assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == []
        assert SettingsKey.AUDIO_INPUT_DEVICE not in store.load_all_settings()

    def test_voice_choice_uses_the_canonical_controller_label_without_model_setup(self, make_dialog):
        from ui_qt.widgets.speech_backend_picker import backend_display_name
        dialog, store = make_dialog()
        combo = dialog.home.voice_combo
        backend = combo.itemData((combo.currentIndex() + 1) % combo.count())
        def choose(display):
            from config import config
            store.save_model_selection(config.MODEL_VALUE_MAP[display])
        dialog.models.on_backend_changed = MagicMock(side_effect=choose)
        combo.setCurrentIndex(combo.findData(backend))
        assert store.load_model_selection() == backend
        dialog.models.on_backend_changed.assert_called_once_with(backend_display_name(backend))
        assert VOICE_MODEL not in dialog._built_pages

    def test_app_theme_and_meeting_report_edits_use_existing_handlers(self, make_dialog):
        dialog, store = make_dialog()
        dialog.on_ui_theme_changed = MagicMock()
        theme = dialog.home.controls[SettingsKey.UI_THEME]
        theme.setCurrentIndex(theme.findData("light"))
        assert store.get(SettingsKey.UI_THEME) == "light"
        dialog.on_ui_theme_changed.assert_called_once_with("light")
        report = dialog.home.controls[SettingsKey.MEETING_END_REPORT]
        report.click()
        assert store.get(SettingsKey.MEETING_END_REPORT) is report.isChecked()
        dialog.select_destination("meeting_after")
        assert dialog.meeting_end_report_check.isChecked() == report.isChecked()

    def test_failed_save_restores_home_and_allows_retry(self, make_dialog):
        dialog, store = make_dialog()
        switch = dialog.home.controls[SettingsKey.AUTO_PASTE]
        previous = switch.isChecked()
        with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
            switch.click()
        assert switch.isChecked() == previous
        assert dialog.auto_paste_check.isChecked() == previous
        assert "Disk full" in dialog.message_label.text()
        switch.click()
        assert store.get(SettingsKey.AUTO_PASTE) == (not previous)
        assert dialog.message_label.text() == ""

    def test_home_shortcut_capture_cancels_when_leaving_home(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.on_profile_hotkey_capture = MagicMock()
        field = dialog.home.shortcut
        field.begin_capture()
        dialog.on_profile_hotkey_capture.assert_called_with(True)
        dialog.select_destination(RECORDING)
        assert not field._capturing
        dialog.on_profile_hotkey_capture.assert_called_with(False)

    def test_change_button_captures_and_saves_a_shortcut_inline(self, make_dialog):
        from PyQt6.QtWidgets import QPushButton
        dialog, store = make_dialog()
        dialog.show()
        QApplication.instance().processEvents()
        field = dialog.home.shortcut
        button = field.parentWidget().findChild(QPushButton, "settingsChangeShortcut")
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert field.hasFocus()
        QTest.keyClick(field, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert not field._capturing
        assert store.load_hotkey_settings()["record_toggle"] == field.hotkey
        dialog.select_destination("hotkeys")
        assert dialog.hotkey_inputs["record_toggle"].text() == field.text()
        dialog.close()

    @pytest.mark.usefixtures("isolated_app_theme")
    @pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("classic", 1315), ("omarchy", 460)])
    def test_home_rows_fit_narrow_windows_and_large_fonts(self, make_dialog, monkeypatch, ui_mode, width):
        from PyQt6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QLabel
        from ui_qt.utils.font_scale import apply_ui_font_scale
        from ui_qt.utils.theme_manager import ThemeManager
        monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
        app = QApplication.instance()
        manager = ThemeManager()
        dialog = None
        try:
            apply_ui_font_scale(130, app=app, theme_manager=manager)
            dialog, _store = make_dialog()
            dialog.show()
            dialog._fit_to_screen()
            dialog.resize(width, 600)
            for _ in range(8):
                app.processEvents()
            page = dialog.home
            assert dialog.width() == width
            # Too narrow for the rail beside a page: the header's menu stands in.
            assert dialog.rail_pane.isHidden() == (width < 1040)
            assert dialog.page_menu_button.isHidden() == (width >= 1040)
            for control in (page.findChildren(QAbstractButton) + page.findChildren(QComboBox) + page.findChildren(QLineEdit)):
                if not control.isVisible():
                    continue
                assert control.mapTo(page, control.rect().topLeft()).x() >= 0
                assert control.mapTo(page, control.rect().bottomRight()).x() < page.width()
            for label in page.findChildren(QLabel):
                if label.isVisible() and label.wordWrap():
                    assert label.height() >= label.heightForWidth(label.width())
            scroll = dialog._page_scrolls[OVERVIEW]
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            app.processEvents()
            assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
        finally:
            if dialog is not None:
                dialog.close()

    def test_cards_sit_in_two_columns_when_the_page_is_wide(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        try:
            cards = dialog.home.cards
            dialog.resize(1500, 850)
            for _ in range(4):
                QApplication.processEvents()
            assert cards["personalize"].x() > cards["dictation"].x()
            assert cards["app"].y() > cards["dictation"].y()
            dialog.resize(dialog.MINIMUM_SIZE)
            for _ in range(4):
                QApplication.processEvents()
            assert {card.x() for card in cards.values()} == {cards["dictation"].x()}
            assert (cards["storage"].y() > cards["app"].y() > cards["meetings"].y()
                    > cards["personalize"].y() > cards["dictation"].y())
        finally:
            dialog.close()


class TestChapters:
    def test_going_to_a_page_opens_its_chapter_and_marks_it(self, make_dialog):
        dialog, _store = make_dialog()
        assert dialog.rail.active_chapter() == ""
        dialog.select_destination(CLEANUP)
        assert dialog.rail.active_chapter() == "Dictation"
        assert not dialog.rail._items[CLEANUP].isHidden()
        assert dialog.rail._items[MCP].isHidden()
        dialog.select_destination(MCP)
        assert dialog.rail.active_chapter() == "Automate"
        # The chapter you came from stays open, so nothing above shifts.
        assert not dialog.rail._items[CLEANUP].isHidden()

    def test_the_crumb_names_the_chapter(self, make_dialog):
        dialog, _store = make_dialog()
        assert dialog.page_crumb.text() == "SETTINGS"
        dialog.select_destination(MEETING_FAST)
        assert dialog.page_crumb.text() == "MEETING MODE"
        assert dialog.page_title.text() == "Fast judgments"

    def test_every_page_is_in_exactly_one_place(self, make_dialog):
        dialog, _store = make_dialog()
        keys = dialog.rail.keys()
        assert keys[0] == OVERVIEW and dialog.rail.chapter(OVERVIEW) == ""
        chaptered = [key for chapter in dialog.rail.chapters() for key in dialog.rail.chapter_keys(chapter)]
        assert sorted(chaptered) == sorted(keys[1:])
        assert set(keys) == set(dialog._pages)

    def test_the_page_menu_lists_every_page_by_chapter(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.select_destination(HOOKS)
        dialog._show_page_menu()
        menu = dialog._page_menu
        try:
            texts = [action.text() for action in menu.actions() if not action.isSeparator()]
            assert texts[0] == "Home"
            assert texts[texts.index("Automate") + 1:texts.index("Automate") + 4] == ["Actions", "Hooks", "API & MCP"]
            checked = [action.text() for action in menu.actions() if action.isChecked()]
            assert checked == ["Hooks"]
            headings = [action for action in menu.actions() if action.text() in dialog.rail.chapters()]
            assert headings and not any(action.isEnabled() for action in headings)
            next(action for action in menu.actions() if action.text() == "Runtime").trigger()
            assert dialog.rail.current_key() == RUNTIME
        finally:
            menu.close()

class TestControllerRouting:
    def _controller(self, dialog, missing_runtime=None):
        controller = SimpleNamespace(
            _prepare_settings_dialog=MagicMock(return_value=dialog),
            _raise_dialog=MagicMock(),
            get_missing_local_runtime=(lambda: missing_runtime),
        )
        controller.open_downloads = lambda component_id=None: (
            UIController.open_downloads(controller, component_id)
        )
        return controller

    def test_text_alias_scrolls_to_the_cleanup_model(self):
        dialog = MagicMock()
        controller = self._controller(dialog)
        UIController.open_settings_destination(controller, "text")
        dialog.focus_cleanup_model.assert_called_once_with()
        controller._raise_dialog.assert_called_once_with(dialog)

    def test_engine_downloads_focuses_the_missing_runtime(self):
        dialog = MagicMock()
        controller = self._controller(dialog, missing_runtime="asr-nvidia-cuda")
        UIController.open_settings_destination(controller, "engine_downloads")
        dialog.select_destination.assert_called_once_with(DOWNLOADS)
        dialog.downloads.focus_component.assert_called_once_with("asr-nvidia-cuda")

    def test_other_names_select_their_destination(self):
        dialog = MagicMock()
        controller = self._controller(dialog)
        UIController.open_settings_destination(controller, "ondemand")
        dialog.select_destination.assert_called_once_with("ondemand")
        controller._raise_dialog.assert_called_once_with(dialog)


class TestRailValues:
    def test_downloads_rail_reports_totals_then_live_progress(self, make_dialog):
        dialog, _store = make_dialog(cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)})
        dialog.refresh_models()
        assert dialog.rail.value(DOWNLOADS).startswith("1 of ")

        dialog.downloads.set_downloading("tiny")
        dialog.downloads.set_download_progress("tiny", 50, 100)
        assert dialog.rail.value(DOWNLOADS) == "Downloading tiny · 50%"

        dialog.downloads.finish_download("tiny", True)
        assert dialog.rail.value(DOWNLOADS).startswith("1 of ")

    def test_cleanup_rail_combines_the_level_and_the_hosted_model(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-test",
        })
        assert dialog.rail.value(CLEANUP) == "Medium · gpt-test"
        dialog.transcript_cleanup_check.setChecked(False)
        assert dialog.rail.value(CLEANUP) == "Off"


class TestHomeSummary:
    def test_rows_and_storage_repeat_what_each_destination_reports(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.MEETING_LLM_PROVIDER: "openai",
            SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
            SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OFF,
        }, cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)})
        dialog.refresh_models()
        dialog.select_destination(OVERVIEW)
        home = dialog.home

        assert home.meeting_intelligence.text().startswith("gpt-4o-mini")
        assert "Me / Others labels" in home.meeting_voice.text()
        assert home.storage_value.text() == "145 MB of speech models"
        assert home.storage_detail.text().startswith("1 of ")

    def test_links_and_buttons_open_their_destination(self, make_dialog):
        from PyQt6.QtWidgets import QPushButton

        dialog, _store = make_dialog()
        dialog.show()
        try:
            button = next(b for b in dialog.home.findChildren(QPushButton)
                          if b.accessibleName() == "Set up AI insights")
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert dialog.rail.current_key() == MEETING_INTELLIGENCE

            dialog.show_home()
            link = next(b for b in dialog.home.cards["storage"].findChildren(QPushButton, "homeLink"))
            link.click()
            assert dialog.rail.current_key() == DOWNLOADS
        finally:
            dialog.close()

    def test_where_strip_places_a_remote_cleanup_provider_in_the_cloud(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free",
        })
        dialog.select_destination(OVERVIEW)
        assert "AI cleanup" in dialog.home.cloud_list.text()
        assert "AI cleanup" not in dialog.home.local_list.text()

    def test_where_strip_keeps_a_local_cleanup_provider_on_this_computer(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2",
        })
        dialog.select_destination(OVERVIEW)
        assert "AI cleanup" in dialog.home.local_list.text()
        assert "AI cleanup" not in dialog.home.cloud_list.text()

    def test_home_is_only_reread_while_it_shows(self, make_dialog):
        dialog, store = make_dialog()
        dialog.select_destination(GENERAL)
        with patch.object(dialog.home, "refresh") as refresh:
            dialog.auto_paste_check.click()
        refresh.assert_not_called()
        dialog.show_home()
        assert dialog.home.controls[SettingsKey.AUTO_PASTE].isChecked() == store.get(SettingsKey.AUTO_PASTE)

class TestSearchMatching:
    def _entries(self):
        return [
            SearchEntry(SETTING, "Recording engine: Parakeet", "Dictation › Voice model", VOICE_MODEL),
            SearchEntry(SETTING, "Microphone", "Dictation › Recording", RECORDING,
                        keywords="audio input device parakeet-free"),
            SearchEntry(MODEL, "Parakeet TDT 0.6B v3", "Downloads · 714 MB", DOWNLOADS,
                        model_name="parakeet-v3"),
            SearchEntry(HELP, "Voice model", "…Parakeet transcribes short chunks…", VOICE_MODEL),
        ]

    def test_every_word_must_match_and_sections_keep_their_order(self):
        results = match_entries(self._entries(), "parakeet")
        assert [entry.kind for entry in results] == [SETTING, SETTING, MODEL, HELP]
        # A title hit outranks a keyword-only hit.
        assert results[0].title == "Recording engine: Parakeet"
        assert match_entries(self._entries(), "parakeet engine")[0].destination == VOICE_MODEL
        assert match_entries(self._entries(), "nothing like this") == []
        assert match_entries(self._entries(), "   ") == []

    def test_each_section_is_capped(self):
        entries = [
            SearchEntry(MODEL, f"model {index}", "Downloads", DOWNLOADS)
            for index in range(20)
        ]
        assert len(match_entries(entries, "model")) == 5

    def test_highlight_escapes_and_tints_every_match(self):
        html = highlight("Tag <b> parakeet Parakeet", ["parakeet"], "#123456")
        assert "&lt;b&gt;" in html
        assert html.count("color:#123456") == 2


class TestSearchPalette:
    def test_history_sharing_search_opens_and_marks_its_mcp_control(self, make_dialog):
        dialog, _store = make_dialog()
        try:
            dialog.show()
            assert MCP not in dialog._built_pages
            dialog.open_search("share history")
            entries = dialog.search_palette.current_entries()
            assert len(entries) == 1 and entries[0].destination == MCP
            QTest.keyClick(dialog.search_palette.input, Qt.Key.Key_Return)
            QApplication.processEvents()
            assert dialog.rail.current_key() == MCP
            assert dialog.mcp_history_tile.isVisible()
            assert dialog.mcp_history_tile.property("searchHit") is True
            matches = match_entries(dialog._search_index(), "share history")
            assert len(matches) == 1 and matches[0].target is dialog.mcp_history_tile
        finally:
            dialog.close()

    def test_index_covers_tiles_fields_help_models_and_components(self, make_dialog):
        dialog, _store = make_dialog()
        index = dialog._search_index()
        titles = {entry.title for entry in index}
        kinds = {entry.kind for entry in index}

        assert {SETTING, MODEL, HELP} <= kinds
        assert "Clean up transcripts with AI" in titles
        assert "Paste into the active window" in titles
        assert any(title.startswith("Recording engine") for title in titles)
        assert "Voice model" in titles
        assert any(entry.model_name == "tiny" for entry in index)
        dialog.ensure_page(DOWNLOADS)
        assert any(entry.component_id for entry in index) == bool(
            dialog.downloads._component_rows
        )
        voice = next(e for e in index if e.title.startswith("Recording engine"))
        assert voice.detail == "Dictation › Voice model"

    def test_ctrl_k_opens_the_palette_and_escape_keeps_settings_open(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        # Offscreen windows never become active, so fire the window shortcut
        # the way Qt would once the key reaches it.
        keys = {shortcut.key().toString() for shortcut in dialog._search_shortcuts}
        assert "Ctrl+K" in keys
        dialog._search_shortcuts[0].activated.emit()
        assert dialog.search_palette.isVisible()

        QTest.keyClick(dialog.search_palette.input, Qt.Key.Key_Escape)
        assert not dialog.search_palette.isVisible()
        assert dialog.isVisible()
        dialog.close()

    def test_rail_search_button_opens_the_palette(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog.search_button.click()
        assert dialog.search_palette.isVisible()
        dialog.close()

    def test_enter_opens_the_setting_and_marks_its_tile(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog.open_search("paste into the active")
        entries = dialog.search_palette.current_entries()
        assert entries and entries[0].title == "Paste into the active window"

        QTest.keyClick(dialog.search_palette.input, Qt.Key.Key_Return)
        QApplication.processEvents()

        assert not dialog.search_palette.isVisible()
        assert dialog.rail.current_key() == GENERAL
        assert dialog.auto_paste_tile.property("searchHit") is True
        dialog._clear_search_flash()
        assert dialog.auto_paste_tile.property("searchHit") is False
        dialog.close()

    def test_arrow_keys_skip_section_headers(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog.open_search("tiny")
        palette = dialog.search_palette
        first = palette.results.currentItem()
        assert first is not None and first.data(Qt.ItemDataRole.UserRole) is not None

        QTest.keyClick(palette.input, Qt.Key.Key_Down)
        current = palette.results.currentItem()
        assert current.data(Qt.ItemDataRole.UserRole) is not None
        dialog.close()

    def test_a_model_result_opens_downloads_on_that_model(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog.downloads.status_filter_combo.setCurrentIndex(
            dialog.downloads.status_filter_combo.findData("downloaded")
        )
        dialog.open_search("tiny.en")
        entry = next(
            e for e in dialog.search_palette.current_entries() if e.model_name == "tiny.en"
        )
        dialog._on_search_activated(entry)

        assert dialog.rail.current_key() == DOWNLOADS
        assert dialog.downloads._selected_model == "tiny.en"
        # Filters that hid the model are cleared so it can be seen.
        assert not dialog.downloads.rows["tiny.en"].isHidden()
        dialog.close()

    def test_old_window_names_still_find_their_pages(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog.open_search("model manager")
        entries = dialog.search_palette.current_entries()
        assert entries[0].destination == VOICE_MODEL
        dialog.close()


class TestWorkers:
    @pytest.mark.parametrize("work", [
        "api-key-verify", "jev-route-verify", "cleanup-rule-polish", "rule-dictation",
    ])
    def test_a_window_closed_mid_work_is_not_held_by_the_worker(
        self, make_dialog, monkeypatch, work
    ):
        # The workers used to capture the window (dictation also through its
        # recorder's level callback): one kept a closed window alive, then
        # dropped the last reference (deleting a QWidget) or emitted on it
        # from its own thread, which segfaulted CI.
        gate = threading.Event()

        def blocked(result):
            def call(*_args, **_kwargs):
                gate.wait(5)
                return result
            return call

        dialog, _store = make_dialog()
        if work == "api-key-verify":
            monkeypatch.setattr(settings_dialog_module, "verify_api_key", blocked((True, "ok")))
            dialog.focus_api_keys("OPENAI_API_KEY")
            dialog.api_key_edit.setText("sk-proj-abcdefghijklmnopqrstuvwxyz")
            dialog._test_api_key()
        elif work == "jev-route-verify":
            credentials.store().set(
                settings_dialog_module.TYPESAFE_CREDENTIAL_ENV, "ts-live-0123456789abcdefghij"
            )
            monkeypatch.setattr(
                settings_dialog_module, "typesafe_verify_key", blocked((True, "ok"))
            )
            dialog.select_destination(MEETING_FAST)
            dialog._test_typesafe_route()
        elif work == "cleanup-rule-polish":
            monkeypatch.setattr(
                "services.transcript_cleanup.polish_cleanup_rule", blocked(("Rule.", None))
            )
            dialog.select_destination(CLEANUP_RULES)
            dialog._polish_cleanup_rule("capitalize kubernetes")
        else:
            monkeypatch.setattr(settings_dialog_module, "AudioRecorder", _FakeRecorder)
            dialog.on_dictation_transcribe = blocked("capitalize kubernetes")
            dialog.select_destination(CLEANUP_RULES)
            dialog._toggle_rule_dictation()
            dialog._toggle_rule_dictation()
        workers = [t for t in threading.enumerate() if t.name == work]
        assert workers
        ref = weakref.ref(dialog)
        dialog.deleteLater()
        del dialog
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        # PyQt releases the window's lambda slots by a queued call, then
        # deleteLater; until both run they hold it too.
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        gc.collect()
        assert ref() is None
        gate.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        QApplication.processEvents()
