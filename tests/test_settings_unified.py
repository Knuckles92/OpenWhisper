"""The unified Settings window: routing, the Overview, and search.

Settings hosts what used to be three windows. These tests pin the parts that
only exist because of that: legacy destination names, the Overview landing
page, the Downloads rail value, and the Ctrl+K palette across every page.
"""
import os
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

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
    DOWNLOADS,
    GENERAL,
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
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_dialog():
    """Build Settings against a throwaway store with a fixed model cache."""
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, cached=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings(values or {})
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
            get_loaded_model=lambda: None, background_cache_scan=False
        )
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


class TestRouting:
    def test_window_opens_on_the_overview(self, make_dialog):
        dialog, _store = make_dialog()
        assert dialog.rail.current_key() == OVERVIEW
        assert dialog.page_title.text() == "Overview"
        assert dialog.rail.value(OVERVIEW) == "What is running now"

    def test_refresh_rebuilds_the_overview_once(self, make_dialog):
        dialog, _store = make_dialog()
        with patch.object(
            dialog, "_refresh_overview", wraps=dialog._refresh_overview
        ) as overview:
            dialog.refresh()
        # Model and download refreshes each ask for a rail redraw; the
        # Overview behind it is rebuilt once, after they all finish.
        assert overview.call_count == 1

    @pytest.mark.parametrize("alias, destination", [
        ("ondemand", VOICE_MODEL),
        ("text", CLEANUP),
        ("meeting", MEETING_VOICE),
        ("runtime", RUNTIME),
        ("downloads", DOWNLOADS),
        ("library", DOWNLOADS),
        ("voice", DOWNLOADS),
        ("engine_downloads", DOWNLOADS),
        (GENERAL, GENERAL),
    ])
    def test_legacy_model_manager_names_land_on_a_destination(
        self, make_dialog, alias, destination
    ):
        assert resolve_destination(alias) == destination
        dialog, _store = make_dialog()
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

    def test_cleanup_rail_combines_the_switch_and_the_hosted_model(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-test",
        })
        assert dialog.rail.value(CLEANUP) == "On · gpt-test"
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
