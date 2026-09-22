"""Model assignments hosted by the Settings window.

Builds the model pages Settings shows under Dictation, Meeting Mode, and
Models & storage (Voice model, Voice & speakers, Runtime), plus the two chat
model sections the AI cleanup and Meeting intelligence pages embed, so every
model choice sits on the page for the feature it powers. Every choice persists
on change.

The host owns the window, the rail, and navigation. This object only reports
each model destination's current value through ``rail.set_value`` and emits
``assignments_changed`` so the host can refresh values that combine a model
with its own settings (AI cleanup, the Overview).
"""
import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root, config
from services.components import (
    ComponentId,
    ComponentState,
    component_coordinator,
    current_platform_tag,
    meeting_agent_payload_dir,
)
from services.hf_access import (
    CachedModelInfo,
    peek_cached_models,
    resolve_model_repo,
    scan_cached_models,
)
from services.settings import (
    MeetingAgentCore,
    MeetingLanguage,
    MeetingSpeakerIdBackend,
    SettingsKey,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    TranscriptCleanupReasoning,
    default_transcript_cleanup_model,
    resolve_api_transcription_model,
    resolve_meeting_agent_core,
    resolve_meeting_audio_upload_consent,
    resolve_meeting_language,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    settings_manager,
)
from services.text_llm import (
    get_profile,
    list_profiles,
    profile_display_name,
    remove_custom_profile,
    upsert_custom_profile,
)
from ui_qt.dialogs.settings_destinations import (
    CLEANUP,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    RUNTIME,
    VOICE_MODEL,
)
from ui_qt.widgets import Button, ElidingComboBox, InfoTile
from ui_qt.widgets.local_model_picker import LocalModelPicker
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.text_model_picker import TextModelPicker
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

_ENGINE_CAPTIONS = {
    "local_whisper": "Local faster-whisper on this computer.",
    "api": "OpenAI transcription. Enter your API key in Settings → API keys.",
}

#: Downloads filter value for the Whisper family.
WHISPER_FILTER = "local_whisper"


def _design_icon(filename: str) -> QIcon:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    # Preserve the semantic icon color for disabled current-state buttons.
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def _display_name_for_backend(model_value: str) -> str:
    names = {value: display for display, value in config.MODEL_VALUE_MAP.items()}
    return names.get(model_value) or names[config.DEFAULT_BACKEND]


def agent_core_label(core: str) -> str:
    """Short display name for a ``MeetingAgentCore`` value."""
    if core == MeetingAgentCore.PI:
        return "Pi (sidecar)"
    if core == MeetingAgentCore.OPENCODE:
        return "OpenCode v2 (beta)"
    return "Direct (no sidecar)"


def speaker_id_label(backend: str) -> str:
    """Short display name for a ``MeetingSpeakerIdBackend`` value."""
    if backend == MeetingSpeakerIdBackend.OFF:
        return "Me / Others labels"
    if backend == MeetingSpeakerIdBackend.OPENAI:
        return "OpenAI speaker labels"
    return "On-device speaker labels"


def meeting_language_label(code: str) -> str:
    return next(
        (label for value, label in MeetingLanguage.CHOICES if value == code), code
    )


class ModelAssignments(QObject):
    """Every model assignment, built as pages and sections for Settings.

    Args:
        host: Parent for the confirmation and endpoint dialogs this opens.
        rail: The host's rail; model destinations report their value here.
        message_label: Where status and error messages are shown.
        get_loaded_model: Provider returning the model name currently loaded
            by the engine (or None). Used to resolve what "auto" means.
        background_cache_scan: Scan the model cache on a worker thread. Tests
            pass False to scan synchronously through a patched scanner.
    """

    #: The model page needs Downloads, filtered to one backend ("" for all).
    downloads_requested = pyqtSignal(str)
    #: A model assignment or its rail value changed.
    assignments_changed = pyqtSignal()
    _text_models_loaded = pyqtSignal(str, str, list, str, object)
    _cache_scan_finished = pyqtSignal(int, object)

    COMPUTE_CHOICES = ("auto", "float16", "float32", "int8")

    #: Assigned by UIController.
    on_set_active_requested: Optional[Callable[[str], None]] = None
    on_backend_changed: Optional[Callable[[str], None]] = None
    on_runtime_settings_changed: Optional[Callable[[], None]] = None

    def __init__(
        self,
        host: QWidget,
        rail: NavRail,
        message_label: QLabel,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        background_cache_scan: bool = True,
    ):
        super().__init__(host)
        self._host = host
        self.rail = rail
        self.message_label = message_label
        self._get_loaded_model = get_loaded_model
        self._background_cache_scan = bool(background_cache_scan)
        self._cache_scan_generation = 0
        self._cached: Dict[str, CachedModelInfo] = {}
        self._text_models_cache: Dict[tuple, list] = {}
        self._catalog_tokens = {}
        self._text_models_loading = set()
        self._active_text_provider = TranscriptCleanupProvider.OPENAI
        self._active_text_model = default_transcript_cleanup_model(
            self._active_text_provider
        )
        self._active_meeting_provider = TranscriptCleanupProvider.OPENROUTER
        self._active_meeting_llm_model = config.MEETING_LLM_MODEL
        self._pi_payload_available = meeting_agent_payload_dir() is not None
        self._opencode_payload_available = (
            meeting_agent_payload_dir("opencode") is not None
        )
        self._built = set()
        self._text_models_loaded.connect(self._on_text_models_loaded)
        self._cache_scan_finished.connect(self._on_cache_scan_finished)

    # ---- construction helpers ----

    def _field(self, label: str, widget: QWidget) -> QWidget:
        """Wrap a control with its field label."""
        wrapper = QWidget()
        wrapper.setObjectName("modelManagerFieldGroup")
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)
        caption = QLabel(label)
        caption.setObjectName("textModelFieldLabel")
        col.addWidget(caption)
        col.addWidget(widget)
        return wrapper

    @staticmethod
    def _group_title(layout: QVBoxLayout, text: str) -> QLabel:
        # Qt stylesheets have no text-transform, so the eyebrow case is set here.
        caption = QLabel(text.upper())
        caption.setObjectName("settingsTileGroupTitle")
        layout.addWidget(caption)
        return caption

    @staticmethod
    def _card(layout: QVBoxLayout) -> QVBoxLayout:
        """Add a resting settings card and return its content layout."""
        card = QFrame()
        card.setObjectName("settingsTile")
        card.setProperty("kind", "field")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(10)
        layout.addWidget(card)
        return inner

    def _footnote(self, text: str) -> QWidget:
        card = QFrame()
        card.setObjectName("textModelFootnoteCard")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(12)
        icon = QLabel()
        icon.setObjectName("textModelFootnoteIcon")
        icon.setFixedSize(18, 18)
        icon.setPixmap(_design_icon("info-blue.svg").pixmap(16, 16))
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)
        note = WrappedLabel(text)
        note.setObjectName("textModelFootnote")
        layout.addWidget(note, stretch=1)
        return card

    @staticmethod
    def _caption(text: str) -> WrappedLabel:
        label = WrappedLabel(text)
        label.setObjectName("infoLabel")
        return label

    def _say(self, text: str) -> None:
        self.message_label.setText(text)

    # ---- page builders (the host passes its page layout) ----

    def build_voice_page(self, layout: QVBoxLayout) -> None:
        """Dictation → Voice model: engine, model, and what is downloaded."""
        self._group_title(layout, "Engine")
        card = self._card(layout)
        self.engine_combo = ElidingComboBox()
        self.engine_combo.setObjectName("ondemandEngineCombo")
        self.engine_combo.setMinimumHeight(40)
        for display in config.MODEL_CHOICES:
            self.engine_combo.addItem(display, config.MODEL_VALUE_MAP[display])
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        card.addWidget(self._field("Recording engine", self.engine_combo))

        self.engine_caption = self._caption("")
        card.addWidget(self.engine_caption)

        self.ondemand_whisper_picker = LocalModelPicker()
        self.ondemand_whisper_picker.model_changed.connect(
            self._on_set_active_clicked
        )
        self.ondemand_whisper_picker.manage_downloads_requested.connect(
            lambda: self.downloads_requested.emit(WHISPER_FILTER)
        )
        self.ondemand_whisper_field = self._field(
            "Model", self.ondemand_whisper_picker
        )
        card.addWidget(self.ondemand_whisper_field)

        from ui_qt.widgets.local_engine_controls import LocalEngineControls
        self.speech_controls = LocalEngineControls()
        self.speech_controls.engine_settings_changed.connect(
            self._on_speech_settings_changed
        )
        card.addWidget(self.speech_controls)
        self.api_model_combo = ElidingComboBox()
        self.api_model_combo.setMinimumHeight(40)
        self.api_model_combo.addItems(list(config.API_MODEL_CHOICES))
        self.api_model_combo.currentTextChanged.connect(self._on_api_model_changed)
        self.api_model_field = self._field("Model", self.api_model_combo)
        card.addWidget(self.api_model_field)

        self.engine_inventory_title = self._group_title(layout, "On this computer")
        self.engine_inventory_row = QWidget()
        self.engine_inventory_row.setObjectName("engineInventoryRow")
        row = QHBoxLayout(self.engine_inventory_row)
        row.setContentsMargins(3, 0, 0, 0)
        row.setSpacing(12)
        self.engine_inventory_label = self._caption("")
        self.engine_inventory_label.setObjectName("engineInventoryLabel")
        row.addWidget(self.engine_inventory_label, stretch=1)
        self.speech_download_button = Button("Get models and runtimes")
        self.speech_download_button.setObjectName("engineInventoryButton")
        self.speech_download_button.set_base_minimum_size(0, 34)
        self.speech_download_button.clicked.connect(
            lambda: self.downloads_requested.emit(self._engine_filter())
        )
        row.addWidget(self.speech_download_button)
        layout.addWidget(self.engine_inventory_row)

        layout.addWidget(
            self._footnote(
                "For dictation previews, Nemotron uses native streaming; Parakeet "
                "transcribes short audio chunks with the loaded model; Local "
                "Whisper uses a separate tiny.en model. The final transcript uses "
                "your selected model. Nemotron and Moonshine provide native live "
                "previews in Meeting Mode."
            )
        )
        self._built.add(VOICE_MODEL)

    def build_cleanup_model_section(self, layout: QVBoxLayout) -> InfoTile:
        """The chat model AI cleanup runs, embedded in the AI cleanup page."""
        self.text_model_picker = TextModelPicker()
        self._connect_picker_profile_signals(self.text_model_picker)
        self.text_model_picker.provider_changed.connect(
            self._on_text_provider_changed
        )
        self.text_model_picker.refresh_requested.connect(
            lambda provider: self._fetch_text_models(provider, force=True)
        )
        self.text_model_picker.activation_requested.connect(
            self._activate_text_model
        )
        self.text_model_picker.sort_changed.connect(self._on_text_sort_changed)

        self.cleanup_reasoning_combo = ElidingComboBox()
        self.cleanup_reasoning_combo.setMinimumHeight(40)
        for label, value in (
            ("Off", TranscriptCleanupReasoning.OFF),
            ("Low", TranscriptCleanupReasoning.LOW),
            ("Medium", TranscriptCleanupReasoning.MEDIUM),
            ("High", TranscriptCleanupReasoning.HIGH),
        ):
            self.cleanup_reasoning_combo.addItem(label, value)
        self.cleanup_reasoning_combo.currentIndexChanged.connect(
            self._on_cleanup_reasoning_changed
        )

        self.cleanup_model_tile = InfoTile(
            "Chat model",
            "The provider and model that rewrite each dictation. Cleanup "
            "profiles use it too. The provider's key lives under API keys.",
            _design_icon("box-blue.svg"),
        )
        self.cleanup_model_tile.add_body(self.text_model_picker)
        self.cleanup_model_tile.add_body(
            self._field("Thinking level", self.cleanup_reasoning_combo)
        )
        self.cleanup_model_tile.add_body(
            self._caption("Used only by models with configurable reasoning.")
        )
        layout.addWidget(self.cleanup_model_tile)
        self._built.add(CLEANUP)
        return self.cleanup_model_tile

    def build_meeting_voice_page(self, layout: QVBoxLayout) -> None:
        """Meeting Mode → Voice & speakers."""
        self._group_title(layout, "Speech")
        card = self._card(layout)
        self.meeting_whisper_picker = LocalModelPicker(include_speech_models=True)
        self.meeting_whisper_picker.model_changed.connect(
            self._on_meeting_set_active_clicked
        )
        self.meeting_whisper_picker.manage_downloads_requested.connect(
            lambda: self.downloads_requested.emit("")
        )
        card.addWidget(
            self._field("Meeting speech model", self.meeting_whisper_picker)
        )
        self.meeting_runtime_label = self._caption("")
        card.addWidget(self.meeting_runtime_label)

        self.meeting_language_combo = ElidingComboBox()
        self.meeting_language_combo.setObjectName("meetingLanguageCombo")
        self.meeting_language_combo.setMinimumHeight(40)
        for code, label in MeetingLanguage.CHOICES:
            self.meeting_language_combo.addItem(label, code)
        self.meeting_language_combo.setToolTip(
            "Choose the meeting language when known. This avoids unreliable "
            "language detection on short chunks and strong accents."
        )
        self.meeting_language_combo.currentIndexChanged.connect(
            self._on_meeting_language_changed
        )
        card.addWidget(self._field("Spoken language", self.meeting_language_combo))

        self._group_title(layout, "Speakers")
        card = self._card(layout)
        self.meeting_speaker_id_combo = ElidingComboBox()
        self.meeting_speaker_id_combo.setObjectName("meetingSpeakerIdCombo")
        self.meeting_speaker_id_combo.setMinimumHeight(40)
        self.meeting_speaker_id_combo.addItem(
            "Off (Me / Others channel labels only)",
            MeetingSpeakerIdBackend.OFF,
        )
        self.meeting_speaker_id_combo.addItem(
            "On-device (WeSpeaker · Speaker 1, Speaker 2, …)",
            MeetingSpeakerIdBackend.LOCAL,
        )
        self.meeting_speaker_id_combo.addItem(
            "OpenAI (gpt-4o-transcribe-diarize, system audio after End)",
            MeetingSpeakerIdBackend.OPENAI,
        )
        self._speaker_id_backend_previous = MeetingSpeakerIdBackend.LOCAL
        self.meeting_speaker_id_combo.currentIndexChanged.connect(
            self._on_speaker_id_backend_changed
        )
        card.addWidget(
            self._field("Speaker identification", self.meeting_speaker_id_combo)
        )
        self.speaker_id_status = self._caption("")
        card.addWidget(self.speaker_id_status)
        self._built.add(MEETING_VOICE)

    def build_meeting_model_section(self, layout: QVBoxLayout) -> InfoTile:
        """The meeting chat model and agent core, embedded in Intelligence."""
        self.meeting_model_picker = TextModelPicker(
            idle_status="Open Meeting intelligence to load the model catalog."
        )
        self._connect_picker_profile_signals(self.meeting_model_picker)
        self.meeting_model_picker.provider_changed.connect(
            self._on_meeting_provider_changed
        )
        self.meeting_model_picker.refresh_requested.connect(
            lambda provider: self._fetch_catalog_models(
                provider,
                picker=self.meeting_model_picker,
                force=True,
            )
        )
        self.meeting_model_picker.activation_requested.connect(
            self._activate_meeting_llm_model
        )
        self.meeting_model_picker.sort_changed.connect(
            self._on_meeting_sort_changed
        )

        self.meeting_agent_core_combo = ElidingComboBox()
        self.meeting_agent_core_combo.setObjectName("meetingAgentCoreCombo")
        self.meeting_agent_core_combo.setMinimumHeight(40)
        pi_label = (
            "Pi (sidecar)" if self._pi_payload_available
            else "Pi (sidecar not built)"
        )
        self.meeting_agent_core_combo.addItem(pi_label, MeetingAgentCore.PI)
        model = self.meeting_agent_core_combo.model()
        item = model.item(0) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(self._pi_payload_available)
        self.meeting_agent_core_combo.addItem(
            "Direct (no sidecar)", MeetingAgentCore.DIRECT
        )
        self.meeting_agent_core_combo.addItem(
            self._opencode_label(), MeetingAgentCore.OPENCODE
        )
        item = model.item(2) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(self._opencode_payload_available)
        self.meeting_agent_core_combo.currentIndexChanged.connect(
            self._on_meeting_agent_core_changed
        )

        self.meeting_model_tile = InfoTile(
            "Chat model",
            "Runs live cards, the note taker, polish, summaries, and the final "
            "report. Install Pi or OpenCode from Downloads.",
            _design_icon("box-blue.svg"),
        )
        self.meeting_model_tile.add_body(self.meeting_model_picker)
        self.meeting_model_tile.add_body(
            self._field("Agent core", self.meeting_agent_core_combo)
        )
        layout.addWidget(self.meeting_model_tile)
        self._built.add(MEETING_INTELLIGENCE)
        return self.meeting_model_tile

    def build_runtime_page(self, layout: QVBoxLayout) -> None:
        """Models & storage → Runtime: device and quantization."""
        self._group_title(layout, "Local Whisper")
        card = self._card(layout)
        runtime_row = QHBoxLayout()
        runtime_row.setSpacing(12)
        device_choices = (
            ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
        )
        self.device_combo = ElidingComboBox()
        self.device_combo.setObjectName("libraryDeviceCombo")
        self.device_combo.addItems(device_choices)
        self.device_combo.setMinimumHeight(40)
        self.device_combo.currentTextChanged.connect(self._on_runtime_changed)
        self.compute_combo = ElidingComboBox()
        self.compute_combo.setObjectName("libraryComputeCombo")
        self.compute_combo.addItems(self.COMPUTE_CHOICES)
        self.compute_combo.setMinimumHeight(40)
        self.compute_combo.currentTextChanged.connect(self._on_runtime_changed)
        runtime_row.addWidget(self._field("Device", self.device_combo))
        runtime_row.addWidget(self._field("Quantization", self.compute_combo))
        card.addLayout(runtime_row)
        card.addWidget(
            self._caption(
                "auto picks CUDA when a supported GPU is present and falls back "
                "to CPU otherwise. Changing either value reloads the local "
                "engine."
            )
        )
        layout.addWidget(
            self._footnote(
                "Parakeet, Nemotron, Moonshine, and Qwen3-ASR set their device on "
                "Dictation → Voice model. Downloaded models and optional "
                "components are managed in Downloads."
            )
        )
        self._built.add(RUNTIME)

    def _opencode_label(self) -> str:
        if self._opencode_payload_available:
            return "OpenCode v2 (beta)"
        if current_platform_tag() != "win_amd64":
            return "OpenCode v2 (beta — Windows x64 only)"
        return "OpenCode v2 (beta — install from Downloads)"

    # ---- navigation hooks ----

    def on_destination_shown(self, key: str) -> None:
        """Load the chat-model catalog a destination shows, once it is open."""
        if key == CLEANUP:
            self._fetch_catalog_models(
                self.text_model_picker.provider, picker=self.text_model_picker
            )
        elif key == MEETING_INTELLIGENCE:
            self._fetch_catalog_models(
                self.meeting_model_picker.provider,
                picker=self.meeting_model_picker,
            )

    # ---- assignment handlers ----

    def _on_set_active_clicked(self, model_name: str):
        if self.on_set_active_requested:
            self.on_set_active_requested(model_name)
        self.refresh()

    def _on_meeting_set_active_clicked(self, model_name: str) -> None:
        try:
            from services.local_asr.catalog import MODELS
            if model_name in MODELS:
                settings_manager.save_setting(SettingsKey.MEETING_ASR_MODEL, model_name)
            else:
                settings_manager.update_settings({
                    SettingsKey.MEETING_ASR_MODEL: "",
                    SettingsKey.MEETING_WHISPER_MODEL: model_name,
                })
        except Exception as exc:
            logger.error("Couldn't set meeting Whisper model: %s", exc)
            self._say(f"Couldn't set meeting transcription model: {exc}")
            return
        self._say(f'Meeting transcription model set to "{model_name}"')
        self.refresh()

    def _on_engine_changed(self, _index: int) -> None:
        """Route a recording-engine change through the main-window path."""
        display = self.engine_combo.currentText()
        if self.on_backend_changed:
            self.on_backend_changed(display)
        self._update_engine_caption()
        self._update_ondemand_whisper_enabled()
        self._refresh_engine_inventory()
        self._refresh_rail_values()

    def _on_speech_settings_changed(self):
        self._refresh_meeting_runtime_label()
        self._refresh_engine_inventory()
        if self.on_runtime_settings_changed:
            self.on_runtime_settings_changed()
        self._refresh_rail_values()

    def _on_api_model_changed(self, model: str) -> None:
        settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_MODEL, model)
        if self.on_backend_changed:
            self.on_backend_changed("API")
        self._refresh_rail_values()

    def _on_runtime_changed(self, _text: str = "") -> None:
        """Persist shared device/quant and ask the controller to reload."""
        try:
            settings_manager.update_settings({
                SettingsKey.WHISPER_DEVICE: self.device_combo.currentText(),
                SettingsKey.WHISPER_COMPUTE_TYPE: (
                    self.compute_combo.currentText()
                ),
            })
        except Exception as exc:
            logger.error("Couldn't save shared Whisper runtime: %s", exc)
            self._say(f"Couldn't save device or quant: {exc}")
            return
        if self.on_runtime_settings_changed:
            self.on_runtime_settings_changed()
        self._refresh_meeting_runtime_label()
        self._refresh_rail_values()

    def _on_meeting_language_changed(self, _index: int) -> None:
        language = self.meeting_language_combo.currentData()
        if language is None:
            return
        try:
            settings_manager.save_setting(SettingsKey.MEETING_LANGUAGE, language)
        except Exception as exc:
            logger.error("Couldn't save meeting language: %s", exc)
            self._say(f"Couldn't save spoken language: {exc}")
            return
        self._refresh_rail_values()

    def _on_meeting_agent_core_changed(self, _index: int) -> None:
        core = self.meeting_agent_core_combo.currentData()
        if core is None:
            return
        try:
            settings_manager.save_setting(SettingsKey.MEETING_AGENT_CORE, core)
        except Exception as exc:
            logger.error("Couldn't save meeting agent core: %s", exc)
            self._say(f"Couldn't save agent core: {exc}")
            return
        self._refresh_rail_values()

    def _on_speaker_id_backend_changed(self, _index: int = 0) -> None:
        """Ask for audio-upload consent when the user picks OpenAI speaker ID."""
        backend = self.meeting_speaker_id_combo.currentData()
        if backend == MeetingSpeakerIdBackend.OPENAI:
            if not resolve_meeting_audio_upload_consent():
                from ui_qt.dialogs.meeting_audio_consent_dialog import (
                    MeetingAudioConsentDialog,
                )

                dialog = MeetingAudioConsentDialog(self._host)
                dialog.exec()
                granted = (
                    dialog.result_action == MeetingAudioConsentDialog.RESULT_ENABLE
                )
                if granted:
                    settings_manager.save_setting(
                        SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN, True,
                    )
                else:
                    previous = getattr(
                        self,
                        "_speaker_id_backend_previous",
                        MeetingSpeakerIdBackend.LOCAL,
                    )
                    previous_index = self.meeting_speaker_id_combo.findData(
                        previous
                    )
                    blocker = self.meeting_speaker_id_combo.blockSignals(True)
                    self.meeting_speaker_id_combo.setCurrentIndex(
                        max(0, previous_index)
                    )
                    self.meeting_speaker_id_combo.blockSignals(blocker)
                    return
        if backend is None:
            return
        try:
            settings_manager.save_setting(
                SettingsKey.MEETING_SPEAKER_ID_BACKEND, backend
            )
        except Exception as exc:
            logger.error("Couldn't save speaker identification: %s", exc)
            self._say(f"Couldn't save speaker identification: {exc}")
        else:
            self._speaker_id_backend_previous = backend
        self._refresh_speaker_id_status()
        self._refresh_rail_values()

    # ---- text endpoint profiles ----

    def _settings_snapshot(self) -> dict:
        """Load settings, or an empty dict when the store is unavailable."""
        try:
            return settings_manager.load_all_settings()
        except Exception:
            return {}

    def _connect_picker_profile_signals(self, picker: TextModelPicker) -> None:
        picker.add_endpoint_requested.connect(self._add_text_endpoint)
        picker.edit_endpoint_requested.connect(self._edit_text_endpoint)
        picker.delete_endpoint_requested.connect(self._delete_text_endpoint)

    def _refresh_picker_profiles(self) -> None:
        profiles = list_profiles(self._settings_snapshot())
        settings = self._settings_snapshot()
        for picker, key in ((self.text_model_picker, "cleanup_model_memory"),
                            (self.meeting_model_picker, "meeting_model_memory")):
            memory = settings.get(key, {})
            if isinstance(memory, dict):
                picker._staged_models.update({p: m for p, m in memory.items()
                                               if isinstance(m, str)})
            picker.set_profiles(profiles)

    def _add_text_endpoint(self) -> None:
        from ui_qt.dialogs.text_endpoint_dialog import TextEndpointDialog

        sender = self.sender()
        dialog = TextEndpointDialog(parent=self._host)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload() or {}
        try:
            profile = settings_manager.mutate_settings(
                lambda settings: upsert_custom_profile(
                    settings,
                    name=payload["name"],
                    base_url=payload["base_url"],
                    api_key_env=payload.get("api_key_env", ""),
                )
            )
        except Exception as exc:
            logger.error("Couldn't add text endpoint: %s", exc)
            self._say(f"Couldn't add endpoint: {exc}")
            return
        self._refresh_picker_profiles()
        if sender is self.meeting_model_picker:
            self.meeting_model_picker.set_provider(profile.id)
            self._fetch_catalog_models(
                profile.id, picker=self.meeting_model_picker
            )
        else:
            self.text_model_picker.set_provider(profile.id)
            self._fetch_catalog_models(
                profile.id, picker=self.text_model_picker
            )
        self._say(f'Added endpoint "{profile.name}"')

    def _edit_text_endpoint(self, profile_id: str) -> None:
        from ui_qt.dialogs.text_endpoint_dialog import TextEndpointDialog

        profile = get_profile(profile_id, self._settings_snapshot())
        if profile is not None and profile.id == "ollama":
            url, accepted = QInputDialog.getText(
                self._host, "Ollama server",
                "API base URL (shared by cleanup and meetings):",
                text=profile.base_url or "",
            )
            if not accepted:
                return
            from services.text_llm import save_ollama_url
            try:
                save_ollama_url(url)
            except ValueError as exc:
                self._say(str(exc))
                return
            self._invalidate_text_catalog("ollama")
            self._refresh_picker_profiles()
            for picker in (self.text_model_picker, self.meeting_model_picker):
                if picker.provider == "ollama":
                    self._fetch_catalog_models("ollama", picker=picker, force=True)
            self._say(
                "Ollama server updated. Active meetings keep their original server."
            )
            return
        if profile is None or profile.builtin:
            return
        dialog = TextEndpointDialog(profile, parent=self._host)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload() or {}
        try:
            updated = settings_manager.mutate_settings(
                lambda settings: upsert_custom_profile(
                    settings,
                    name=payload["name"],
                    base_url=payload["base_url"],
                    api_key_env=payload.get("api_key_env", ""),
                    profile_id=profile_id,
                )
            )
        except Exception as exc:
            logger.error("Couldn't edit text endpoint: %s", exc)
            self._say(f"Couldn't save endpoint: {exc}")
            return
        self._invalidate_text_catalog(updated.id)
        self._refresh_picker_profiles()
        self.text_model_picker.set_provider(updated.id)
        self.meeting_model_picker.set_provider(
            self.meeting_model_picker.provider
        )
        self._say(f'Updated endpoint "{updated.name}"')

    def _delete_text_endpoint(self, profile_id: str) -> None:
        """Delete a custom endpoint that is not currently assigned."""
        settings = self._settings_snapshot()
        profile = get_profile(profile_id, settings)
        if profile is None or profile.builtin:
            return
        cleanup_id = resolve_transcript_cleanup_provider(settings)
        meeting_id = resolve_meeting_llm_provider(settings)
        if profile_id in (cleanup_id, meeting_id):
            self._say(
                f'"{profile.name}" is in use. Choose another text model '
                "before deleting this endpoint."
            )
            return
        confirmed = QMessageBox.question(
            self._host,
            "Delete endpoint",
            f'Delete "{profile.name}"?\n\n'
            "Meetings that already recorded this endpoint can still retry "
            "using the stored connection snapshot.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        try:
            settings_manager.mutate_settings(
                lambda stored: remove_custom_profile(stored, profile_id)
            )
        except Exception as exc:
            logger.error("Couldn't delete text endpoint: %s", exc)
            self._say(f"Couldn't delete endpoint: {exc}")
            return
        self._refresh_picker_profiles()
        self._say(f'Deleted endpoint "{profile.name}"')

    # ---- catalog loading ----

    def _on_text_provider_changed(self, provider: str) -> None:
        self._update_cleanup_reasoning_controls(
            provider, self.text_model_picker.model_combo.currentText()
        )
        if self.rail.current_key() == CLEANUP:
            self._fetch_catalog_models(
                provider, picker=self.text_model_picker
            )

    def _on_meeting_provider_changed(self, provider: str) -> None:
        if self.rail.current_key() == MEETING_INTELLIGENCE:
            self._fetch_catalog_models(
                provider, picker=self.meeting_model_picker
            )

    def _on_text_sort_changed(self, provider: str) -> None:
        """Persist OpenRouter's catalog order and reload that catalog."""
        try:
            settings_manager.save_setting(
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
                self.text_model_picker.current_sort(),
            )
        except Exception as exc:
            logger.warning("Couldn't save text model sort: %s", exc)
        self._fetch_catalog_models(provider, picker=self.text_model_picker)

    def _on_meeting_sort_changed(self, provider: str) -> None:
        """Reload the meeting catalog order without touching cleanup sort."""
        self._fetch_catalog_models(
            provider, picker=self.meeting_model_picker
        )

    def _fetch_text_models(self, provider: str, force: bool = False) -> None:
        """Compatibility wrapper for cleanup catalog loads."""
        self._fetch_catalog_models(
            provider, picker=self.text_model_picker, force=force
        )

    def _invalidate_text_catalog(self, provider: str) -> None:
        for key in list(self._text_models_cache):
            if key[0] == provider:
                self._text_models_cache.pop(key, None)
        for key in list(self._catalog_tokens):
            if key[0] == provider:
                self._catalog_tokens.pop(key, None)
                self._text_models_loading.discard(key)

    def _fetch_catalog_models(
        self,
        provider: str,
        picker: TextModelPicker,
        force: bool = False,
    ) -> None:
        """Load one provider's chat-model catalog on a worker thread.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            picker: Cleanup or Meeting picker that requested the catalog.
            force: Bypass the in-memory cache when true.
        """
        if provider == picker.provider:
            picker._update_credential_status()
        sort = picker.current_sort()
        key = (provider, sort)
        if not force and key in self._text_models_cache:
            models = self._text_models_cache[key]
            self._apply_catalog_to_picker(picker, provider, sort, models, "")
            return
        if key in self._text_models_loading:
            if provider == picker.provider:
                picker.set_loading(True)
            return

        self._text_models_loading.add(key)
        token = object()
        self._catalog_tokens[key] = token
        if provider == picker.provider:
            picker.set_loading(True)

        def worker():
            try:
                from services.transcript_cleanup import list_cleanup_models

                models = list_cleanup_models(provider, sort=sort)
                error = ""
            except Exception as exc:
                models = []
                error = str(exc)
            try:
                self._text_models_loaded.emit(provider, sort, models, error, token)
            except RuntimeError:
                pass  # Settings was destroyed before the catalog finished.

        threading.Thread(
            target=worker,
            name=f"text-models-{provider}",
            daemon=True,
        ).start()

    def _apply_catalog_to_picker(
        self,
        picker: TextModelPicker,
        provider: str,
        sort: str,
        models: list,
        error: str,
    ) -> None:
        """Apply a catalog result to one picker when it still matches."""
        if provider != picker.provider:
            return
        provider_loading = any(
            loading_provider == provider
            for loading_provider, _loading_sort in self._text_models_loading
        )
        picker.set_loading(provider_loading)
        if sort != picker.current_sort():
            return
        if error:
            cached = self._text_models_cache.get((provider, sort))
            if cached is not None:
                picker.set_models(cached)
            picker.status_label.setText(f"Couldn't load models: {error}")
            return
        picker.set_models(models)
        picker.status_label.setText(f"{len(models)} models available")

    def _on_text_models_loaded(
        self, provider: str, sort: str, models: list, error: str, token=None
    ) -> None:
        """Apply a provider catalog result on the Qt thread."""
        key = (provider, sort)
        if token is not None and token is not self._catalog_tokens.get(key):
            return
        self._text_models_loading.discard(key)
        if not error:
            self._text_models_cache[key] = models
        self._apply_catalog_to_picker(
            self.text_model_picker, provider, sort, models, error
        )
        self._apply_catalog_to_picker(
            self.meeting_model_picker, provider, sort, models, error
        )

    def _update_cleanup_reasoning_controls(self, provider: str, model: str) -> None:
        from services.text_model_catalog import model_spec
        profile = get_profile(provider, self._settings_snapshot())
        supported = profile is not None and profile.kind in ("openai", "openrouter")
        if profile is not None and model:
            try:
                supported = supported or model_spec(profile, model).reasoning_format in ("openai", "deepseek")
            except ValueError:
                pass
        self.cleanup_reasoning_combo.setEnabled(supported)
        self.cleanup_reasoning_combo.setToolTip(
            "Thinking effort for this model." if supported
            else "This model uses its provider's default reasoning behavior."
        )

    def _validate_text_model(self, provider: str, model: str) -> bool:
        from services.text_model_catalog import model_spec
        if not model:
            return False
        try:
            model_spec(get_profile(provider, self._settings_snapshot()), model)
            return True
        except ValueError as exc:
            self._say(str(exc))
            return False

    def _activate_text_model(self, provider: str) -> None:
        if provider != self.text_model_picker.provider:
            return
        model = self.text_model_picker.model_combo.currentText().strip()
        if not self._validate_text_model(provider, model):
            return
        if (
            provider == self._active_text_provider
            and model == self._active_text_model
        ):
            return
        try:
            settings_manager.update_settings({
                SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: provider,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: model,
                "cleanup_model_memory": {**self._settings_snapshot().get("cleanup_model_memory", {}), provider: model},
            })
        except Exception as exc:
            logger.error("Couldn't activate text model: %s", exc)
            self._say(f"Couldn't set text model: {exc}")
            return

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_active_selection(provider, model)
        self._update_cleanup_reasoning_controls(provider, model)
        self._say(f"Text model set to {self.text_summary()}")
        self._refresh_rail_values()

    def _activate_meeting_llm_model(self, provider: str) -> None:
        if provider != self.meeting_model_picker.provider:
            return
        model = self.meeting_model_picker.model_combo.currentText().strip()
        if not self._validate_text_model(provider, model):
            return
        if (
            provider == self._active_meeting_provider
            and model == self._active_meeting_llm_model
        ):
            return
        try:
            settings_manager.update_settings({
                SettingsKey.MEETING_LLM_PROVIDER: provider,
                SettingsKey.MEETING_LLM_MODEL: model,
                "meeting_model_memory": {**self._settings_snapshot().get("meeting_model_memory", {}), provider: model},
            })
        except Exception as exc:
            logger.error("Couldn't activate meeting LLM model: %s", exc)
            self._say(f"Couldn't set meeting model: {exc}")
            return

        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        self.meeting_model_picker.set_active_selection(provider, model)
        self._say(f"Meeting intelligence model set to {self.meeting_text_summary()}")
        self._refresh_rail_values()

    # ---- state loading ----

    def _on_cleanup_reasoning_changed(self, _index: int = 0) -> None:
        try:
            settings_manager.save_setting(
                SettingsKey.TRANSCRIPT_CLEANUP_REASONING,
                self.cleanup_reasoning_combo.currentData(),
            )
        except Exception as exc:
            logger.error("Couldn't save cleanup thinking level: %s", exc)
            self._say(f"Couldn't save thinking level: {exc}")

    def _load_text_settings(self) -> None:
        settings = self._settings_snapshot()
        provider = resolve_transcript_cleanup_provider(settings)
        model = resolve_transcript_cleanup_model(settings)
        if not isinstance(model, str):
            model = ""
        model = model.strip()

        sort = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
            config.TRANSCRIPT_CLEANUP_MODEL_SORT,
        )
        if sort not in TranscriptCleanupModelSort.ALL:
            sort = config.TRANSCRIPT_CLEANUP_MODEL_SORT

        reasoning_index = self.cleanup_reasoning_combo.findData(
            resolve_transcript_cleanup_reasoning(settings)
        )
        blocker = self.cleanup_reasoning_combo.blockSignals(True)
        self.cleanup_reasoning_combo.setCurrentIndex(max(0, reasoning_index))
        self.cleanup_reasoning_combo.blockSignals(blocker)

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_sort(sort)
        self._refresh_picker_profiles()
        self.text_model_picker.set_provider(provider, model)
        self.text_model_picker.set_active_selection(provider, model)
        self._update_cleanup_reasoning_controls(provider, model)

    def _load_meeting_settings(self) -> None:
        settings = self._settings_snapshot()
        provider = resolve_meeting_llm_provider(settings)
        model = resolve_meeting_llm_model(settings)
        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        # Sort order is in-session only for Meeting (never overwrites cleanup).
        self.meeting_model_picker.set_provider(provider, model)
        self.meeting_model_picker.set_active_selection(provider, model)

        language_index = self.meeting_language_combo.findData(
            resolve_meeting_language(settings)
        )
        blocker = self.meeting_language_combo.blockSignals(True)
        self.meeting_language_combo.setCurrentIndex(max(0, language_index))
        self.meeting_language_combo.blockSignals(blocker)

        self._sync_pi_core_availability(settings)

        resolved_backend = resolve_meeting_speaker_id_backend(settings)
        backend_index = self.meeting_speaker_id_combo.findData(resolved_backend)
        blocker = self.meeting_speaker_id_combo.blockSignals(True)
        self.meeting_speaker_id_combo.setCurrentIndex(max(0, backend_index))
        self.meeting_speaker_id_combo.blockSignals(blocker)
        self._speaker_id_backend_previous = resolved_backend
        self._refresh_speaker_id_status()

    def refresh_engine_selection(self) -> None:
        self._load_engine_and_runtime()
        self._refresh_engine_inventory()
        self._refresh_rail_values()

    def _load_engine_and_runtime(self) -> None:
        try:
            model_value = settings_manager.load_model_selection()
        except Exception:
            model_value = config.DEFAULT_BACKEND
        display = _display_name_for_backend(model_value)
        index = self.engine_combo.findText(display)
        blocker = self.engine_combo.blockSignals(True)
        self.engine_combo.setCurrentIndex(max(0, index))
        self.engine_combo.blockSignals(blocker)
        self._update_engine_caption()
        self._update_ondemand_whisper_enabled()

        settings = self._settings_snapshot()
        blocker = self.api_model_combo.blockSignals(True)
        self.api_model_combo.setCurrentText(resolve_api_transcription_model(settings))
        self.api_model_combo.blockSignals(blocker)
        device = settings.get(SettingsKey.WHISPER_DEVICE, "auto")
        compute = settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto")
        if self.device_combo.findText(str(device)) < 0:
            device = "auto"
        if self.compute_combo.findText(str(compute)) < 0:
            compute = "auto"
        blocker = self.device_combo.blockSignals(True)
        self.device_combo.setCurrentText(str(device))
        self.device_combo.blockSignals(blocker)
        blocker = self.compute_combo.blockSignals(True)
        self.compute_combo.setCurrentText(str(compute))
        self.compute_combo.blockSignals(blocker)
        self._refresh_meeting_runtime_label()

    def _update_engine_caption(self) -> None:
        value = self.engine_combo.currentData() or "local_whisper"
        from services.local_asr.catalog import BACKENDS, DEFAULT_MODELS, MODELS
        caption = MODELS[DEFAULT_MODELS[value]].purpose + ". Runs locally." if value in BACKENDS else _ENGINE_CAPTIONS.get(value, "")
        self.engine_caption.setText(caption)

    def _update_ondemand_whisper_enabled(self) -> None:
        is_local = self.engine_combo.currentData() == "local_whisper"
        self.ondemand_whisper_picker.setEnabled(is_local)
        self.ondemand_whisper_field.setVisible(is_local)
        from services.local_asr.catalog import BACKENDS
        backend = self.engine_combo.currentData()
        is_speech = backend in BACKENDS
        self.speech_controls.setVisible(is_speech)
        self.speech_download_button.setVisible(is_speech)
        if is_speech:
            self.speech_controls.set_backend(backend)
        self.api_model_field.setVisible(backend == "api")
        self.engine_inventory_title.setVisible(backend != "api")
        self.engine_inventory_row.setVisible(backend != "api")

    def _engine_filter(self) -> str:
        """Downloads backend filter for the selected recording engine."""
        backend = self.engine_combo.currentData() or WHISPER_FILTER
        return "" if backend == "api" else backend

    def _refresh_engine_inventory(self) -> None:
        """Say what the selected engine has on this computer."""
        backend = self.engine_combo.currentData() or WHISPER_FILTER
        if backend == "api":
            self.engine_inventory_label.setText("")
            return
        try:
            if backend == WHISPER_FILTER:
                repos = {
                    resolve_model_repo(name)
                    for name in config.WHISPER_MODEL_CHOICES
                    if name != "auto"
                }
                present = sum(1 for repo in repos if repo in self._cached)
                text = f"{present} of {len(repos)} Whisper models on this computer."
            else:
                from services.components import is_installed
                from services.local_asr.cache import is_cached
                from services.local_asr.catalog import (
                    BACKENDS,
                    MODELS,
                    resolve_runtime,
                    selected_device,
                )
                keys = [key for key, model in MODELS.items() if model.backend == backend]
                present = sum(1 for key in keys if is_cached(key))
                noun = "model" if len(keys) == 1 else "models"
                text = (
                    f"{present} of {len(keys)} {BACKENDS[backend]} {noun} on this "
                    "computer"
                )
                component, _device = resolve_runtime(
                    backend, selected_device(backend, self._settings_snapshot())
                )
                name = component_coordinator.describe(component).display_name
                state = "installed" if is_installed(component) else "not installed"
                text += f" · {name} {state}."
        except Exception:
            logger.debug("Engine inventory lookup failed", exc_info=True)
            text = "Open Downloads to see which models are on this computer."
        self.engine_inventory_label.setText(text)

    def _refresh_meeting_runtime_label(self) -> None:
        from services.local_asr.catalog import MODELS, selected_device
        model = resolve_meeting_whisper_model(self._settings_snapshot())
        if model in MODELS:
            device = selected_device(MODELS[model].backend, self._settings_snapshot())
            self.meeting_runtime_label.setText(
                f"Uses {MODELS[model].label} with its {device} device preference. "
                "Set that engine's device on Dictation → Voice model. Install "
                "its runtime and model in Downloads."
            )
            return
        device = self.device_combo.currentText() or "auto"
        compute = self.compute_combo.currentText() or "auto"
        self.meeting_runtime_label.setText(
            f"Device and quantization come from Models & storage → Runtime "
            f"({device} · {compute}) and are shared with dictation's Local "
            "Whisper."
        )

    def _refresh_speaker_id_status(self) -> None:
        backend = self.meeting_speaker_id_combo.currentData()
        if backend == MeetingSpeakerIdBackend.OFF:
            self.speaker_id_status.setText(
                "Channel labels only: microphone is Me, system audio is "
                "Others. No on-device model and no audio upload."
            )
            return
        if backend == MeetingSpeakerIdBackend.OPENAI:
            self.speaker_id_status.setText(
                "Uploads system audio after End and relabels speakers on the "
                "local transcript. Requires an OpenAI API key (Settings → API "
                "keys). Microphone audio stays on this computer."
            )
            return
        try:
            info = component_coordinator.describe(ComponentId.SPEAKER_ID)
            installed = info.state in (
                ComponentState.INSTALLED,
                ComponentState.UPDATE_AVAILABLE,
                ComponentState.EXTERNAL,
            )
        except Exception:
            installed = False
        if installed:
            self.speaker_id_status.setText(
                "On-device WeSpeaker (voxceleb_resnet34_LM.onnx) is available."
            )
        else:
            self.speaker_id_status.setText(
                "On-device WeSpeaker (voxceleb_resnet34_LM.onnx). Install "
                "Speaker Identification from Downloads if live labels are "
                "missing."
            )

    # ---- refresh ----

    def _sync_pi_core_availability(self, settings: Optional[dict] = None) -> None:
        """Refresh the Pi combo after a meeting-agent install or remove.

        Settings is non-modal and cached, so ``_pi_payload_available`` cannot
        stay as the value computed in ``__init__``.
        """
        self._pi_payload_available = meeting_agent_payload_dir() is not None
        self._opencode_payload_available = meeting_agent_payload_dir("opencode") is not None
        combo = getattr(self, "meeting_agent_core_combo", None)
        if combo is None:
            return
        combo.setItemText(
            0,
            "Pi (sidecar)" if self._pi_payload_available else "Pi (sidecar not built)",
        )
        model = combo.model()
        item = model.item(0) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(self._pi_payload_available)
        oc_index = combo.findData(MeetingAgentCore.OPENCODE)
        combo.setItemText(oc_index, self._opencode_label())
        oc_item = model.item(oc_index) if hasattr(model, "item") else None
        if oc_item is not None:
            oc_item.setEnabled(self._opencode_payload_available)
        snapshot = settings if settings is not None else self._settings_snapshot()
        core = resolve_meeting_agent_core(snapshot)
        if core == MeetingAgentCore.PI and not self._pi_payload_available:
            core = MeetingAgentCore.DIRECT
        core_index = combo.findData(core)
        blocker = combo.blockSignals(True)
        combo.setCurrentIndex(max(0, core_index))
        combo.blockSignals(blocker)

    def refresh_component_state(self) -> None:
        """Re-read component install state these pages report on."""
        self._sync_pi_core_availability()
        self._refresh_speaker_id_status()
        self._refresh_engine_inventory()

    def refresh(self, scan: bool = True) -> None:
        """Reload every assignment, then the model cache it depends on.

        Args:
            scan: Rescan the model cache. Without it, the last shared scan is
                reused, so loading a hidden window never touches the disk.
        """
        self._load_text_settings()
        self._load_meeting_settings()
        self._load_engine_and_runtime()
        if not self._background_cache_scan:
            self._refresh_cached_model_state(
                scan_cached_models(max_age_seconds=30.0)
            )
            return

        self._refresh_cached_model_state(peek_cached_models() or {})
        if not scan:
            return
        self._cache_scan_generation += 1
        generation = self._cache_scan_generation

        def load() -> None:
            result = scan_cached_models(max_age_seconds=30.0)
            try:
                self._cache_scan_finished.emit(generation, result)
            except RuntimeError:
                pass  # Settings was destroyed before the scan finished.

        threading.Thread(
            target=load,
            name="settings-models-cache-scan",
            daemon=True,
        ).start()

    def _on_cache_scan_finished(self, generation: int, cached) -> None:
        if generation != self._cache_scan_generation:
            return
        self._refresh_cached_model_state(dict(cached or {}))

    def _refresh_cached_model_state(
        self,
        cached: Dict[str, CachedModelInfo],
    ) -> None:
        self._cached = dict(cached)
        settings = self._settings_snapshot()
        active_model = settings_manager.get(
            SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL
        )
        if active_model not in config.WHISPER_MODEL_CHOICES:
            active_model = config.DEFAULT_WHISPER_MODEL
        meeting_model = resolve_meeting_whisper_model(settings)
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None

        self.ondemand_whisper_picker.set_options(
            cached, active_model, resolved=loaded_model
        )
        self._update_ondemand_whisper_enabled()
        self.meeting_whisper_picker.set_options(cached, meeting_model)
        self._refresh_engine_inventory()
        self._refresh_rail_values()

    def set_downloading(self, model_name: str) -> None:
        self.refresh()

    def set_download_progress(self, model_name: str, done: int, total: int) -> None:
        return

    def finish_download(self, model_name: str, success: bool) -> None:
        self.refresh()

    # ---- summaries read by the host (rail, AI cleanup, Overview) ----

    @property
    def active_text_provider(self) -> str:
        return self._active_text_provider

    @property
    def active_text_model(self) -> str:
        return self._active_text_model

    @property
    def active_meeting_provider(self) -> str:
        return self._active_meeting_provider

    def voice_summary(self) -> str:
        """The dictation engine and model, as the rail shows it."""
        engine_value = self.engine_combo.currentData() or "local_whisper"
        if engine_value == "local_whisper":
            return f"Local Whisper · {self.ondemand_whisper_picker.current_model()}"
        if engine_value == "api":
            return f"API · {self.api_model_combo.currentText()}"
        return self.speech_controls.model_combo.currentText()

    def voice_detail(self) -> str:
        """Where dictation runs, for the Overview card."""
        engine_value = self.engine_combo.currentData() or "local_whisper"
        if engine_value == "api":
            return "Sent to OpenAI for transcription"
        if engine_value == "local_whisper":
            device = self.device_combo.currentText() or "auto"
            return f"On this computer · {device}"
        from services.local_asr.catalog import selected_device
        return f"On this computer · {selected_device(engine_value, self._settings_snapshot())}"

    def voice_is_remote(self) -> bool:
        return (self.engine_combo.currentData() or "") == "api"

    def text_summary(self) -> str:
        provider = profile_display_name(
            self._active_text_provider, self._settings_snapshot()
        )
        return f"{provider} · {self._active_text_model}"

    def meeting_model_label(self) -> str:
        from services.local_asr.catalog import MODELS
        model = self.meeting_whisper_picker.current_model()
        return MODELS[model].label if model in MODELS else model

    def meeting_voice_summary(self) -> str:
        language = meeting_language_label(
            self.meeting_language_combo.currentData() or "auto"
        )
        return f"{self.meeting_model_label()} · {language}"

    def meeting_voice_detail(self) -> str:
        language = meeting_language_label(
            self.meeting_language_combo.currentData() or "auto"
        )
        speakers = speaker_id_label(self.meeting_speaker_id_combo.currentData())
        return f"{language} · {speakers}"

    def speaker_id_is_remote(self) -> bool:
        return (
            self.meeting_speaker_id_combo.currentData()
            == MeetingSpeakerIdBackend.OPENAI
        )

    def meeting_text_summary(self) -> str:
        provider = profile_display_name(
            self._active_meeting_provider, self._settings_snapshot()
        )
        return f"{provider} · {self._active_meeting_llm_model}"

    def meeting_model_name(self) -> str:
        return self._active_meeting_llm_model

    def meeting_agent_core_label(self) -> str:
        return agent_core_label(self.meeting_agent_core_combo.currentData())

    def runtime_summary(self) -> str:
        return (
            f"{self.device_combo.currentText()} · "
            f"{self.compute_combo.currentText()}"
        )

    def _refresh_rail_values(self) -> None:
        """Mirror each model destination's current assignment into the rail."""
        self.rail.set_value(VOICE_MODEL, self.voice_summary())
        self.rail.set_value(MEETING_VOICE, self.meeting_voice_summary())
        self.rail.set_value(MEETING_INTELLIGENCE, self.meeting_text_summary())
        self.rail.set_value(RUNTIME, self.runtime_summary())
        self.assignments_changed.emit()
