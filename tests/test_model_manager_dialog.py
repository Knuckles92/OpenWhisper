"""Qt tests for the Model Manager dialog's rail, destinations, and pickers.

Catalog, download, and component behavior moved to ``test_downloads_dialog``.
"""
import pytest
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QScrollArea

from services.hf_access import CachedModelInfo
from services.settings import (
    MeetingAgentCore,
    MeetingSpeakerIdBackend,
    SettingsKey,
    SettingsManager,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
)
from ui_qt.dialogs import model_manager_dialog as dialog_module
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs.model_manager_dialog import (
    MEETING_TEXT,
    MEETING_VOICE,
    ONDEMAND_TEXT,
    ONDEMAND_VOICE,
    SHARED_RUNTIME,
    ModelManagerDialog,
)
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets import text_model_picker as picker_module


def _cached(repo_id, size_bytes):
    return CachedModelInfo(
        repo_id=repo_id,
        size_bytes=size_bytes,
        path=f"/hub/models--{repo_id.replace('/', '--')}",
        revision_hashes=("abc",),
    )


BASE_REPO = "Systran/faster-whisper-base"
TINY_REPO = "Systran/faster-whisper-tiny"


class _FakeSettings:
    """In-memory settings store that matches the Model Manager call surface."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save_setting(self, key, value):
        self.values[key] = value

    def load_all_settings(self):
        return dict(self.values)

    def save_all_settings(self, settings):
        self.values.clear()
        self.values.update(settings)

    def load_model_selection(self):
        return self.values.get(SettingsKey.SELECTED_MODEL, "local_whisper")


class _DialogTestCase:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _qapp(cls):
        cls.app = QApplication.instance() or QApplication([])

    @pytest.fixture(autouse=True)
    def _patch_stack(self):
        """Hold the fake settings store for the whole test, not just __init__.

        Every assignment in this dialog writes on change, so releasing the
        patch after construction would send those writes to the real settings
        file and make the persistence assertions read a store nobody wrote to.
        """
        self._started = []
        yield
        for patcher in reversed(self._started):
            patcher.stop()

    def _settings_values(self, active_model="base", extra=None):
        values = {
            SettingsKey.WHISPER_MODEL: active_model,
            SettingsKey.WHISPER_DEVICE: "auto",
            SettingsKey.WHISPER_COMPUTE_TYPE: "auto",
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.MEETING_WHISPER_MODEL: "auto",
            SettingsKey.MEETING_LANGUAGE: "auto",
            SettingsKey.MEETING_LLM_PROVIDER: "openrouter",
            SettingsKey.MEETING_LLM_MODEL: "deepseek/test-model",
            SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.DIRECT,
            SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.LOCAL,
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-test",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                TranscriptCleanupModelSort.ALPHABETICAL
            ),
        }
        if extra:
            values.update(extra)
        return values

    def _make_dialog(
        self,
        cached=None,
        active_model="base",
        loaded_model=None,
        extra_settings=None,
        api_keys=None,
    ):
        values = self._settings_values(active_model, extra_settings)
        fake_settings = _FakeSettings(values)
        patchers = [
            patch.object(
                dialog_module, "scan_cached_models", return_value=cached or {}
            ),
            patch.object(dialog_module, "settings_manager", fake_settings),
            patch.object(
                picker_module,
                "find_api_key",
                side_effect=lambda selected_provider: (api_keys or {}).get(
                    selected_provider
                ),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self._started.append(patcher)
        return ModelManagerDialog(get_loaded_model=lambda: loaded_model), values


class TestDialogShell(_DialogTestCase):
    """The window is a fixed shell: every destination fits, nothing scrolls."""

    def test_dialog_uses_resizable_default_that_fits_every_destination(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            self.app.processEvents()

            assert (dialog.width(), dialog.height()) == (980, 660)
            assert (dialog.minimumWidth(), dialog.minimumHeight()) == (840, 620)
            assert dialog.isSizeGripEnabled()
            assert dialog.minimumSizeHint().height() <= dialog.height()

            dialog.resize(900, 640)
            self.app.processEvents()
            assert (dialog.width(), dialog.height()) == (900, 640)
        finally:
            self.app.setStyleSheet(previous_stylesheet)

    def test_no_destination_is_wrapped_in_a_scroll_area(self):
        """The former design paid for its height with a page-level scroller."""
        dialog, _values = self._make_dialog()
        assert dialog.findChildren(QScrollArea) == []

    def test_every_destination_fits_the_default_height(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            self.app.processEvents()

            for key in dialog.rail.keys():
                dialog.rail.select(key)
                self.app.processEvents()
                page = dialog._pages[key]
                assert page.sizeHint().height() <= page.height(), key
        finally:
            self.app.setStyleSheet(previous_stylesheet)


class TestRail(_DialogTestCase):
    """The rail lists every assignable thing and reports its current value."""

    def test_rail_offers_five_grouped_destinations(self):
        dialog, _values = self._make_dialog()
        assert dialog.rail.keys() == (
            ONDEMAND_VOICE,
            ONDEMAND_TEXT,
            MEETING_VOICE,
            MEETING_TEXT,
            SHARED_RUNTIME,
        )
        assert dialog.stack.count() == 5
        assert dialog.rail.current_key() == ONDEMAND_VOICE

    def test_rail_items_show_the_value_each_destination_owns(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        assert dialog.rail.value(ONDEMAND_VOICE) == "Local Whisper · base"
        assert dialog.rail.value(ONDEMAND_TEXT) == "OpenAI · gpt-test"
        assert dialog.rail.value(MEETING_VOICE) == "auto"
        assert dialog.rail.value(MEETING_TEXT) == (
            "OpenRouter · deepseek/test-model"
        )
        assert dialog.rail.value(SHARED_RUNTIME) == "auto · auto"

    def test_rail_value_follows_a_new_assignment(self):
        dialog, values = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)},
            active_model="base",
        )
        # Persisting the choice is the controller's job; the dialog re-reads it.
        dialog.on_set_active_requested = lambda name: values.__setitem__(
            SettingsKey.WHISPER_MODEL, name
        )
        index = dialog.ondemand_whisper_picker.model_combo.findData("tiny")
        dialog.ondemand_whisper_picker.model_combo.setCurrentIndex(index)

        assert dialog.rail.value(ONDEMAND_VOICE) == "Local Whisper · tiny"

    def test_cloud_engine_reports_itself_instead_of_a_whisper_size(self):
        dialog, _values = self._make_dialog()
        dialog.engine_combo.setCurrentIndex(
            dialog.engine_combo.findData("api_whisper")
        )
        assert dialog.rail.value(ONDEMAND_VOICE) == "API: Whisper"

    def test_rail_footer_summarizes_the_cache_next_to_downloads(self):
        dialog, _values = self._make_dialog(
            cached={
                BASE_REPO: _cached(BASE_REPO, 145_000_000),
                TINY_REPO: _cached(TINY_REPO, 76_000_000),
            }
        )
        text = dialog.cache_summary_label.text()
        assert text.startswith("2 of ")
        assert "221 MB" in text

    def test_downloads_button_asks_the_controller_to_open_that_window(self):
        dialog, _values = self._make_dialog()
        opened = []
        dialog.downloads_requested.connect(lambda: opened.append(True))

        dialog.downloads_button.click()

        assert opened == [True]

    def test_manage_downloads_link_reuses_the_same_request(self):
        dialog, _values = self._make_dialog()
        opened = []
        dialog.downloads_requested.connect(lambda: opened.append(True))

        dialog.ondemand_whisper_picker.manage_button.click()
        dialog.meeting_whisper_picker.manage_button.click()

        assert opened == [True, True]


class TestDestinationNavigation(_DialogTestCase):
    """Deep links land on a destination and retitle the page."""

    def test_show_text_selects_the_cleanup_destination(self):
        dialog, _values = self._make_dialog()
        dialog.show_text_tab()

        assert dialog.rail.current_key() == ONDEMAND_TEXT
        assert dialog.stack.currentWidget() is dialog._pages[ONDEMAND_TEXT]
        assert dialog.page_title.text() == "On-demand text cleanup"

    def test_show_meeting_selects_meeting_voice(self):
        dialog, _values = self._make_dialog()
        dialog.show_meeting_tab()

        assert dialog.rail.current_key() == MEETING_VOICE
        assert dialog.page_title.text() == "Meeting voice"

    def test_show_runtime_selects_shared_runtime(self):
        dialog, _values = self._make_dialog()
        dialog.show_runtime()

        assert dialog.rail.current_key() == SHARED_RUNTIME
        assert dialog.page_title.text() == "Shared runtime"

    def test_clicking_the_rail_swaps_the_page(self):
        dialog, _values = self._make_dialog()
        dialog.rail.select(MEETING_TEXT)

        assert dialog.stack.currentWidget() is dialog._pages[MEETING_TEXT]
        assert dialog.rail.is_selected(MEETING_TEXT)
        assert not dialog.rail.is_selected(ONDEMAND_VOICE)


class TestTextModelPicker(_DialogTestCase):
    """One stacked endpoint-then-model control per text destination."""

    def _make_text_dialog(
        self, provider="openai", model="gpt-test", api_keys=None
    ):
        return self._make_dialog(
            extra_settings={
                SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: provider,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: model,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                    TranscriptCleanupModelSort.MOST_POPULAR
                ),
            },
            api_keys=api_keys,
        )

    def test_provider_credential_status_shows_when_key_is_found(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER,
            api_keys={TranscriptCleanupProvider.OPENROUTER: "test-key"},
        )

        status = dialog.text_model_picker.provider_requirement
        assert status.text() == "OPENROUTER_API_KEY found"
        assert status.property("available")

    def test_provider_credential_status_keeps_requirement_when_key_is_missing(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER
        )

        status = dialog.text_model_picker.provider_requirement
        assert status.text() == "Requires OPENROUTER_API_KEY"
        assert not status.property("available")

    def test_picker_is_one_column_of_labeled_fields(self):
        dialog, _values = self._make_text_dialog()
        picker = dialog.text_model_picker

        assert picker.active_summary_card.minimumHeight() >= 56
        assert not picker.refresh_button.icon().isNull()
        assert picker.provider_combo.minimumSizeHint().width() <= 170

    def test_text_models_use_one_labeled_provider_selector(self):
        dialog, _values = self._make_text_dialog()
        picker = dialog.text_model_picker
        labels = [
            picker.provider_combo.itemText(i)
            for i in range(picker.provider_combo.count())
        ]
        assert labels == ["OpenAI", "OpenRouter"]
        assert picker.provider == TranscriptCleanupProvider.OPENAI
        assert picker.active_summary.text() == "Active now: OpenAI · gpt-test"

    def test_switching_provider_updates_the_same_model_picker(self):
        dialog, _values = self._make_text_dialog()
        picker = dialog.text_model_picker

        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENROUTER
        )
        picker.provider_combo.setCurrentIndex(index)

        assert picker.provider == TranscriptCleanupProvider.OPENROUTER
        assert picker.provider_combo.currentText() == "OpenRouter"
        assert picker.model_combo.currentText() == default_transcript_cleanup_model(
            "openrouter"
        )
        assert not picker.sort_combo.isHidden()

    def test_active_line_stays_readable_while_browsing_another_endpoint(self):
        """Browsing an endpoint must not hide which model is actually in use."""
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER,
            model="openrouter/free",
        )
        picker = dialog.text_model_picker
        expected = "Active now: OpenRouter · openrouter/free"

        assert picker.active_summary.text() == expected
        assert picker.active_summary_card.property("matches")

        picker.provider_combo.setCurrentIndex(
            picker.provider_combo.findData(TranscriptCleanupProvider.OPENAI)
        )

        assert picker.active_summary.text() == expected
        assert not picker.active_summary_card.property("matches")

        picker.provider_combo.setCurrentIndex(
            picker.provider_combo.findData(TranscriptCleanupProvider.OPENROUTER)
        )
        assert picker.active_summary_card.property("matches")

    def test_set_active_persists_provider_and_model(self):
        dialog, values = self._make_text_dialog()
        picker = dialog.text_model_picker
        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENROUTER
        )
        picker.provider_combo.setCurrentIndex(index)
        picker.model_combo.setCurrentText("anthropic/claude-test")

        dialog._activate_text_model(TranscriptCleanupProvider.OPENROUTER)

        assert values[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "openrouter"
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "anthropic/claude-test"
        assert picker.activate_button.text() == "Active"
        assert not picker.activate_button.isEnabled()
        assert dialog.rail.value(ONDEMAND_TEXT) == (
            "OpenRouter · anthropic/claude-test"
        )

    def test_custom_endpoint_appears_and_activation_persists(self):
        dialog, values = self._make_dialog(
            extra_settings={
                SettingsKey.TEXT_LLM_PROFILES: [
                    {
                        "id": "custom_abcd1234",
                        "name": "LM Studio",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "api_key_env": "",
                    }
                ],
                SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "local-qwen",
            }
        )
        picker = dialog.text_model_picker
        labels = [
            picker.provider_combo.itemText(i)
            for i in range(picker.provider_combo.count())
        ]
        assert "LM Studio" in labels
        assert picker.provider == "custom_abcd1234"
        assert picker.provider_url.toolTip() == "http://127.0.0.1:1234/v1"
        assert picker.provider_requirement.text() == "No API key required"
        picker.model_combo.setCurrentText("other-local")
        dialog._activate_text_model("custom_abcd1234")
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "custom_abcd1234"
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "other-local"

    def test_assigned_custom_endpoint_cannot_be_deleted(self):
        dialog, values = self._make_dialog(
            extra_settings={
                SettingsKey.TEXT_LLM_PROFILES: [
                    {
                        "id": "custom_abcd1234",
                        "name": "LM Studio",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "api_key_env": "",
                    }
                ],
                SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "local-qwen",
            }
        )
        dialog._delete_text_endpoint("custom_abcd1234")
        assert "in use" in dialog.message_label.text()
        assert len(values[SettingsKey.TEXT_LLM_PROFILES]) == 1

    def test_catalog_result_populates_matching_provider(self):
        dialog, _values = self._make_text_dialog(model="gpt-4o-mini")
        models = ["gpt-4.1", "gpt-4o-mini", "o4-mini"]

        dialog._on_text_models_loaded(
            TranscriptCleanupProvider.OPENAI,
            TranscriptCleanupModelSort.ALPHABETICAL,
            models,
            "",
        )

        picker = dialog.text_model_picker
        assert picker.model_combo.count() == len(models)
        assert picker.model_combo.currentText() == "gpt-4o-mini"
        assert picker.status_label.text() == "3 models available"

    def test_opening_a_text_destination_loads_that_provider_catalog(self):
        dialog, _values = self._make_text_dialog()
        requests = []
        dialog._fetch_catalog_models = lambda provider, picker, force=False: (
            requests.append((provider, picker))
        )

        dialog.rail.select(ONDEMAND_TEXT)
        dialog.rail.select(MEETING_TEXT)

        assert requests[0][1] is dialog.text_model_picker
        assert requests[1][1] is dialog.meeting_model_picker


class TestMeetingDestinations(_DialogTestCase):
    """Meeting voice and intelligence own their own model choices."""

    def _make_meeting_dialog(
        self,
        whisper="auto",
        provider="openrouter",
        model="deepseek/test-model",
        cached=None,
        extra=None,
    ):
        settings = {
            SettingsKey.MEETING_WHISPER_MODEL: whisper,
            SettingsKey.MEETING_LLM_PROVIDER: provider,
            SettingsKey.MEETING_LLM_MODEL: model,
        }
        if extra:
            settings.update(extra)
        return self._make_dialog(cached=cached, extra_settings=settings)

    def test_meeting_picker_offers_auto(self):
        dialog, _values = self._make_meeting_dialog()
        names = [
            dialog.meeting_whisper_picker.model_combo.itemData(i)
            for i in range(dialog.meeting_whisper_picker.model_combo.count())
        ]
        assert "auto" in names

    def test_meeting_picker_persists_whisper_model(self):
        dialog, values = self._make_meeting_dialog(
            whisper="auto",
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)},
        )
        index = dialog.meeting_whisper_picker.model_combo.findData("tiny")
        dialog.meeting_whisper_picker.model_combo.setCurrentIndex(index)

        assert values[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"
        assert dialog.rail.value(MEETING_VOICE) == "tiny"

    def test_meeting_llm_activation_persists_provider_and_model(self):
        dialog, values = self._make_meeting_dialog()
        picker = dialog.meeting_model_picker
        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENAI
        )
        picker.provider_combo.setCurrentIndex(index)
        picker.model_combo.setCurrentText("gpt-4o-mini")

        dialog._activate_meeting_llm_model(TranscriptCleanupProvider.OPENAI)

        assert values[SettingsKey.MEETING_LLM_PROVIDER] == "openai"
        assert values[SettingsKey.MEETING_LLM_MODEL] == "gpt-4o-mini"
        assert picker.activate_button.text() == "Active"
        assert dialog.rail.value(MEETING_TEXT) == "OpenAI · gpt-4o-mini"

    def test_meeting_custom_endpoint_activation_persists(self):
        dialog, values = self._make_meeting_dialog(
            provider="custom_abcd1234",
            model="local-qwen",
            extra={
                SettingsKey.TEXT_LLM_PROFILES: [
                    {
                        "id": "custom_abcd1234",
                        "name": "LM Studio",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "api_key_env": "",
                    }
                ]
            },
        )
        picker = dialog.meeting_model_picker
        assert picker.provider == "custom_abcd1234"
        assert picker.provider_url.toolTip() == "http://127.0.0.1:1234/v1"
        picker.model_combo.setCurrentText("other-local")
        dialog._activate_meeting_llm_model("custom_abcd1234")
        assert values[SettingsKey.MEETING_LLM_PROVIDER] == "custom_abcd1234"
        assert values[SettingsKey.MEETING_LLM_MODEL] == "other-local"

    def test_meeting_language_and_core_persist(self):
        dialog, values = self._make_meeting_dialog()
        language_index = dialog.meeting_language_combo.findData("en")
        dialog.meeting_language_combo.setCurrentIndex(language_index)
        core_index = dialog.meeting_agent_core_combo.findData(
            MeetingAgentCore.DIRECT
        )
        dialog.meeting_agent_core_combo.setCurrentIndex(core_index)

        assert values[SettingsKey.MEETING_LANGUAGE] == "en"
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.DIRECT

    def test_speaker_id_status_points_at_downloads_when_not_installed(self):
        dialog, _values = self._make_meeting_dialog()
        with patch.object(
            dialog_module.component_coordinator,
            "describe",
            side_effect=RuntimeError("unavailable"),
        ):
            dialog.refresh_component_state()
        assert "Downloads" in dialog.speaker_id_status.text()

    def test_refresh_component_state_enables_pi_after_install(self):
        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value=None
        ):
            dialog, _values = self._make_meeting_dialog()
        item = dialog.meeting_agent_core_combo.model().item(0)
        assert item is not None
        assert not item.isEnabled()
        assert "not built" in dialog.meeting_agent_core_combo.itemText(0)

        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value="C:/payload"
        ):
            dialog.refresh_component_state()
        assert item.isEnabled()
        assert dialog.meeting_agent_core_combo.itemText(0) == "Pi (sidecar)"

    def test_refresh_component_state_restores_saved_pi_core(self):
        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value=None
        ):
            dialog, _values = self._make_meeting_dialog(
                extra={SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.PI}
            )
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.DIRECT

        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value="C:/payload"
        ):
            dialog.refresh_component_state()
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.PI


class TestSharedRuntime(_DialogTestCase):
    """Device and quantization are shared, and say so on both surfaces."""

    def test_runtime_change_persists_and_reloads_the_engine(self):
        dialog, values = self._make_dialog()
        reloads = []
        dialog.on_runtime_settings_changed = lambda: reloads.append(True)

        dialog.compute_combo.setCurrentText("int8")

        assert values[SettingsKey.WHISPER_COMPUTE_TYPE] == "int8"
        assert reloads == [True]
        assert dialog.rail.value(SHARED_RUNTIME) == "auto · int8"

    def test_meeting_voice_names_the_shared_runtime_destination(self):
        dialog, _values = self._make_dialog()
        assert "Shared" in dialog.meeting_runtime_label.text()
        assert "auto · auto" in dialog.meeting_runtime_label.text()


class TestOnDemandEngine(_DialogTestCase):
    """On-demand recording engine routes through the main-window path."""

    def test_engine_combo_invokes_backend_callback(self):
        dialog, _values = self._make_dialog()
        requested = []
        dialog.on_backend_changed = requested.append
        index = dialog.engine_combo.findData("api_whisper")
        dialog.engine_combo.setCurrentIndex(index)
        assert requested == ["API: Whisper"]
        assert not dialog.ondemand_whisper_picker.isEnabled()


class TestCleanupSettingsOwnership(_DialogTestCase):
    """Cleanup Settings must not overwrite Model Manager selections."""

    def test_saving_cleanup_settings_preserves_text_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "provider/model-test",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                        TranscriptCleanupModelSort.NEWEST
                    ),
                }
            )
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                next_index = (dialog.cleanup_reasoning_combo.currentIndex() + 1) % (
                    dialog.cleanup_reasoning_combo.count()
                )
                dialog.cleanup_reasoning_combo.setCurrentIndex(next_index)

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "openrouter"
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "provider/model-test"
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT] == TranscriptCleanupModelSort.NEWEST

    def test_cleanup_tab_links_to_model_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                requested = []
                dialog.model_manager_requested.connect(requested.append)

                dialog.open_model_manager_btn.click()

                assert requested == ["text"]
                assert dialog.result() != dialog.DialogCode.Accepted

    def test_saving_meeting_settings_preserves_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.MEETING_WHISPER_MODEL: "tiny",
                    SettingsKey.MEETING_LLM_PROVIDER: "openai",
                    SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
                    SettingsKey.MEETING_LANGUAGE: "fr",
                    SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.DIRECT,
                    SettingsKey.MEETING_SPEAKER_ID_BACKEND: (
                        MeetingSpeakerIdBackend.OPENAI
                    ),
                    SettingsKey.TEXT_LLM_PROFILES: [
                        {
                            "id": "custom_abcd1234",
                            "name": "LM Studio",
                            "base_url": "http://127.0.0.1:1234/v1",
                            "api_key_env": "",
                        }
                    ],
                }
            )
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                summary = dialog.meeting_model_summary.text()
                assert "Whisper · tiny" in summary
                assert "Spoken language · French" in summary
                assert "OpenAI · gpt-4o-mini" in summary
                assert "Agent core · Direct" in summary
                assert "Speaker ID · OpenAI" in summary
                dialog.meeting_end_polish_check.setChecked(
                    not dialog.meeting_end_polish_check.isChecked()
                )

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"
            assert saved[SettingsKey.MEETING_LLM_PROVIDER] == "openai"
            assert saved[SettingsKey.MEETING_LLM_MODEL] == "gpt-4o-mini"
            assert saved[SettingsKey.MEETING_LANGUAGE] == "fr"
            assert saved[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.DIRECT
            assert saved[SettingsKey.MEETING_SPEAKER_ID_BACKEND] == MeetingSpeakerIdBackend.OPENAI
            assert saved[SettingsKey.TEXT_LLM_PROFILES][0]["id"] == "custom_abcd1234"

    def test_meeting_tab_links_to_model_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                requested = []
                dialog.model_manager_requested.connect(requested.append)

                dialog.open_meeting_model_manager_btn.click()

                assert requested == ["meeting"]
                assert dialog.result() != dialog.DialogCode.Accepted
