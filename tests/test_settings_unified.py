"""The unified Settings window: routing, the Overview, and search.

Settings hosts what used to be three windows. These tests pin the parts that
only exist because of that: legacy destination names, the Overview landing
page, the Downloads rail value, and the Ctrl+K palette across every page.
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
    SettingsView,
)
from tests.test_rule_composer import _FakeRecorder
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import (
    APP_GENERAL,
    BASIC_APP,
    BASIC_DICTATION,
    BASIC_MEETINGS,
    CLEANUP,
    CLEANUP_RULES,
    DOWNLOADS,
    GENERAL,
    MCP,
    MEETING_FAST,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
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
    from PyQt6.QtGui import QFont, QFontDatabase
    app = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and os.name == "nt":
        for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/" + name)
        app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.fixture
def make_dialog():
    """Build Settings against a throwaway store with a fixed model cache."""
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, cached=None, *, background_cache_scan=False):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
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
    def test_page_transitions_keep_sidebar_rows_over_their_click_targets(self, make_dialog):
        from ui_qt.utils.font_scale import (
            apply_ui_font_scale,
            current_ui_font_scale_percent,
        )
        from ui_qt.utils.theme_manager import ThemeManager

        app = QApplication.instance()
        previous_style, previous_font = app.styleSheet(), app.font()
        previous_scale = current_ui_font_scale_percent()
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
                rail = dialog.category_rail
                for destination, row in rail._rows.items():
                    expected = rail.visualItemRect(rail._items[destination])
                    assert row.geometry().top() == expected.top(), (key, destination, row.geometry(), expected)
                    assert row.geometry().bottom() == expected.bottom(), (key, destination, row.geometry(), expected)
        finally:
            dialog.close()
            apply_ui_font_scale(previous_scale, app=app)
            app.setFont(previous_font)
            app.setStyleSheet(previous_style)

    @pytest.mark.parametrize("scale", [100, 115, 130])
    @pytest.mark.parametrize("target", ["name_label", "icon_label", "row"])
    def test_scrolled_sidebar_clicks_open_the_visible_destination(self, make_dialog, scale, target):
        from ui_qt.utils.font_scale import (
            apply_ui_font_scale,
            current_ui_font_scale_percent,
        )
        from ui_qt.utils.theme_manager import ThemeManager

        app = QApplication.instance()
        previous_style = app.styleSheet()
        previous_font = app.font()
        previous_scale = current_ui_font_scale_percent()
        apply_ui_font_scale(scale, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog()
        dialog.show()
        dialog.resize(1430, 850)
        try:
            app.processEvents()
            rail = dialog.category_rail
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
            for key in ("models", "connections", "app"):
                row = rail._rows[key]
                label = row if target == "row" else getattr(row, target)
                point = label.mapTo(rail.viewport(), label.rect().center())
                assert rail.viewport().rect().contains(point)
                assert rail.itemAt(point) is rail._items[key]
                QTest.mouseClick(dialog.windowHandle(), Qt.MouseButton.LeftButton,
                                 pos=label.mapTo(dialog, label.rect().center()))
                app.processEvents()
                assert rail.current_key() == key
                assert dialog.stack.currentWidget() is dialog.sections.areas[key]
                assert dialog.page_title.text() == rail.name(key)
        finally:
            dialog.close()
            apply_ui_font_scale(previous_scale, app=app)
            app.setFont(previous_font)
            app.setStyleSheet(previous_style)

    def test_window_opens_on_dictation(self, make_dialog):
        dialog, _store = make_dialog()
        assert dialog.rail.current_key() == RECORDING
        assert dialog.category_rail.current_key() == "dictation"
        assert dialog.page_title.text() == "Dictation"
        assert dialog.sections.sections[RECORDING].is_expanded()


    def test_refresh_rebuilds_the_overview_once(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.select_destination(OVERVIEW)
        with patch.object(
            dialog, "_refresh_overview", wraps=dialog._refresh_overview
        ) as overview:
            dialog.refresh()
        # Model and download refreshes each ask for a rail redraw; the
        # Overview behind it is rebuilt once, after they all finish.
        assert overview.call_count == 1

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

    def test_cleanup_profiles_model_link_opens_the_cleanup_chat_model(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.cleanup_profiles_panel.model_requested.emit()
        assert dialog.rail.current_key() == CLEANUP

    def test_hf_policy_persists_from_the_downloads_page(self, make_dialog):
        dialog, store = make_dialog()
        combo = dialog.hf_policy_combo
        combo.setCurrentIndex(combo.findData(HuggingFaceAccessPolicy.NEVER))
        assert store.load_hf_access_policy() == HuggingFaceAccessPolicy.NEVER


class TestGroupedSettings:
    @pytest.mark.parametrize("legacy_view", [SettingsView.BASIC, SettingsView.ADVANCED, "unknown"])
    def test_one_view_preserves_legacy_preferences_without_eager_model_setup(self, make_dialog, legacy_view):
        dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: legacy_view, SettingsKey.AUTO_PASTE: False})
        assert dialog.category_rail.keys() == ("dictation", "meetings", "personalize", "models", "connections", "app")
        assert dialog.page_title.text() == "Dictation"
        assert not dialog.rail_pane.isHidden()
        assert dialog.basic_tabs.isHidden()
        assert not dialog.view_buttons
        assert not {VOICE_MODEL, CLEANUP, MEETING_INTELLIGENCE, DOWNLOADS} & dialog._built_pages
        assert store.get(SettingsKey.SETTINGS_VIEW) == legacy_view
        assert store.get(SettingsKey.AUTO_PASTE) is False
        dialog.close()

    def test_category_remembers_its_last_destination(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.select_destination(GENERAL)
        dialog.category_rail.select("app")
        dialog.category_rail.select("dictation")
        assert dialog.rail.current_key() == GENERAL
        assert dialog.sections.sections[GENERAL].is_expanded()
        dialog.close()

    @pytest.mark.parametrize("alias,category", [(BASIC_DICTATION, "dictation"), (BASIC_MEETINGS, "meetings"), (BASIC_APP, "app")])
    def test_retired_basic_destinations_reach_unified_categories(self, make_dialog, alias, category):
        dialog, _store = make_dialog()
        dialog.select_destination(alias)
        assert dialog.category_rail.current_key() == category
        assert dialog.basic_tabs.isHidden()
        dialog.close()

    def test_section_can_be_opened_and_closed_with_keyboard(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        section = dialog.sections.sections[GENERAL]
        assert GENERAL not in dialog._built_pages
        section.header.setFocus()
        QTest.keyClick(section.header, Qt.Key.Key_Space)
        assert section.is_expanded() and GENERAL in dialog._built_pages
        assert dialog.auto_paste_check.isVisible()
        QTest.keyClick(section.header, Qt.Key.Key_Space)
        assert not section.is_expanded() and not dialog.auto_paste_check.isVisible()
        dialog.close()

    def test_existing_save_handlers_and_callbacks_survive_grouping(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.STREAMING_ENABLED: False, SettingsKey.STREAMING_OVERLAY_ENABLED: True})
        dialog.on_streaming_settings_changed = MagicMock()
        dialog.streaming_enabled_check.setChecked(True)
        assert store.get(SettingsKey.STREAMING_ENABLED) is True
        assert SettingsKey.STREAMING_OVERLAY_ENABLED not in store.load_all_settings()
        dialog.on_streaming_settings_changed.assert_called_once_with()
        dialog.select_destination(APP_GENERAL)
        dialog.update_check_check.setChecked(False)
        assert store.get(SettingsKey.UPDATE_CHECK_ENABLED) is False
        assert not dialog.update_notify_tile.isEnabled()
        dialog.close()

    def test_search_reveals_nested_model_options_without_changing_the_prompt(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True, SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "Keep my terminology."})
        dialog.show()
        dialog.select_destination(CLEANUP)
        options = dialog.models.cleanup_options_section
        assert not options.is_expanded()
        dialog._on_search_activated(SearchEntry(SETTING, "Custom prompt", "", CLEANUP, target_name="cleanup_prompt_tile"))
        QApplication.processEvents()
        assert options.is_expanded()
        assert dialog.cleanup_prompt_edit.isVisible()
        assert dialog.cleanup_prompt_tile.property("searchHit") is True
        assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT) == "Keep my terminology."
        dialog.close()

    def test_disabled_cleanup_search_focuses_its_enable_switch(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False})
        dialog.show()
        dialog._on_search_activated(SearchEntry(SETTING, "Custom prompt", "", CLEANUP, target_name="cleanup_prompt_tile"))
        QApplication.processEvents()
        assert not dialog.cleanup_model_tile.isVisible()
        assert not dialog.cleanup_level_tile.isVisible()
        assert dialog.sections.sections[CLEANUP].property("searchHit") is True
        assert not store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED)
        dialog.close()

    def test_speech_deep_link_opens_both_disclosures(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.sections.sections[RECORDING].set_expanded(False)
        dialog.select_destination(VOICE_MODEL)
        assert dialog.sections.sections[RECORDING].is_expanded()
        assert dialog.sections.sections[VOICE_MODEL].is_expanded()
        assert dialog.category_rail.current_key() == "dictation"
        dialog.close()

    @pytest.mark.parametrize("provider,phrase", [("openrouter", "is sent to OpenRouter"), ("ollama", "stays on this computer")])
    def test_provider_privacy_is_visible_outside_model_options(self, make_dialog, provider, phrase):
        dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True, SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: provider, SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2" if provider == "ollama" else "openrouter/free"})
        dialog.show()
        dialog.select_destination(CLEANUP)
        assert dialog.models.cleanup_privacy_note.isVisible()
        assert phrase in dialog.models.cleanup_privacy_note.text()
        assert not dialog.models.cleanup_options_section.is_expanded()
        dialog.close()

    def test_busy_backup_locks_other_categories_and_disclosures(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        dialog._on_backup_busy_changed(True)
        assert dialog.category_rail.current_key() == "app"
        assert not dialog.category_rail.isEnabled()
        assert not dialog.sections.sections[GENERAL].header.isEnabled()
        assert not dialog.sections.sections[GENERAL].body.isEnabled()
        assert dialog.sections.sections["backup"].body.isEnabled()
        dialog.select_destination(CLEANUP)
        assert dialog.rail.current_key() == "backup"
        dialog._on_backup_busy_changed(False)
        assert dialog.category_rail.isEnabled()
        assert dialog.sections.sections[GENERAL].header.isEnabled()
        dialog.close()

    def test_visible_cleanup_picker_loads_when_another_section_is_active(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
        dialog.show()
        dialog.select_destination(CLEANUP)
        dialog.select_destination(RECORDING)
        assert dialog.models.text_model_picker.isVisible()
        assert dialog.rail.current_key() == RECORDING
        with patch.object(dialog.models, "_fetch_catalog_models") as fetch:
            dialog.models._on_text_provider_changed("ollama")
        fetch.assert_called_once_with("ollama", picker=dialog.models.text_model_picker)
        dialog.close()

    def test_catalog_errors_stay_visible_with_model_options_closed(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
        dialog.show()
        dialog.select_destination(CLEANUP)
        picker = dialog.models.text_model_picker
        assert not dialog.models.cleanup_options_section.is_expanded()
        dialog.models._apply_catalog_to_picker(picker, picker.provider, picker.current_sort(), [], "Server unavailable")
        assert picker.error_label.isVisible()
        assert "Server unavailable" in picker.error_label.text()
        dialog.models._apply_catalog_to_picker(picker, picker.provider, picker.current_sort(), [], "")
        assert picker.error_label.isHidden()
        dialog.close()


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

    def test_text_alias_scrolls_to_the_cleanup_chat_model(self):
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


class TestOverview:
    def test_cards_repeat_what_each_destination_reports(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.MEETING_LLM_PROVIDER: "openai",
            SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
            SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OFF,
        }, cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)})
        dialog.refresh_models()
        dialog.select_destination(OVERVIEW)
        cards = dialog.overview.cards

        assert cards["voice_model"].value_label.text() == dialog.models.voice_summary()
        assert cards["cleanup"].value_label.text() == "Off"
        assert cards["meeting_intelligence"].value_label.text() == "gpt-4o-mini"
        assert "Me / Others labels" in cards["meeting_voice"].detail_label.text()
        assert cards["downloads"].value_label.text() == "145 MB"
        assert "1 of " in cards["downloads"].detail_label.text()

    def test_clicking_a_card_opens_its_destination(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        card = dialog.overview.cards["meeting_intelligence"]
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        assert dialog.rail.current_key() == MEETING_INTELLIGENCE

        dialog.select_destination(OVERVIEW)
        dialog.overview.cards["components"].clicked.emit("components")
        assert dialog.rail.current_key() == DOWNLOADS
        dialog.close()

    def test_where_strip_places_a_remote_cleanup_provider_in_the_cloud(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free",
        })
        dialog.select_destination(OVERVIEW)
        assert "AI cleanup" in dialog.overview.cloud_list.text()
        assert "AI cleanup" not in dialog.overview.local_list.text()

    def test_where_strip_keeps_a_local_cleanup_provider_on_this_computer(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2",
        })
        dialog.select_destination(OVERVIEW)
        assert "AI cleanup" in dialog.overview.local_list.text()
        assert "AI cleanup" not in dialog.overview.cloud_list.text()


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
