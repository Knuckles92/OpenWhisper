"""Qt tests for the model assignments Settings hosts on its feature pages.

``ModelAssignments`` builds Voice model, Voice & speakers, Runtime, and the
chat-model sections of AI cleanup and Intelligence. These tests host it in a
minimal rail harness; ``test_settings_unified`` covers the real window.
Catalog, download, and component behavior lives in ``test_settings_downloads``.
"""
import gc
import pytest
import os
import tempfile
import threading
import weakref
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from services import installed_agents
from services.hf_access import CachedModelInfo
from services.installed_agents import AgentModel, InstalledAgent
from services.settings import (
    MeetingAgentCore,
    MeetingSpeakerIdBackend,
    SettingsKey,
    SettingsManager,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
)
from tests.fakes.settings import InMemorySettings
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as dialog_module
from ui_qt.dialogs.settings_destinations import (
    CLEANUP,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    RUNTIME,
    VOICE_MODEL,
)
from ui_qt.dialogs.settings_models import WHISPER_FILTER, ModelAssignments
from ui_qt.widgets import agent_picker
from ui_qt.widgets import text_model_picker as picker_module
from ui_qt.widgets.nav_rail import NavRail

# The rail keys these tests used when Model Manager was its own window.
ONDEMAND_VOICE = VOICE_MODEL
ONDEMAND_TEXT = CLEANUP
MEETING_TEXT = MEETING_INTELLIGENCE
SHARED_RUNTIME = RUNTIME

CLAUDE = InstalledAgent("claude_code", "C:/bin/claude.exe", "2.1.281",
                        "Claude Team", True)
CODEX = InstalledAgent("codex", "C:/bin/codex.exe", "0.158.0", "ChatGPT", True)


class _Host(QWidget):
    """The smallest stand-in for Settings: a rail, a message line, pages."""

    def __init__(self, get_loaded_model):
        super().__init__()
        self.rail = NavRail(self)
        for key in (VOICE_MODEL, CLEANUP, MEETING_VOICE, MEETING_INTELLIGENCE, RUNTIME):
            self.rail.add_destination(key, key)
        self.message_label = QLabel(self)
        self.models = ModelAssignments(
            self,
            self.rail,
            self.message_label,
            get_loaded_model=get_loaded_model,
            background_cache_scan=False,
        )
        self.pages = {}
        for key, builder in (
            (VOICE_MODEL, self.models.build_voice_page),
            (CLEANUP, self.models.build_cleanup_model_section),
            (MEETING_VOICE, self.models.build_meeting_voice_page),
            (MEETING_INTELLIGENCE, self.models.build_meeting_model_section),
            (RUNTIME, self.models.build_runtime_page),
        ):
            page = QWidget(self)
            builder(QVBoxLayout(page))
            self.pages[key] = page
        self.rail.destination_changed.connect(self.models.on_destination_shown)
        self.models.host = self


def _cached(repo_id, size_bytes):
    return CachedModelInfo(
        repo_id=repo_id,
        size_bytes=size_bytes,
        path=f"/hub/models--{repo_id.replace('/', '--')}",
        revision_hashes=("abc",),
    )


BASE_REPO = "Systran/faster-whisper-base"
TINY_REPO = "Systran/faster-whisper-tiny"


def _isolated_settings(isolated):
    """Point every Settings module at one throwaway store."""
    from contextlib import ExitStack

    stack = ExitStack()
    for module in (settings_dialog_module, dialog_module, downloads_module):
        stack.enter_context(patch.object(module, "settings_manager", isolated))
    stack.enter_context(
        patch.object(settings_dialog_module.history_manager, "set_retention")
    )
    stack.enter_context(
        patch.object(dialog_module, "peek_cached_models", return_value={})
    )
    stack.enter_context(
        patch.object(downloads_module, "scan_cached_models", return_value={})
    )
    stack.enter_context(
        patch.object(dialog_module, "scan_cached_models", return_value={})
    )
    return stack


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
            SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.PI,
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
        fake_settings = InMemorySettings(values)
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
        host = _Host(lambda: loaded_model)
        host.models.refresh()
        # Tests talk to the assignments directly; the host stays alive with them.
        return host.models, values


class TestRail(_DialogTestCase):
    """The rail lists every assignable thing and reports its current value."""

    def test_registered_local_model_survives_assignment_refresh(self, tmp_path):
        for name, contents in (("model.bin", b"weights"), ("config.json", b"{}"),
                               ("tokenizer.json", b"{}")):
            (tmp_path / name).write_bytes(contents)
        model = str(tmp_path.resolve())
        dialog, _values = self._make_dialog(active_model=model, extra_settings={
            SettingsKey.CUSTOM_WHISPER_MODELS: [model],
            SettingsKey.MEETING_WHISPER_MODEL: model,
        })
        dialog.refresh()
        assert dialog.ondemand_whisper_picker.current_model() == model
        assert dialog.meeting_whisper_picker.current_model() == model
        assert dialog.engine_inventory_label.text().startswith("1 of ")

    def test_rail_items_show_the_value_each_destination_owns(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        assert dialog.rail.value(ONDEMAND_VOICE) == "Local Whisper · base"
        # AI cleanup's rail value also says whether cleanup is on, so Settings
        # composes it; the assignment reports the model half.
        assert dialog.text_summary() == "OpenAI · gpt-test"
        assert dialog.rail.value(MEETING_VOICE) == "auto · Detect automatically"
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
            dialog.engine_combo.findData("api")
        )
        assert dialog.rail.value(ONDEMAND_VOICE) == "API · gpt-transcribe"

    def test_manage_downloads_links_open_downloads_filtered(self):
        dialog, _values = self._make_dialog()
        opened = []
        dialog.downloads_requested.connect(opened.append)

        dialog.ondemand_whisper_picker.manage_button.click()
        dialog.meeting_whisper_picker.manage_button.click()

        assert opened == [WHISPER_FILTER, ""]

    def test_get_models_filters_downloads_to_the_selected_engine(self):
        dialog, _values = self._make_dialog(
            extra_settings={SettingsKey.SELECTED_MODEL: "parakeet"}
        )
        opened = []
        dialog.downloads_requested.connect(opened.append)

        dialog.speech_download_button.click()

        assert opened == ["parakeet"]

    def test_voice_page_says_what_the_engine_has_on_this_computer(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        text = dialog.engine_inventory_label.text()
        assert text.startswith("1 of ")
        assert "Whisper models on this computer" in text
        assert dialog.engine_inventory_row.isVisibleTo(dialog.host)

    def test_cloud_engine_hides_the_on_this_computer_row(self):
        dialog, _values = self._make_dialog(
            extra_settings={SettingsKey.SELECTED_MODEL: "api"}
        )
        assert not dialog.engine_inventory_row.isVisibleTo(dialog.host)


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
        assert status.text() == "OpenRouter API key found"
        assert status.property("available")

    def test_provider_credential_status_keeps_requirement_when_key_is_missing(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER
        )

        status = dialog.text_model_picker.provider_requirement
        assert status.text() == (
            "Requires OpenRouter API key — add it in Settings → API keys"
        )
        assert not status.property("available")

    def test_picker_is_one_column_of_labeled_fields(self):
        dialog, _values = self._make_text_dialog()
        picker = dialog.text_model_picker

        assert not hasattr(picker, "activate_button")
        assert not picker.refresh_button.icon().isNull()
        assert picker.provider_combo.minimumSizeHint().width() <= 170

    def test_text_models_use_one_labeled_provider_selector(self):
        dialog, _values = self._make_text_dialog()
        picker = dialog.text_model_picker
        labels = [
            picker.provider_combo.itemText(i)
            for i in range(picker.provider_combo.count())
        ]
        assert labels == ["OpenAI", "OpenRouter", "Ollama", "Groq", "OpenCode Go", "OpenCode Zen"]
        assert picker.provider == TranscriptCleanupProvider.OPENAI
        assert picker.model_combo.currentText() == "gpt-test"
        assert picker.model_combo.badge_text() == "Active"

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

    def test_badge_marks_only_the_endpoint_actually_in_use(self):
        """Browsing another endpoint shows no badge, and its tooltip says why."""
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER,
            model="openrouter/free",
        )
        picker = dialog.text_model_picker

        assert picker.model_combo.badge_text() == "Active"

        picker.provider_combo.setCurrentIndex(
            picker.provider_combo.findData(TranscriptCleanupProvider.OPENAI)
        )

        assert picker.model_combo.badge_text() == ""
        assert "OpenRouter · openrouter/free" in picker.model_combo.toolTip()

        picker.provider_combo.setCurrentIndex(
            picker.provider_combo.findData(TranscriptCleanupProvider.OPENROUTER)
        )
        assert picker.model_combo.badge_text() == "Active"

    def test_picking_a_model_persists_it_without_a_confirm_step(self):
        dialog, values = self._make_text_dialog()
        picker = dialog.text_model_picker
        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENROUTER
        )
        picker.provider_combo.setCurrentIndex(index)
        picker.model_combo.setCurrentText("anthropic/claude-test")
        picker.model_combo.textActivated.emit("anthropic/claude-test")

        assert values[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "openrouter"
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "anthropic/claude-test"
        assert picker.model_combo.badge_text() == "Active"
        assert dialog.text_summary() == "OpenRouter · anthropic/claude-test"

    def test_typed_model_id_persists_on_enter(self):
        dialog, values = self._make_text_dialog()
        picker = dialog.text_model_picker
        line_edit = picker.model_combo.lineEdit()

        line_edit.setText("gpt-typed")
        line_edit.textEdited.emit("gpt-typed")
        line_edit.returnPressed.emit()
        line_edit.editingFinished.emit()

        assert values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "gpt-typed"
        assert picker.model_combo.badge_text() == "Active"

    def test_search_fragment_is_not_saved_when_focus_leaves(self):
        """Typing filters the dropdown, so a fragment is not a model choice."""
        dialog, values = self._make_text_dialog(model="gpt-test")
        picker = dialog.text_model_picker
        line_edit = picker.model_combo.lineEdit()

        line_edit.setText("clau")
        line_edit.textEdited.emit("clau")
        line_edit.editingFinished.emit()

        assert values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "gpt-test"
        assert picker.model_combo.currentText() == "gpt-test"
        assert picker.model_combo.badge_text() == "Active"

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
        assert picker.model_combo.badge_text() == "Active"
        assert dialog.text_summary() == "OpenRouter · anthropic/claude-test"

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
        assert dialog.rail.value(MEETING_VOICE) == "tiny · Detect automatically"

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
        assert picker.model_combo.badge_text() == "Active"
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
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value="C:/payload"):
            dialog, values = self._make_meeting_dialog()
        language_index = dialog.meeting_language_combo.findData("en")
        dialog.meeting_language_combo.setCurrentIndex(language_index)
        core_index = dialog.meeting_agent_core_combo.findData(
            MeetingAgentCore.OPENCODE
        )
        dialog.meeting_agent_core_combo.setCurrentIndex(core_index)

        assert values[SettingsKey.MEETING_LANGUAGE] == "en"
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.OPENCODE
        assert dialog.meeting_agent_picker.tiles[agent_picker.BUILTIN].name_label.text() == "OpenCode SDK"
        assert dialog.rail.value(MEETING_VOICE).endswith("· English")

    def test_speaker_id_combo_includes_off_and_persists(self):
        dialog, values = self._make_meeting_dialog()
        combo = dialog.meeting_speaker_id_combo
        backends = [combo.itemData(i) for i in range(combo.count())]
        assert backends == [
            MeetingSpeakerIdBackend.OFF,
            MeetingSpeakerIdBackend.LOCAL,
            MeetingSpeakerIdBackend.OPENAI,
        ]
        off_index = combo.findData(MeetingSpeakerIdBackend.OFF)
        combo.setCurrentIndex(off_index)
        assert values[SettingsKey.MEETING_SPEAKER_ID_BACKEND] == (
            MeetingSpeakerIdBackend.OFF
        )
        status = dialog.speaker_id_status.text()
        assert "Me" in status
        assert "Others" in status
        assert "No on-device model" in status

    def test_openai_consent_decline_restores_previous_off(self):
        dialog, values = self._make_meeting_dialog(
            extra={
                SettingsKey.MEETING_SPEAKER_ID_BACKEND: (
                    MeetingSpeakerIdBackend.OFF
                )
            }
        )
        assert dialog.meeting_speaker_id_combo.currentData() == (
            MeetingSpeakerIdBackend.OFF
        )

        class _DeclinedConsent:
            RESULT_ENABLE = "enable"
            result_action = "cancel"

            def __init__(self, parent=None):
                pass

            def exec(self):
                return 0

        with (
            patch.object(
                dialog_module,
                "resolve_meeting_audio_upload_consent",
                return_value=False,
            ),
            patch(
                "ui_qt.dialogs.meeting_audio_consent_dialog.MeetingAudioConsentDialog",
                _DeclinedConsent,
            ),
        ):
            openai_index = dialog.meeting_speaker_id_combo.findData(
                MeetingSpeakerIdBackend.OPENAI
            )
            dialog.meeting_speaker_id_combo.setCurrentIndex(openai_index)

        assert dialog.meeting_speaker_id_combo.currentData() == (
            MeetingSpeakerIdBackend.OFF
        )
        assert values[SettingsKey.MEETING_SPEAKER_ID_BACKEND] == (
            MeetingSpeakerIdBackend.OFF
        )

    def test_speaker_id_status_explains_first_meeting_download(self):
        """No Downloads row exists: the first meeting fetches the model."""
        dialog, _values = self._make_meeting_dialog()
        with patch.object(dialog_module, "speaker_model_path", return_value=None):
            dialog.refresh_component_state()
        status = dialog.speaker_id_status.text()
        assert "Hugging Face" in status
        assert "next meeting" in status
        assert "Downloads" not in status

    def test_speaker_id_status_reports_a_cached_model(self):
        dialog, _values = self._make_meeting_dialog()
        with patch.object(
            dialog_module, "speaker_model_path", return_value="C:/cache/model.onnx"
        ):
            dialog.refresh_component_state()
        assert "is ready" in dialog.speaker_id_status.text()

    def test_refresh_component_state_enables_pi_after_install(self):
        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value=None
        ), patch.object(
            dialog_module, "meeting_agent_needs_update", return_value=False
        ), patch.object(dialog_module, "is_frozen", return_value=False):
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

    def test_pi_label_says_why_it_is_unavailable(self):
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value=None):
            dialog, _values = self._make_meeting_dialog()
        combo = dialog.meeting_agent_core_combo
        for needs_update, frozen, label in (
            (True, True, "Pi (update from Downloads)"),
            (True, False, "Pi (update from Downloads)"),
            (False, True, "Pi (install from Downloads)"),
            (False, False, "Pi (sidecar not built)"),
        ):
            with patch.object(dialog_module, "meeting_agent_payload_dir", return_value=None), \
                    patch.object(dialog_module, "meeting_agent_needs_update",
                                 return_value=needs_update), \
                    patch.object(dialog_module, "is_frozen", return_value=frozen):
                dialog.refresh_component_state()
            assert combo.itemText(0) == label
            assert not combo.model().item(0).isEnabled()

    def test_refresh_component_state_restores_saved_pi_core(self):
        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value=None
        ):
            dialog, _values = self._make_meeting_dialog(
                extra={SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.PI}
            )
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.PI
        assert "Pi" in dialog.meeting_agent_core_notice.text()
        assert "no direct API fallback" in dialog.meeting_agent_core_notice.text()
        assert not dialog.meeting_agent_core_notice.isHidden()

        with patch.object(
            dialog_module, "meeting_agent_payload_dir", return_value="C:/payload"
        ):
            dialog.refresh_component_state()
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.PI
        assert dialog.meeting_agent_core_notice.isHidden()

    def test_agent_core_combo_lists_only_the_built_in_cores(self):
        dialog, _values = self._make_meeting_dialog()
        combo = dialog.meeting_agent_core_combo
        cores = [combo.itemData(i) for i in range(combo.count())]
        assert cores == [MeetingAgentCore.PI, MeetingAgentCore.OPENCODE]
        assert all("Standard API" not in combo.itemText(i) for i in range(combo.count()))
        assert "Downloads" not in dialog.meeting_model_tile.description_label.text()
        assert dialog_module.agent_core_label(MeetingAgentCore.CLAUDE_CODE) == "Claude Code"
        assert dialog_module.agent_core_label(MeetingAgentCore.OPENCODE_CLI) == "OpenCode"


class TestMeetingAgentChoice(_DialogTestCase):
    """Who runs AI insights: an installed agent or OpenWhisper's engine."""

    @pytest.fixture(autouse=True)
    def _agents_found(self):
        """Claude Code and Codex are found; OpenCode is not installed."""
        scan = {
            MeetingAgentCore.CLAUDE_CODE: CLAUDE,
            MeetingAgentCore.CODEX: CODEX,
            MeetingAgentCore.OPENCODE_CLI: None,
        }
        models = {
            MeetingAgentCore.CLAUDE_CODE: [
                AgentModel("", "Claude Code default"),
                AgentModel("haiku", "Haiku (fastest)"),
                AgentModel("sonnet", "Sonnet"),
            ],
            MeetingAgentCore.CODEX: [AgentModel("", "Codex default")],
        }
        with patch.object(agent_picker, "cached_agents", return_value=scan), \
                patch.object(agent_picker, "scan_installed_agents", return_value=scan), \
                patch.object(agent_picker, "list_agent_models",
                             side_effect=lambda agent: models[agent.id]), \
                patch.object(installed_agents, "configured_default_model",
                             return_value=""):
            yield

    def _make_agent_dialog(self, core, models=None, pi=False):
        extra = {SettingsKey.MEETING_AGENT_CORE: core}
        if models is not None:
            extra[SettingsKey.MEETING_AGENT_MODELS] = models
        with patch.object(dialog_module, "meeting_agent_payload_dir",
                          return_value="C:/pi" if pi else None):
            return self._make_dialog(extra_settings=extra)

    def test_saved_agent_is_chosen_and_hides_the_built_in_chat_model(self):
        dialog, _values = self._make_agent_dialog(
            MeetingAgentCore.CLAUDE_CODE, models={MeetingAgentCore.CLAUDE_CODE: "haiku"}
        )
        picker = dialog.meeting_agent_picker
        assert picker.choice() == MeetingAgentCore.CLAUDE_CODE
        assert picker.tiles[MeetingAgentCore.CLAUDE_CODE].selected
        assert dialog.meeting_model_tile.isHidden()
        assert dialog.meeting_model_title.isHidden()
        assert dialog.rail.value(MEETING_TEXT) == "Claude Code · Haiku"
        assert picker.model_combo().currentData() == "haiku"
        assert "Claude Team" in picker.usage_label.text()

    def test_built_in_core_shows_the_chat_model_tile(self):
        dialog, _values = self._make_agent_dialog(MeetingAgentCore.PI)
        assert dialog.meeting_agent_picker.choice() == agent_picker.BUILTIN
        assert dialog.meeting_agent_picker.tiles[agent_picker.BUILTIN].name_label.text() == "Pi"
        assert not dialog.meeting_model_tile.isHidden()
        assert dialog.rail.value(MEETING_TEXT) == "OpenRouter · deepseek/test-model"

    def test_choosing_an_agent_saves_it_and_openwhisper_restores_pi_even_if_missing(self):
        dialog, values = self._make_agent_dialog(MeetingAgentCore.PI)
        picker = dialog.meeting_agent_picker
        picker.tiles[MeetingAgentCore.CODEX].clicked.emit(MeetingAgentCore.CODEX)
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.CODEX
        assert picker.choice() == MeetingAgentCore.CODEX
        assert dialog.meeting_model_tile.isHidden()
        assert dialog.rail.value(MEETING_TEXT) == "Codex · default model"

        picker.tiles[agent_picker.BUILTIN].clicked.emit(agent_picker.BUILTIN)
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.PI
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.PI
        assert not dialog.meeting_agent_core_notice.isHidden()
        assert not dialog.meeting_model_tile.isHidden()

    def test_openwhisper_restores_pi_when_it_was_the_last_built_in_core(self):
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value="C:/pi"):
            dialog, values = self._make_agent_dialog(MeetingAgentCore.PI, pi=True)
            picker = dialog.meeting_agent_picker
            picker.tiles[MeetingAgentCore.CLAUDE_CODE].clicked.emit(
                MeetingAgentCore.CLAUDE_CODE
            )
            assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.CLAUDE_CODE
            picker.tiles[agent_picker.BUILTIN].clicked.emit(agent_picker.BUILTIN)
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.PI

    def test_saved_agent_starts_from_pi_when_the_payload_is_there(self):
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value="C:/pi"):
            dialog, values = self._make_agent_dialog(MeetingAgentCore.CODEX, pi=True)
            dialog.meeting_agent_picker.tiles[agent_picker.BUILTIN].clicked.emit(
                agent_picker.BUILTIN
            )
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.PI

    def test_agent_core_combo_does_not_save_while_an_agent_is_chosen(self):
        dialog, values = self._make_agent_dialog(MeetingAgentCore.CLAUDE_CODE)
        combo = dialog.meeting_agent_core_combo
        combo.setCurrentIndex(combo.findData(MeetingAgentCore.PI))
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.CLAUDE_CODE

    def test_switching_to_an_installed_agent_restores_packaged_sdk_choice(self):
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value="C:/payload"):
            dialog, values = self._make_agent_dialog(MeetingAgentCore.OPENCODE, pi=True)
            combo = dialog.meeting_agent_core_combo
            picker = dialog.meeting_agent_picker
            assert combo.currentData() == MeetingAgentCore.OPENCODE
            assert combo.model().item(combo.findData(MeetingAgentCore.OPENCODE)).isEnabled()
            assert combo.currentText() == "OpenCode SDK"
            picker.tiles[MeetingAgentCore.CODEX].clicked.emit(MeetingAgentCore.CODEX)
            assert dialog.meeting_model_tile.isHidden()
            dialog.refresh_component_state()
            picker.tiles[agent_picker.BUILTIN].clicked.emit(agent_picker.BUILTIN)
            assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.OPENCODE
            assert not dialog.meeting_model_tile.isHidden()

    def test_sdk_download_state_refresh_preserves_selection(self):
        dialog, values = self._make_agent_dialog(MeetingAgentCore.OPENCODE)
        combo = dialog.meeting_agent_core_combo
        index = combo.findData(MeetingAgentCore.OPENCODE)
        assert not combo.model().item(index).isEnabled()
        with patch.object(dialog_module, "meeting_agent_payload_dir", return_value="C:/sdk"):
            dialog.refresh_component_state()
        assert combo.model().item(index).isEnabled()
        assert combo.currentText() == "OpenCode SDK"
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.OPENCODE

    def test_model_choice_merges_into_the_saved_models(self):
        dialog, values = self._make_agent_dialog(
            MeetingAgentCore.CLAUDE_CODE, models={MeetingAgentCore.CODEX: "gpt-x"}
        )
        combo = dialog.meeting_agent_picker.model_combo()
        combo.activated.emit(combo.findData("sonnet"))
        assert values[SettingsKey.MEETING_AGENT_MODELS] == {
            MeetingAgentCore.CODEX: "gpt-x",
            MeetingAgentCore.CLAUDE_CODE: "sonnet",
        }
        assert dialog.rail.value(MEETING_TEXT) == "Claude Code · Sonnet"
        combo.activated.emit(combo.findData(""))
        assert values[SettingsKey.MEETING_AGENT_MODELS][MeetingAgentCore.CLAUDE_CODE] == ""

    def test_saved_agent_that_is_missing_stays_selected_with_a_notice(self):
        dialog, values = self._make_agent_dialog(MeetingAgentCore.OPENCODE_CLI)
        picker = dialog.meeting_agent_picker
        tile = picker.tiles[MeetingAgentCore.OPENCODE_CLI]
        assert tile.selected
        assert tile.state.tone == agent_picker.MISSING
        assert not tile.selectable
        assert not picker.notice.isHidden()
        assert "until it is installed" in picker.notice_label.text()
        assert picker.model_card.isHidden()
        assert dialog.meeting_model_tile.isHidden()
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.OPENCODE_CLI
        assert dialog.rail.value(MEETING_TEXT) == "OpenCode · not installed"

    def test_legacy_direct_selection_shows_pi_without_an_api_option(self):
        dialog, values = self._make_agent_dialog(MeetingAgentCore.DIRECT)
        assert dialog.meeting_agent_core() == MeetingAgentCore.PI
        assert dialog.meeting_agent_core_combo.currentData() == MeetingAgentCore.PI
        assert dialog.meeting_agent_core_combo.findData(MeetingAgentCore.DIRECT) == -1
        assert not dialog.meeting_agent_core_notice.isHidden()
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.DIRECT
        dialog.meeting_agent_picker.tiles[MeetingAgentCore.CODEX].clicked.emit(MeetingAgentCore.CODEX)
        dialog.meeting_agent_picker.tiles[agent_picker.BUILTIN].clicked.emit(agent_picker.BUILTIN)
        assert values[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.PI

    def test_opening_intelligence_uses_the_cached_scan(self):
        dialog, _values = self._make_agent_dialog(MeetingAgentCore.PI)
        with patch.object(agent_picker, "scan_installed_agents") as scan:
            dialog.on_destination_shown(MEETING_INTELLIGENCE)
        scan.assert_not_called()
        assert not dialog.meeting_agent_picker.is_scanning()

    def test_overview_names_the_agent_and_its_sign_in(self):
        dialog, _values = self._make_agent_dialog(
            MeetingAgentCore.CLAUDE_CODE, models={MeetingAgentCore.CLAUDE_CODE: "haiku"}
        )
        assert dialog.meeting_intelligence_overview() == (
            "Haiku", "Claude Code · your Claude Team sign-in"
        )
        assert dialog.meeting_intelligence_is_remote(lambda provider: False)
        assert dialog.meeting_agent_core_label() == "Claude Code"


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
        assert "Models & storage → Runtime" in dialog.meeting_runtime_label.text()
        assert "auto · auto" in dialog.meeting_runtime_label.text()


class TestOnDemandEngine(_DialogTestCase):
    """On-demand recording engine routes through the main-window path."""

    def test_engine_combo_invokes_backend_callback(self):
        dialog, _values = self._make_dialog()
        requested = []
        dialog.on_backend_changed = requested.append
        index = dialog.engine_combo.findData("api")
        dialog.engine_combo.setCurrentIndex(index)
        assert requested == ["API"]
        assert not dialog.ondemand_whisper_picker.isEnabled()


class TestCleanupSettingsOwnership(_DialogTestCase):
    """Cleanup Settings must not overwrite Model Manager selections."""

    @pytest.mark.parametrize("reasoning", ["off", "low", "medium", "high"])
    def test_thinking_level_loads_saves_and_refreshes(self, reasoning):
        dialog, values = self._make_dialog(extra_settings={
            SettingsKey.TRANSCRIPT_CLEANUP_REASONING: reasoning,
        })
        assert dialog.cleanup_reasoning_combo.currentData() == reasoning
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_REASONING] == reasoning

        combo = dialog.cleanup_reasoning_combo
        combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_REASONING] == combo.currentData()

        values[SettingsKey.TRANSCRIPT_CLEANUP_REASONING] = reasoning
        dialog.refresh()
        assert combo.currentData() == reasoning

    def test_saving_cleanup_settings_preserves_text_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "provider/model-test",
                    SettingsKey.TRANSCRIPT_CLEANUP_REASONING: "high",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                        TranscriptCleanupModelSort.NEWEST
                    ),
                }
            )
            with _isolated_settings(isolated):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                dialog.transcript_cleanup_check.toggle()

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "openrouter"
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "provider/model-test"
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_REASONING] == "high"
            assert saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT] == TranscriptCleanupModelSort.NEWEST

    def test_cleanup_page_hosts_the_chat_model_picker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with _isolated_settings(isolated):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                page = dialog._pages[CLEANUP]
                picker = dialog.models.text_model_picker
                assert page.isAncestorOf(picker)
                assert page.isAncestorOf(dialog.models.cleanup_reasoning_combo)
                assert dialog.cleanup_model_tile.isAncestorOf(picker)
                assert not hasattr(dialog, "open_model_manager_btn")

    def test_saving_meeting_settings_preserves_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.MEETING_WHISPER_MODEL: "tiny",
                    SettingsKey.MEETING_LLM_PROVIDER: "openai",
                    SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
                    SettingsKey.MEETING_LANGUAGE: "fr",
                    SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.PI,
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
            with _isolated_settings(isolated):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                assert dialog.rail.value(MEETING_VOICE) == "tiny · French"
                assert dialog.rail.value(MEETING_INTELLIGENCE) == "OpenAI · gpt-4o-mini"
                assert dialog.models.meeting_agent_core_label() == "Pi (sidecar)"
                assert dialog.models.speaker_id_is_remote()
                dialog.meeting_end_polish_check.setChecked(
                    not dialog.meeting_end_polish_check.isChecked()
                )

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"
            assert saved[SettingsKey.MEETING_LLM_PROVIDER] == "openai"
            assert saved[SettingsKey.MEETING_LLM_MODEL] == "gpt-4o-mini"
            assert saved[SettingsKey.MEETING_LANGUAGE] == "fr"
            assert saved[SettingsKey.MEETING_AGENT_CORE] == MeetingAgentCore.PI
            assert saved[SettingsKey.MEETING_SPEAKER_ID_BACKEND] == MeetingSpeakerIdBackend.OPENAI
            assert saved[SettingsKey.TEXT_LLM_PROFILES][0]["id"] == "custom_abcd1234"

    def test_meeting_voice_detail_shows_off_speaker_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.MEETING_SPEAKER_ID_BACKEND: (
                        MeetingSpeakerIdBackend.OFF
                    ),
                }
            )
            with _isolated_settings(isolated):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                assert "Me / Others labels" in dialog.models.meeting_voice_detail()

    def test_intelligence_page_hosts_the_meeting_picker_and_agent_core(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with _isolated_settings(isolated):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                page = dialog._pages[MEETING_INTELLIGENCE]
                assert page.isAncestorOf(dialog.models.meeting_model_picker)
                assert page.isAncestorOf(dialog.models.meeting_agent_core_combo)
                assert page.isAncestorOf(dialog.meeting_past_recall_tile)
                assert not hasattr(dialog, "open_meeting_model_manager_btn")
                # Who runs AI insights comes first, above the chat model.
                picker = dialog.meeting_agent_picker
                assert page.isAncestorOf(picker)
                assert picker.y() < dialog.meeting_model_tile.y()
                entries = [
                    entry for entry in dialog._search_index()
                    if entry.title == "Who runs AI insights"
                ]
                assert len(entries) == 1
                assert entries[0].target is picker
                assert entries[0].destination == MEETING_INTELLIGENCE
                assert "claude code" in entries[0].keywords
                assert "pi" in entries[0].keywords.split()
                heading, subtitle = dialog._headings[MEETING_INTELLIGENCE]
                assert "coding agent" in subtitle and "Pi" in subtitle
                assert "OpenWhisper's built-in engine" not in subtitle


class TestApiModelSelection(_DialogTestCase):
    def test_model_selection_persists_and_updates_rail(self):
        dialog, values = self._make_dialog(extra_settings={
            SettingsKey.SELECTED_MODEL: "api",
            SettingsKey.API_TRANSCRIPTION_MODEL: "whisper-1",
        })
        requested = []
        dialog.on_backend_changed = requested.append
        assert not dialog.api_model_field.isHidden()
        assert dialog.ondemand_whisper_field.isHidden()
        combo = dialog.api_model_combo
        assert combo.currentData() == "whisper-1"
        assert combo.currentText() == "whisper-1 (retiring Feb 26, 2027)"
        combo.setCurrentIndex(combo.findData("gpt-transcribe"))
        assert values[SettingsKey.API_TRANSCRIPTION_MODEL] == "gpt-transcribe"
        assert requested == ["API"]
        assert dialog.rail.value(ONDEMAND_VOICE) == "API · gpt-transcribe"
        dialog.refresh()
        assert requested == ["API"]

    def test_retired_models_leave_the_picker_after_shutdown(self):
        from datetime import date

        from services import openai_retirement

        with patch.object(openai_retirement, "_today", lambda: date(2027, 2, 26)):
            dialog, _values = self._make_dialog(extra_settings={
                SettingsKey.SELECTED_MODEL: "api",
                SettingsKey.API_TRANSCRIPTION_MODEL: "whisper-1",
                SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OPENAI,
            })
            combo = dialog.api_model_combo
            assert [combo.itemData(i) for i in range(combo.count())] == ["gpt-transcribe"]
            assert combo.currentData() == "gpt-transcribe"
            speaker = dialog.meeting_speaker_id_combo
            backends = [speaker.itemData(i) for i in range(speaker.count())]
            assert backends == [MeetingSpeakerIdBackend.OFF, MeetingSpeakerIdBackend.LOCAL]
            assert speaker.currentData() == MeetingSpeakerIdBackend.LOCAL


class TestOptionalSpeechSummary(_DialogTestCase):
    def test_rail_reports_optional_family_and_meeting_model(self):
        dialog, _ = self._make_dialog(extra_settings={
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.MEETING_ASR_MODEL: "moonshine-small",
        })
        assert dialog.rail.value(ONDEMAND_VOICE) == "Parakeet TDT 0.6B v3"
        assert dialog.rail.value(MEETING_VOICE) == (
            "Moonshine Streaming Small · Detect automatically"
        )
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("moonshine"))
        assert "Moonshine" in dialog.rail.value(ONDEMAND_VOICE)


def test_display_name_falls_back_to_the_default_backend():
    from config import config

    default_display = next(
        display for display, value in config.MODEL_VALUE_MAP.items()
        if value == config.DEFAULT_BACKEND
    )
    assert dialog_module._display_name_for_backend("parakeet") == "Parakeet"
    assert dialog_module._display_name_for_backend("api") == "API"
    assert dialog_module._display_name_for_backend("not-a-backend") == default_display
    assert dialog_module._display_name_for_backend("") == default_display

class TestNewTextProviders(_DialogTestCase):
    def test_independent_model_memory_survives_dialog_recreation(self):
        dialog, values = self._make_dialog()
        dialog.text_model_picker.set_provider("ollama", "llama3.2")
        dialog._activate_text_model("ollama")
        dialog.meeting_model_picker.set_provider("groq", "llama-3.3-70b-versatile")
        dialog._activate_meeting_llm_model("groq")
        assert values["cleanup_model_memory"] == {"ollama": "llama3.2"}
        assert values["meeting_model_memory"] == {"groq": "llama-3.3-70b-versatile"}
        reopened, _ = self._make_dialog(extra_settings=values)
        assert reopened.text_model_picker._staged_models["ollama"] == "llama3.2"
        assert reopened.meeting_model_picker._staged_models["groq"] == "llama-3.3-70b-versatile"

    def test_unknown_go_model_cannot_be_assigned(self):
        dialog, values = self._make_dialog()
        picker = dialog.text_model_picker
        picker.set_provider("opencode_go")
        picker.set_models(["glm-5.2", "future-model"])
        assert not picker.model_combo.model().item(1).isEnabled()
        picker.model_combo.setCurrentText("future-model")
        dialog._activate_text_model("opencode_go")
        assert values[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] == "openai"
        assert "no supported API route" in dialog.message_label.text()

    def test_stale_ollama_catalog_result_is_discarded(self):
        dialog, _ = self._make_dialog()
        key = ("ollama", "alphabetical")
        old_token = object()
        dialog._catalog_tokens[key] = old_token
        dialog._text_models_loading.add(key)
        dialog._invalidate_text_catalog("ollama")
        dialog._on_text_models_loaded("ollama", "alphabetical", ["old-model"], "", old_token)
        assert key not in dialog._text_models_cache

    def test_failed_refresh_keeps_last_catalog_after_provider_switch(self):
        dialog, _ = self._make_dialog()
        picker = dialog.text_model_picker
        picker.set_provider("groq")
        dialog._on_text_models_loaded("groq", "alphabetical", ["llama-3.3-70b-versatile"], "")
        picker.set_provider("ollama")
        picker.set_provider("groq")
        dialog._on_text_models_loaded("groq", "alphabetical", [], "offline")
        assert picker.model_combo.count() == 1
        assert "offline" in picker.status_label.text()

    def test_ollama_exposes_edit_but_not_delete(self):
        dialog, _ = self._make_dialog()
        picker = dialog.text_model_picker
        picker.set_provider("ollama")
        assert picker.edit_endpoint_button.isEnabled()
        assert not picker.delete_endpoint_button.isEnabled()
        assert picker.provider_requirement.text() == "No API key required"

def test_meeting_remote_source_is_independent_and_preserves_local_model():
    settings = InMemorySettings({SettingsKey.SELECTED_MODEL: "local_whisper",
                              SettingsKey.MEETING_WHISPER_MODEL: "tiny"})
    with _isolated_settings(settings):
        host = _Host(lambda: "base")
        models = host.models
        models.refresh()
        assert models.meeting_source_combo.currentData() == "local"
        assert not models.meeting_local_model_field.isHidden()
        models.meeting_source_combo.setCurrentIndex(models.meeting_source_combo.findData("remote"))
        assert models.meeting_local_model_field.isHidden()
        assert not models.meeting_remote_controls.isHidden()
        assert "Microphone and system audio are sent" in models.meeting_runtime_label.text()
        assert settings.values[SettingsKey.SELECTED_MODEL] == "local_whisper"
        assert settings.values[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"
        reopened = _Host(lambda: "base")
        reopened.models.refresh()
        assert reopened.models.meeting_source_combo.currentData() == "remote"
        reopened.models.meeting_source_combo.setCurrentIndex(0)
        assert reopened.models.meeting_whisper_picker.current_model() == "tiny"


def test_meeting_remote_settings_link_and_unpaired_connection_check(monkeypatch):
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from tests.test_remote_engine import _wait_for
    settings = InMemorySettings({SettingsKey.MEETING_ASR_SOURCE: "remote"})
    with _isolated_settings(settings):
        host = _Host(lambda: None)
        selected = []
        host.select_destination = selected.append
        host.models.refresh()
        host.models._open_meeting_remote_settings()
        assert selected == [REMOTE_ENGINE]
        host.models._test_meeting_remote()
        assert _wait_for(lambda: (QApplication.processEvents() or True)
                         and host.models.meeting_remote_test.isEnabled())
        assert "Pair a computer" in host.models.meeting_remote_status.text()


def test_settings_closed_mid_scan_is_not_held_by_the_worker():
    # The worker used to emit on Settings from its own thread, which raced
    # Settings being destroyed (every test's teardown, and app shutdown).
    gate = threading.Event()

    def scan(max_age_seconds=None):
        gate.wait(5)
        return {}

    settings = InMemorySettings({SettingsKey.SELECTED_MODEL: "local_whisper"})
    with _isolated_settings(settings), \
            patch.object(dialog_module, "scan_cached_models", side_effect=scan):
        host = QWidget()
        models = ModelAssignments(host, NavRail(host), QLabel(host))
        models.refresh()
        workers = [t for t in threading.enumerate()
                   if t.name == "settings-models-cache-scan"]
        assert workers
        ref = weakref.ref(models)
        del models
        host.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        del host
        gc.collect()
        assert ref() is None
        gate.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        QApplication.processEvents()
