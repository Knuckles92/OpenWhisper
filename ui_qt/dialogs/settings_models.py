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
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
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

from config import config, is_frozen
from services.components import (
    component_coordinator,
    current_platform_tag,
    meeting_agent_needs_update,
    meeting_agent_payload_dir,
    speaker_model_path,
)
from services import installed_agents, openai_retirement
from services.hf_access import (
    CachedModelInfo,
    custom_model_cache_info,
    peek_cached_models,
    resolve_model_repo,
    scan_cached_models,
)
from services.whisper_sources import custom_models
from ui_qt.dialogs.custom_whisper_dialog import add_custom_models
from services.settings import (
    SETTING_DEFAULTS,
    MeetingAgentCore,
    MeetingLanguage,
    MeetingSpeakerIdBackend,
    SettingsKey,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    TranscriptCleanupReasoning,
    api_model_choices,
    api_model_label,
    default_transcript_cleanup_model,
    resolve_api_transcription_model,
    resolve_meeting_agent_core,
    resolve_meeting_agent_model,
    resolve_meeting_agent_models,
    resolve_meeting_audio_upload_consent,
    resolve_meeting_language,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    resolve_meeting_asr_source,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    setting_value,
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
from ui_qt.dialogs.settings_metadata import MODEL_CONTROL_DESTINATIONS
from ui_qt.dialogs.settings_fields import (
    group_title,
    settings_caption,
    settings_field,
)
from ui_qt.utils.icons import design_icon as _design_icon
from ui_qt.widgets import Button, ElidingComboBox, InfoTile
from ui_qt.widgets.agent_picker import BUILTIN, AgentPicker
from ui_qt.widgets.local_model_picker import LocalModelPicker
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.text_model_picker import TextModelPicker
from ui_qt.widgets.wrapped_label import WrappedLabel
from ui_qt.widgets.speech_backend_picker import (
    backend_display_name,
    backend_picker_tooltip,
    populate_backend_combo,
)

logger = logging.getLogger(__name__)

_ENGINE_CAPTIONS = {
    "local_whisper": "Local faster-whisper on this computer.",
    "api": "OpenAI transcription. Enter your API key in Settings → API keys.",
    "remote": (
        "Another computer's engine, over your network. Pair with it in "
        "Settings → Remote engine."
    ),
}

#: Downloads filter value for the Whisper family.
WHISPER_FILTER = "local_whisper"


def _display_name_for_backend(model_value: str) -> str:
    return backend_display_name(model_value)


def agent_core_label(core: str) -> str:
    """Short display name for a ``MeetingAgentCore`` value."""
    if core in (MeetingAgentCore.PI, MeetingAgentCore.DIRECT):
        return "Pi (sidecar)"
    if core == MeetingAgentCore.OPENCODE:
        return "OpenCode SDK"
    if core in installed_agents.AGENT_SPECS:
        return installed_agents.AGENT_SPECS[core].name
    return "Unknown agent"


def _opencode_in_downloads() -> bool:
    """Whether Downloads offers the packaged OpenCode component."""
    from services.components import ComponentId, component_is_published
    return component_is_published(ComponentId.MEETING_AGENT_OPENCODE)


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
    _engine_runtime_checked = pyqtSignal(object, str)
    _meeting_remote_checked = pyqtSignal(str)

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
        self._engine_runtime_pending: set[tuple[str, str]] = set()
        self._engine_runtime_labels: Dict[tuple[str, str], str] = {}
        self._engine_inventory_label_key: Optional[tuple[str, str]] = None
        self._engine_inventory_prefix = ""
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
        self._opencode_payload_available = meeting_agent_payload_dir("opencode") is not None
        self._built = set()
        self._text_models_loaded.connect(self._on_text_models_loaded)
        self._cache_scan_finished.connect(self._on_cache_scan_finished)
        self._engine_runtime_checked.connect(self._on_engine_runtime_checked)
        self._meeting_remote_checked.connect(self._on_meeting_remote_checked)

    def __getattr__(self, name):
        destination = MODEL_CONTROL_DESTINATIONS.get(name)
        host = self.__dict__.get("_host")
        if destination and host is not None and hasattr(host, "ensure_page"):
            host.ensure_page(destination)
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError(name)

    def _engine_value(self):
        if VOICE_MODEL in self._built:
            return self.engine_combo.currentData() or "local_whisper"
        return setting_value(SettingsKey.SELECTED_MODEL, self._settings_snapshot())

    def _meeting_source(self):
        if MEETING_VOICE in self._built:
            return self.meeting_source_combo.currentData()
        return resolve_meeting_asr_source(self._settings_snapshot())

    # ---- construction helpers ----

    _field = staticmethod(settings_field)
    _group_title = staticmethod(group_title)
    _caption = staticmethod(settings_caption)

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
        populate_backend_combo(self.engine_combo)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        card.addWidget(self._field("Recording engine", self.engine_combo))

        self.engine_caption = self._caption("")
        card.addWidget(self.engine_caption)

        self.ondemand_whisper_picker = LocalModelPicker()
        self.ondemand_whisper_picker.custom_models_requested.connect(self._add_custom_models)
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
        for model in api_model_choices():
            self.api_model_combo.addItem(api_model_label(model), model)
        self.api_model_combo.currentIndexChanged.connect(self._on_api_model_changed)
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
        self.meeting_source_combo = ElidingComboBox()
        self.meeting_source_combo.setObjectName("meetingSpeechSourceCombo")
        self.meeting_source_combo.setMinimumHeight(40)
        self.meeting_source_combo.addItem("This computer", "local")
        self.meeting_source_combo.addItem("Remote computer", "remote")
        self.meeting_source_combo.currentIndexChanged.connect(self._on_meeting_source_changed)
        card.addWidget(self._field("Speech engine", self.meeting_source_combo))
        self.meeting_whisper_picker = LocalModelPicker(include_speech_models=True)
        self.meeting_whisper_picker.custom_models_requested.connect(self._add_custom_models)
        self.meeting_whisper_picker.model_changed.connect(
            self._on_meeting_set_active_clicked
        )
        self.meeting_whisper_picker.manage_downloads_requested.connect(
            lambda: self.downloads_requested.emit("")
        )
        self.meeting_local_model_field = self._field("Meeting speech model", self.meeting_whisper_picker)
        card.addWidget(self.meeting_local_model_field)
        self.meeting_remote_controls = QWidget()
        remote_layout = QHBoxLayout(self.meeting_remote_controls)
        remote_layout.setContentsMargins(0, 0, 0, 0)
        configure_remote = Button("Configure remote engine")
        configure_remote.setObjectName("meetingConfigureRemoteButton")
        configure_remote.clicked.connect(self._open_meeting_remote_settings)
        self.meeting_remote_test = Button("Test connection")
        self.meeting_remote_test.setObjectName("meetingTestRemoteButton")
        self.meeting_remote_test.clicked.connect(self._test_meeting_remote)
        remote_layout.addWidget(configure_remote)
        remote_layout.addWidget(self.meeting_remote_test)
        remote_layout.addStretch()
        card.addWidget(self.meeting_remote_controls)
        self.meeting_remote_status = self._caption("")
        card.addWidget(self.meeting_remote_status)
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
        if not openai_retirement.retired():
            self.meeting_speaker_id_combo.addItem(
                "OpenAI (system audio after End · ends "
                f"{openai_retirement.SHUTDOWN_LABEL})",
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
        """Who runs AI insights, then the built-in chat model, in Intelligence.

        The agent picker comes first. The chat model tile (endpoint, model,
        agent core) belongs to the packaged SDK tile (Pi by default), so it
        shows only while that tile is chosen.
        """
        self.meeting_agent_picker = AgentPicker()
        self.meeting_agent_picker.choice_requested.connect(self._on_meeting_agent_choice)
        self.meeting_agent_picker.model_chosen.connect(self._on_meeting_agent_model_chosen)
        self.meeting_agent_picker.results_changed.connect(self._refresh_rail_values)
        layout.addWidget(self.meeting_agent_picker)
        layout.addSpacing(10)
        self.meeting_model_title = self._group_title(layout, "Model")

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
        self.meeting_agent_core_combo.addItem(self._pi_label(), MeetingAgentCore.PI)
        model = self.meeting_agent_core_combo.model()
        item = model.item(0) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(self._pi_payload_available)
        self.meeting_agent_core_combo.addItem(self._opencode_label(), MeetingAgentCore.OPENCODE)
        oc_item = model.item(1) if hasattr(model, "item") else None
        if oc_item is not None:
            oc_item.setEnabled(self._opencode_payload_available)
        self.meeting_agent_core_combo.currentIndexChanged.connect(
            self._on_meeting_agent_core_changed
        )

        self.meeting_model_tile = InfoTile(
            "Chat model",
            "Pi or OpenCode SDK runs live cards, the note taker, "
            "polish, summaries, and the final report with this endpoint and "
            "your API key.",
            _design_icon("box-blue.svg"),
        )
        self.meeting_model_tile.add_body(self.meeting_model_picker)
        self.meeting_model_tile.add_body(
            self._field("Agent core", self.meeting_agent_core_combo)
        )
        self.meeting_agent_core_notice = self._caption("")
        self.meeting_model_tile.add_body(self.meeting_agent_core_notice)
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

    def _pi_label(self) -> str:
        if self._pi_payload_available:
            return "Pi (sidecar)"
        if meeting_agent_needs_update():
            return "Pi (update from Downloads)"
        if is_frozen():
            return "Pi (install from Downloads)"
        return "Pi (sidecar not built)"

    def _opencode_label(self) -> str:
        if self._opencode_payload_available:
            return "OpenCode SDK"
        from services.opencode_component import SUPPORTED_PLATFORMS
        if current_platform_tag() not in SUPPORTED_PLATFORMS:
            return "OpenCode SDK (Windows and Linux only)"
        if not _opencode_in_downloads():
            return "OpenCode SDK (not in Downloads yet)"
        return "OpenCode SDK (install from Downloads)"

    # ---- navigation hooks ----

    def on_destination_shown(self, key: str) -> None:
        """Load the chat-model catalog a destination shows, once it is open."""
        if key == MEETING_VOICE:
            self._refresh_meeting_source()
        elif key == CLEANUP:
            self._fetch_catalog_models(
                self.text_model_picker.provider, picker=self.text_model_picker
            )
        elif key == MEETING_INTELLIGENCE:
            self.meeting_agent_picker.ensure_scanned()
            if not self.meeting_agent_is_installed():
                self._fetch_catalog_models(
                    self.meeting_model_picker.provider,
                    picker=self.meeting_model_picker,
                )

    # ---- assignment handlers ----

    def _open_meeting_remote_settings(self):
        from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
        self._host.select_destination(REMOTE_ENGINE)

    def _on_meeting_source_changed(self, _index):
        source = self.meeting_source_combo.currentData()
        settings_manager.save_setting(SettingsKey.MEETING_ASR_SOURCE, source)
        self.meeting_remote_status.setText("")
        self._refresh_meeting_source()
        self._refresh_rail_values()

    def _refresh_meeting_source(self):
        remote = self.meeting_source_combo.currentData() == "remote"
        self.meeting_local_model_field.setVisible(not remote)
        self.meeting_remote_controls.setVisible(remote)
        self.meeting_remote_status.setVisible(remote)
        self._refresh_meeting_runtime_label()

    def _test_meeting_remote(self):
        # Handshake and capability check only: no audio or model switches.
        settings = self._settings_snapshot()
        self.meeting_remote_test.setEnabled(False)
        self.meeting_remote_status.setText("Connecting to the paired computer…")

        def check():
            backend = None
            try:
                from meeting.asr.remote import MeetingRemoteBackend, remote_route
                backend = MeetingRemoteBackend(remote_route(settings))
                backend.reload_model()
                message = (f"Ready: {backend.device_info}" if backend.is_available()
                           else backend.last_error or "Remote speech is unavailable.")
            except Exception as exc:
                message = str(exc)
            finally:
                if backend is not None:
                    backend.cleanup()
            try:
                self._meeting_remote_checked.emit(message)
            except RuntimeError:
                pass  # Settings closed while connecting.

        threading.Thread(target=check, name="meeting-remote-check", daemon=True).start()

    def _on_meeting_remote_checked(self, message):
        self.meeting_remote_test.setEnabled(True)
        self.meeting_remote_status.setText(message)

    def _on_set_active_clicked(self, model_name: str):
        if self.on_set_active_requested:
            self.on_set_active_requested(model_name)
        self.refresh()

    def _add_custom_models(self):
        added = add_custom_models(self.host, settings_manager, self._cached)
        if added:
            self.refresh()
            self._say(f"Added {len(added)} custom models. Choose a model above to load it.")
            self.assignments_changed.emit()

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
        self.choose_backend(self.engine_combo.currentData())

    def choose_backend(self, backend: str) -> None:
        """Shared Basic/Advanced engine choice without building a model page."""
        if backend not in config.MODEL_VALUE_MAP.values():
            return
        from ui_qt.widgets.speech_backend_picker import backend_display_name

        try:
            if self.on_backend_changed:
                self.on_backend_changed(backend_display_name(backend))
            else:
                settings_manager.save_setting(SettingsKey.SELECTED_MODEL, backend)
        except Exception as exc:
            self._say(f"Couldn't change voice model: {exc}")
            self.refresh_engine_selection()
            return
        if VOICE_MODEL in self._built:
            blocker = self.engine_combo.blockSignals(True)
            self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(backend)))
            self.engine_combo.blockSignals(blocker)
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

    def _on_api_model_changed(self, _index: int = 0) -> None:
        model = self.api_model_combo.currentData()
        if model is None:
            return
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
        """The packaged SDK inside OpenWhisper's chat model tile."""
        core = self.meeting_agent_core_combo.currentData()
        if core is None or self.meeting_agent_is_installed():
            return
        if self._save_meeting_agent_core(core):
            self._apply_meeting_agent_choice(core)

    def _save_meeting_agent_core(self, core: str) -> bool:
        try:
            settings_manager.save_setting(SettingsKey.MEETING_AGENT_CORE, core)
        except Exception as exc:
            logger.error("Couldn't save meeting agent core: %s", exc)
            self._say(f"Couldn't save who runs AI insights: {exc}")
            return False
        self._refresh_rail_values()
        return True

    def _on_meeting_agent_choice(self, choice: str) -> None:
        """A tile was chosen: an installed agent, or OpenWhisper's own engine.

        OpenWhisper restores the packaged SDK the Agent core combo keeps,
        even when that SDK needs installing or updating. It never falls back
        to direct API calls.
        """
        if choice == BUILTIN:
            core = self.meeting_agent_core_combo.currentData() or MeetingAgentCore.PI
        elif choice in MeetingAgentCore.INSTALLED:
            core = choice
        else:
            return
        if not self._save_meeting_agent_core(core):
            return
        self._apply_meeting_agent_choice(core)
        if core in MeetingAgentCore.INSTALLED:
            self._say(f"AI insights will run through {self.meeting_text_summary()}")
        else:
            self._say(f"AI insights will run through {self.meeting_agent_picker.tiles[BUILTIN].name}")
            if self.rail.current_key() == MEETING_INTELLIGENCE:
                self._fetch_catalog_models(
                    self.meeting_model_picker.provider,
                    picker=self.meeting_model_picker,
                )

    def _on_meeting_agent_model_chosen(self, agent_id: str, model: str) -> None:
        """Save one agent's model, keeping every other agent's entry."""
        try:
            def merge(settings: dict) -> None:
                stored = settings.get(SettingsKey.MEETING_AGENT_MODELS)
                merged = dict(stored) if isinstance(stored, dict) else {}
                merged[agent_id] = model
                settings[SettingsKey.MEETING_AGENT_MODELS] = merged

            settings_manager.mutate_settings(merge)
        except Exception as exc:
            logger.error("Couldn't save the agent model: %s", exc)
            self._say(f"Couldn't save the model: {exc}")
            return
        self.meeting_agent_picker.set_saved_models(
            resolve_meeting_agent_models(self._settings_snapshot())
        )
        if agent_id == self.meeting_agent_core():
            self._say(f"AI insights will run through {self.meeting_text_summary()}")
        self._refresh_rail_values()

    def _apply_meeting_agent_choice(self, core: str) -> None:
        """Show the chosen agent, and chat settings only for packaged SDKs."""
        self.meeting_agent_picker.set_choice(core)
        builtin = core not in MeetingAgentCore.INSTALLED
        self.meeting_model_title.setVisible(builtin)
        self.meeting_model_tile.setVisible(builtin)
        self._refresh_meeting_agent_core_notice(core)

    def _refresh_meeting_agent_core_notice(self, core: str) -> None:
        """A missing SDK blocks AI insights, not recording or agent selection."""
        notice = ""
        if core == MeetingAgentCore.PI and not self._pi_payload_available:
            fix = ("Install or update Pi from Downloads → Components."
                   if is_frozen() else "Build the Pi sidecar, or install/update it from Downloads → Components.")
            notice = f"{fix} Or choose an installed coding agent above."
        elif core == MeetingAgentCore.OPENCODE and not self._opencode_payload_available:
            notice = "Install OpenCode SDK from Downloads → Components, or choose another agent above."
        if notice:
            notice += " Meetings can still record without AI insights; there is no direct API fallback."
        self.meeting_agent_core_notice.setText(notice)
        self.meeting_agent_core_notice.setVisible(bool(notice))

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
        host_snapshot = getattr(self._host, "_settings_snapshot", None)
        if host_snapshot is not None:
            return host_snapshot()
        try:
            return settings_manager.load_all_settings()
        except Exception:
            return {}

    def _page_is_loading(self, key: str) -> bool:
        return (key in self._built
                and self._host.__dict__.get("_initializing_page") in (None, key))

    def _connect_picker_profile_signals(self, picker: TextModelPicker) -> None:
        picker.add_endpoint_requested.connect(self._add_text_endpoint)
        picker.edit_endpoint_requested.connect(self._edit_text_endpoint)
        picker.delete_endpoint_requested.connect(self._delete_text_endpoint)

    def _refresh_picker_profiles(self) -> None:
        profiles = list_profiles(self._settings_snapshot())
        settings = self._settings_snapshot()
        for picker, key, page in ((self.__dict__.get("text_model_picker"), "cleanup_model_memory", CLEANUP),
                                  (self.__dict__.get("meeting_model_picker"), "meeting_model_memory", MEETING_INTELLIGENCE)):
            if picker is None or not self._page_is_loading(page):
                continue
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
                    protocol=payload.get("protocol", "chat"),
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
                    protocol=payload.get("protocol", profile.protocol),
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
        if picker is None or provider != picker.provider:
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
            self.__dict__.get("text_model_picker"), provider, sort, models, error
        )
        self._apply_catalog_to_picker(
            self.__dict__.get("meeting_model_picker"), provider, sort, models, error
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
            SETTING_DEFAULTS[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT],
        )
        if sort not in TranscriptCleanupModelSort.ALL:
            sort = config.TRANSCRIPT_CLEANUP_MODEL_SORT

        self._active_text_provider = provider
        self._active_text_model = model
        if not self._page_is_loading(CLEANUP):
            return
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
        if self._page_is_loading(MEETING_INTELLIGENCE):
            self._refresh_picker_profiles()
            self.meeting_model_picker.set_provider(provider, model)
            self.meeting_model_picker.set_active_selection(provider, model)
            self._sync_pi_core_availability(settings)
        if self._page_is_loading(MEETING_VOICE):
            blocker = self.meeting_source_combo.blockSignals(True)
            self.meeting_source_combo.setCurrentIndex(self.meeting_source_combo.findData(resolve_meeting_asr_source(settings)))
            self.meeting_source_combo.blockSignals(blocker)
            self._refresh_meeting_source()
            blocker = self.meeting_language_combo.blockSignals(True)
            self.meeting_language_combo.setCurrentIndex(max(0, self.meeting_language_combo.findData(resolve_meeting_language(settings))))
            self.meeting_language_combo.blockSignals(blocker)
            backend = resolve_meeting_speaker_id_backend(settings)
            blocker = self.meeting_speaker_id_combo.blockSignals(True)
            self.meeting_speaker_id_combo.setCurrentIndex(max(0, self.meeting_speaker_id_combo.findData(backend)))
            self.meeting_speaker_id_combo.blockSignals(blocker)
            self._speaker_id_backend_previous = backend
            self._refresh_speaker_id_status()

    def refresh_engine_selection(self) -> None:
        self._load_engine_and_runtime()
        self._refresh_engine_inventory()
        self._refresh_rail_values()

    def _load_engine_and_runtime(self) -> None:
        settings = self._settings_snapshot()
        if self._page_is_loading(VOICE_MODEL):
            try:
                model_value = settings_manager.load_model_selection()
            except Exception:
                model_value = config.DEFAULT_BACKEND
            blocker = self.engine_combo.blockSignals(True)
            index = self.engine_combo.findData(model_value)
            if index < 0:
                index = self.engine_combo.findData(config.DEFAULT_BACKEND)
            self.engine_combo.setCurrentIndex(max(0, index))
            self.engine_combo.blockSignals(blocker)
            self._update_engine_caption()
            self._update_ondemand_whisper_enabled()
            blocker = self.api_model_combo.blockSignals(True)
            self.api_model_combo.setCurrentIndex(max(0, self.api_model_combo.findData(resolve_api_transcription_model(settings))))
            self.api_model_combo.blockSignals(blocker)
        if self._page_is_loading(RUNTIME):
            device = setting_value(SettingsKey.WHISPER_DEVICE, settings)
            compute = setting_value(SettingsKey.WHISPER_COMPUTE_TYPE, settings)
            if self.device_combo.findText(str(device)) < 0:
                device = "auto"
            blocker = self.device_combo.blockSignals(True)
            self.device_combo.setCurrentText(str(device))
            self.device_combo.blockSignals(blocker)
            blocker = self.compute_combo.blockSignals(True)
            if self.compute_combo.findText(str(compute)) < 0:
                self.compute_combo.addItem(str(compute))
            self.compute_combo.setCurrentText(str(compute))
            self.compute_combo.blockSignals(blocker)
        self._refresh_meeting_runtime_label()

    def _update_engine_caption(self) -> None:
        value = self.engine_combo.currentData() or "local_whisper"
        from services.local_asr.catalog import BACKENDS, DEFAULT_MODELS, MODELS
        caption = MODELS[DEFAULT_MODELS[value]].purpose + ". Runs locally." if value in BACKENDS else _ENGINE_CAPTIONS.get(value, "")
        display_name = _display_name_for_backend(value)
        tip = backend_picker_tooltip(display_name)
        if tip != display_name:
            caption += " " + tip
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
        # Neither engine keeps anything on this computer.
        on_this_computer = backend not in ("api", "remote")
        self.engine_inventory_title.setVisible(on_this_computer)
        self.engine_inventory_row.setVisible(on_this_computer)

    def _engine_filter(self) -> str:
        """Downloads backend filter for the selected recording engine."""
        backend = self.engine_combo.currentData() or WHISPER_FILTER
        return "" if backend in ("api", "remote") else backend

    def _refresh_engine_inventory(self) -> None:
        if VOICE_MODEL not in self._built:
            return
        """Say what the selected engine has on this computer."""
        backend = self.engine_combo.currentData() or WHISPER_FILTER
        if backend in ("api", "remote"):
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
                custom = custom_models(self._settings_snapshot())
                present += sum(custom_model_cache_info(name) is not None for name in custom)
                text = f"{present} of {len(repos) + len(custom)} Whisper models on this computer."
            else:
                from services.local_asr.cache import is_cached
                from services.local_asr.catalog import (
                    BACKENDS,
                    MODELS,
                    selected_device,
                )
                keys = [key for key, model in MODELS.items() if model.backend == backend]
                present = sum(1 for key in keys if is_cached(key))
                noun = "model" if len(keys) == 1 else "models"
                text = (
                    f"{present} of {len(keys)} {BACKENDS[backend]} {noun} on this "
                    "computer"
                )
                key = (backend, selected_device(backend, self._settings_snapshot()))
                self._engine_inventory_prefix = text
                text += self._engine_runtime_labels.get(key, ".")
                self._engine_inventory_label_key = key
                self.engine_inventory_label.setText(text)
                if key not in self._engine_runtime_pending:
                    self._engine_runtime_pending.add(key)
                    if self._background_cache_scan:
                        threading.Thread(
                            target=self._check_engine_runtime,
                            args=(key,),
                            name="settings-engine-runtime",
                            daemon=True,
                        ).start()
                    else:
                        self._check_engine_runtime(key)
                return
        except Exception:
            logger.debug("Engine inventory lookup failed", exc_info=True)
            text = "Open Downloads to see which models are on this computer."
        self.engine_inventory_label.setText(text)

    def _check_engine_runtime(self, key: tuple[str, str]) -> None:
        # Auto detection can import CTranslate2/PyTorch. Keep it off the Qt
        # thread even when the user only opens Overview or General.
        try:
            from services.components import is_installed
            from services.local_asr.catalog import resolve_runtime

            component, _device = resolve_runtime(*key)
            name = component_coordinator.describe(component).display_name
            state = "installed" if is_installed(component) else "not installed"
            label = f" · {name} {state}."
        except Exception:
            logger.debug("Engine runtime lookup failed", exc_info=True)
            label = ". Open Downloads to check its runtime."
        try:
            self._engine_runtime_checked.emit(key, label)
        except RuntimeError:
            pass  # Settings was destroyed while detection was running.

    def _on_engine_runtime_checked(self, key: tuple[str, str], label: str) -> None:
        self._engine_runtime_pending.discard(key)
        self._engine_runtime_labels[key] = label
        if (
            self.engine_combo.currentData() == key[0]
            and self._engine_inventory_label_key == key
        ):
            self.engine_inventory_label.setText(self._engine_inventory_prefix + label)

    def _refresh_meeting_runtime_label(self) -> None:
        if MEETING_VOICE not in self._built:
            return
        if resolve_meeting_asr_source(self._settings_snapshot()) == "remote":
            from services.remote_asr.settings import load_client_pairing
            pairing = load_client_pairing(self._settings_snapshot())
            host = pairing.host_name if pairing else "your paired computer"
            self.meeting_runtime_label.setText(
                f"Microphone and system audio are sent to {host} over an encrypted connection. "
                "Recordings and transcripts are saved on this computer. Uses the host's "
                "selected meeting-capable model and device; change them in Configure remote "
                "engine → Manage host models. Changes there affect all connected clients. "
                "If disconnected, recording continues here and transcription retries. "
                "Speaker identification and AI insights have separate settings."
            )
            return
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
        settings = self._settings_snapshot()
        device = str(setting_value(SettingsKey.WHISPER_DEVICE, settings))
        compute = str(setting_value(SettingsKey.WHISPER_COMPUTE_TYPE, settings))
        self.meeting_runtime_label.setText(
            f"Device and quantization come from Models & storage → Runtime "
            f"({device} · {compute}) and are shared with dictation's Local "
            "Whisper."
        )

    def _refresh_speaker_id_status(self) -> None:
        if MEETING_VOICE not in self._built:
            return
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
                "saved transcript. Requires an OpenAI API key (Settings → API "
                "keys). This speaker pass does not upload microphone audio. OpenAI "
                "retires the model this uses (gpt-4o-transcribe-diarize) on "
                "February 26, 2027; after that, on-device labels are used."
            )
            return
        # The model is not a Downloads component: the first meeting that
        # needs it fetches it into a per-user cache (ensure_speaker_model).
        if speaker_model_path():
            self.speaker_id_status.setText(
                "On-device WeSpeaker (voxceleb_resnet34_LM.onnx) is ready on "
                "this computer."
            )
        else:
            self.speaker_id_status.setText(
                "On-device WeSpeaker (voxceleb_resnet34_LM.onnx, about 26 MB) "
                "downloads from Hugging Face when your next meeting starts. "
                "If Hugging Face access is set to Never connect, meetings use "
                "Me/Others channel labels instead."
            )

    # ---- refresh ----

    def _sync_pi_core_availability(self, settings: Optional[dict] = None) -> None:
        """Refresh the Pi item and who runs AI insights from settings.

        Settings is non-modal and cached, so ``_pi_payload_available`` cannot
        stay as the value computed in ``__init__``. While an installed agent
        is chosen, the Agent core combo holds the built-in core OpenWhisper
        would return to.
        """
        self._pi_payload_available = meeting_agent_payload_dir() is not None
        self._opencode_payload_available = meeting_agent_payload_dir("opencode") is not None
        combo = self.__dict__.get("meeting_agent_core_combo")
        if combo is None:
            return
        combo.setItemText(0, self._pi_label())
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
        builtin = core
        if core in MeetingAgentCore.INSTALLED:
            builtin = combo.currentData() or MeetingAgentCore.PI
        core_index = combo.findData(builtin)
        blocker = combo.blockSignals(True)
        combo.setCurrentIndex(max(0, core_index))
        combo.blockSignals(blocker)
        self.meeting_agent_picker.set_saved_models(resolve_meeting_agent_models(snapshot))
        self._apply_meeting_agent_choice(core)

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
            SettingsKey.WHISPER_MODEL, SETTING_DEFAULTS[SettingsKey.WHISPER_MODEL]
        )
        custom = custom_models(settings)
        if active_model not in [*config.WHISPER_MODEL_CHOICES, *custom]:
            active_model = config.DEFAULT_WHISPER_MODEL
        meeting_model = resolve_meeting_whisper_model(settings)
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None

        if self._page_is_loading(VOICE_MODEL):
            self.ondemand_whisper_picker.set_options(cached, active_model, resolved=loaded_model, custom=custom)
            self._update_ondemand_whisper_enabled()
        if self._page_is_loading(MEETING_VOICE):
            self.meeting_whisper_picker.set_options(cached, meeting_model, custom=custom)
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
        engine_value = self._engine_value()
        if engine_value == "local_whisper":
            model = self.ondemand_whisper_picker.current_model() if VOICE_MODEL in self._built else setting_value(SettingsKey.WHISPER_MODEL, self._settings_snapshot())
            return f"Local Whisper · {model}"
        if engine_value == "api":
            model = self.api_model_combo.currentData() if VOICE_MODEL in self._built else resolve_api_transcription_model(self._settings_snapshot())
            return f"API · {model or config.DEFAULT_API_MODEL}"
        if engine_value == "remote":
            pairing = self._remote_pairing()
            return f"Remote · {pairing.host_name}" if pairing else "Remote · not paired"
        if VOICE_MODEL in self._built:
            return self.speech_controls.model_combo.currentText()
        from services.local_asr.catalog import MODELS, selected_model
        model = selected_model(engine_value, self._settings_snapshot())
        return MODELS[model].label if model in MODELS else model

    def voice_detail(self) -> str:
        """Where dictation runs, for the Overview card."""
        engine_value = self._engine_value()
        if engine_value == "api":
            return "Sent to OpenAI for transcription"
        if engine_value == "remote":
            pairing = self._remote_pairing()
            if pairing is None:
                return "Pair with a host in Remote engine"
            return f"Sent to {pairing.host_name} on your network"
        if engine_value == "local_whisper":
            device = setting_value(SettingsKey.WHISPER_DEVICE, self._settings_snapshot())
            return f"On this computer · {device}"
        from services.local_asr.catalog import selected_device
        return f"On this computer · {selected_device(engine_value, self._settings_snapshot())}"

    def voice_is_remote(self) -> bool:
        """True when dictation audio leaves this computer."""
        return self._engine_value() in ("api", "remote")

    def voice_is_cloud(self) -> bool:
        return self._engine_value() == "api"

    @staticmethod
    def _remote_pairing():
        from services.remote_asr.settings import load_client_pairing

        try:
            return load_client_pairing()
        except Exception:
            return None

    def text_summary(self) -> str:
        provider = profile_display_name(
            self._active_text_provider, self._settings_snapshot()
        )
        return f"{provider} · {self._active_text_model}"

    def meeting_model_label(self) -> str:
        if self._meeting_source() == "remote":
            return "Remote computer"
        from services.local_asr.catalog import MODELS
        model = self.meeting_whisper_picker.current_model() if MEETING_VOICE in self._built else resolve_meeting_whisper_model(self._settings_snapshot())
        return MODELS[model].label if model in MODELS else model

    def meeting_voice_summary(self) -> str:
        language = meeting_language_label(
            self.meeting_language_combo.currentData() if MEETING_VOICE in self._built else resolve_meeting_language(self._settings_snapshot())
        )
        return f"{self.meeting_model_label()} · {language}"

    def meeting_voice_detail(self) -> str:
        language = meeting_language_label(
            self.meeting_language_combo.currentData() if MEETING_VOICE in self._built else resolve_meeting_language(self._settings_snapshot())
        )
        backend = self.meeting_speaker_id_combo.currentData() if MEETING_VOICE in self._built else resolve_meeting_speaker_id_backend(self._settings_snapshot())
        speakers = speaker_id_label(backend)
        detail = f"{language} · {speakers}"
        if self._meeting_source() == "remote":
            detail += " · audio sent to paired host"
        return detail

    def speaker_id_is_remote(self) -> bool:
        return resolve_meeting_speaker_id_backend(self._settings_snapshot()) == MeetingSpeakerIdBackend.OPENAI

    def meeting_agent_core(self) -> str:
        """The saved ``MeetingAgentCore``: a packaged SDK or an installed agent."""
        return resolve_meeting_agent_core(self._settings_snapshot())

    def meeting_agent_is_installed(self) -> bool:
        """True when an installed coding agent runs AI insights."""
        return self.meeting_agent_core() in MeetingAgentCore.INSTALLED

    def meeting_text_summary(self) -> str:
        """Who runs AI insights, as the rail shows it."""
        settings = self._settings_snapshot()
        core = resolve_meeting_agent_core(settings)
        if core in MeetingAgentCore.INSTALLED:
            scanned = self.meeting_agent_picker.agents() if MEETING_INTELLIGENCE in self._built else None
            if scanned is not None:
                name = installed_agents.AGENT_SPECS[core].name
                agent = scanned.get(core)
                if agent is None:
                    return f"{name} · not installed"
                if agent.problem:
                    return f"{name} · update needed"
                if agent.signed_in is False:
                    return f"{name} · sign in needed"
            return installed_agents.describe_choice(
                core, resolve_meeting_agent_model(core, settings)
            )
        provider = profile_display_name(self._active_meeting_provider, settings)
        return f"{provider} · {self._active_meeting_llm_model}"

    def meeting_intelligence_overview(self) -> tuple:
        """``(value, detail)`` for the Overview's Meeting intelligence card."""
        settings = self._settings_snapshot()
        core = resolve_meeting_agent_core(settings)
        if core in MeetingAgentCore.INSTALLED:
            model = resolve_meeting_agent_model(core, settings)
            name = installed_agents.AGENT_SPECS[core].name
            agent = self.meeting_agent_picker.agent(core) if MEETING_INTELLIGENCE in self._built else None
            account = agent.account if agent is not None else ""
            detail = f"{name} · your {account} sign-in" if account else f"{name} · your sign-in"
            return installed_agents.model_display_name(core, model), detail
        provider = profile_display_name(self._active_meeting_provider, settings)
        return (
            self._active_meeting_llm_model,
            f"{provider} · {self.meeting_agent_core_label()}",
        )

    def meeting_intelligence_is_remote(self, provider_is_remote: Callable[[str], bool]) -> bool:
        """Whether AI insights send transcript text off this computer.

        An installed agent sends it to whichever provider it signs in to,
        which OpenWhisper cannot see, so it counts as leaving.
        """
        if self.meeting_agent_is_installed():
            return True
        return provider_is_remote(self._active_meeting_provider)

    def meeting_model_name(self) -> str:
        return self._active_meeting_llm_model

    def meeting_agent_core_label(self) -> str:
        if self.meeting_agent_is_installed():
            return agent_core_label(self.meeting_agent_core())
        core = self.meeting_agent_core_combo.currentData() if MEETING_INTELLIGENCE in self._built else resolve_meeting_agent_core(self._settings_snapshot())
        return agent_core_label(core)

    def runtime_summary(self) -> str:
        settings = self._settings_snapshot()
        device = self.device_combo.currentText() if RUNTIME in self._built else setting_value(SettingsKey.WHISPER_DEVICE, settings)
        compute = self.compute_combo.currentText() if RUNTIME in self._built else setting_value(SettingsKey.WHISPER_COMPUTE_TYPE, settings)
        return f"{device} · {compute}"

    def _refresh_rail_values(self) -> None:
        """Mirror each model destination's current assignment into the rail."""
        self.rail.set_value(VOICE_MODEL, self.voice_summary())
        self.rail.set_value(MEETING_VOICE, self.meeting_voice_summary())
        self.rail.set_value(MEETING_INTELLIGENCE, self.meeting_text_summary())
        self.rail.set_value(RUNTIME, self.runtime_summary())
        self.assignments_changed.emit()
