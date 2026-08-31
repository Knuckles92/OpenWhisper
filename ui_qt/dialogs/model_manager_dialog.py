"""Model Manager dialog for on-demand and meeting model assignment.

A left rail lists the five things that can be assigned — on-demand voice and
text cleanup, meeting voice and intelligence, and the shared Whisper runtime —
and shows each one's current value next to its name, so the whole configuration
is readable without navigating. Every destination is sized to fit the window;
nothing here scrolls. The Whisper catalog and optional components live in
``DownloadsDialog``, reached from the rail footer.

Unlike the app's other dialogs this one is NON-modal (``show()``, not
``exec()``): downloads are long-running and the user should be able to keep
recording and transcribing while the manager is open. ``UIController`` holds
a single instance and re-raises it instead of stacking copies.
"""
import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root, config
from services.components import (
    ComponentId,
    ComponentState,
    component_coordinator,
    meeting_agent_payload_dir,
)
from services.hf_access import (
    CachedModelInfo,
    format_size_bytes,
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
    default_transcript_cleanup_model,
    resolve_meeting_agent_core,
    resolve_meeting_audio_upload_consent,
    resolve_meeting_language,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    settings_manager,
)
from services.text_llm import (
    get_profile,
    list_profiles,
    profile_display_name,
    remove_custom_profile,
    upsert_custom_profile,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, ElidingComboBox
from ui_qt.widgets.local_model_picker import LocalModelPicker
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.text_model_picker import TextModelPicker
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

_ENGINE_CAPTIONS = {
    "local_whisper": "Local faster-whisper on this computer.",
    "api_whisper": "OpenAI cloud model whisper-1.",
    "api_gpt4o": "OpenAI cloud model gpt-4o-transcribe.",
    "api_gpt4o_mini": "OpenAI cloud model gpt-4o-mini-transcribe.",
}

# Rail destination keys. Stable identifiers used by callers that deep-link into
# one destination, so they never depend on rail order.
ONDEMAND_VOICE = "ondemand_voice"
ONDEMAND_TEXT = "ondemand_text"
MEETING_VOICE = "meeting_voice"
MEETING_TEXT = "meeting_text"
SHARED_RUNTIME = "shared_runtime"


def _design_icon(filename: str) -> QIcon:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    # Preserve the semantic icon color for disabled current-state buttons.
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def _display_name_for_backend(model_value: str) -> str:
    for display, value in config.MODEL_VALUE_MAP.items():
        if value == model_value:
            return display
    return config.MODEL_CHOICES[0]


class ModelManagerDialog(QDialog):
    #: The height floor is measured: 620 is just above the 601 px the tallest
    #: destination needs under the themed font metrics, so no page scrolls. The
    #: width floor is deliberately well above Qt's own minimum — the eliding
    #: combos and labels would happily shrink past the point where a model id or
    #: an endpoint URL is still readable.
    DEFAULT_SIZE = QSize(980, 660)
    MINIMUM_SIZE = QSize(840, 620)
    COMPUTE_CHOICES = ("auto", "float16", "float32", "int8")

    #: Opening the Downloads window is UIController's job — it owns dialog
    #: lifetimes and already routes download progress signals.
    downloads_requested = pyqtSignal()
    _text_models_loaded = pyqtSignal(str, str, list, str)
    _cache_scan_finished = pyqtSignal(int, object)

    def __init__(
        self,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        parent=None,
        background_cache_scan: bool = True,
    ):
        """Assign models, reporting which local model the engine has loaded.

        Args:
            get_loaded_model: Provider returning the model name currently
                loaded by the engine (or None). Used to resolve what "auto"
                actually means in the on-demand voice destination.
        """
        super().__init__(parent)
        self._get_loaded_model = get_loaded_model
        self._background_cache_scan = bool(background_cache_scan)
        self._cache_scan_generation = 0
        self._cache_inventory_loading = False
        self._text_models_cache: Dict[tuple, list] = {}
        self._text_models_loading = set()
        self._active_text_provider = TranscriptCleanupProvider.OPENAI
        self._active_text_model = default_transcript_cleanup_model(
            self._active_text_provider
        )
        self._active_meeting_provider = TranscriptCleanupProvider.OPENROUTER
        self._active_meeting_llm_model = config.MEETING_LLM_MODEL
        self._pi_payload_available = meeting_agent_payload_dir() is not None

        self.setWindowTitle("Model Manager")
        self.setWindowIcon(app_icon())
        self.setObjectName("modelManagerDialog")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)

        self._setup_ui()
        self.setMinimumSize(self.MINIMUM_SIZE)
        self.resize(self.DEFAULT_SIZE)
        self._text_models_loaded.connect(self._on_text_models_loaded)
        self._cache_scan_finished.connect(self._on_cache_scan_finished)
        self.rail.select(ONDEMAND_VOICE)
        self.refresh()

    # ---- construction ----

    def _setup_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_rail_pane())
        root.addWidget(self._build_body(), stretch=1)

    def _build_rail_pane(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("modelManagerRailPane")
        pane.setFixedWidth(NavRail.RAIL_WIDTH)
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(16, 16, 16, 12)
        brand.setSpacing(10)
        brand_icon = QLabel()
        brand_icon.setObjectName("modelManagerHeaderIcon")
        brand_icon.setPixmap(app_icon().pixmap(26, 26))
        brand_icon.setFixedSize(28, 28)
        brand.addWidget(brand_icon)
        brand_title = QLabel("Model Manager")
        brand_title.setObjectName("modelManagerRailBrand")
        brand.addWidget(brand_title)
        brand.addStretch()
        column.addLayout(brand)

        self.rail = NavRail()
        self.rail.add_group("On-demand")
        self.rail.add_destination(
            ONDEMAND_VOICE, "Voice", _design_icon("microphone-blue.svg")
        )
        self.rail.add_destination(
            ONDEMAND_TEXT, "Text cleanup", _design_icon("stack-purple.svg")
        )
        self.rail.add_group("Meeting Mode")
        self.rail.add_destination(
            MEETING_VOICE, "Voice", _design_icon("microphone-blue.svg")
        )
        self.rail.add_destination(
            MEETING_TEXT, "Intelligence", _design_icon("stack-purple.svg")
        )
        self.rail.add_group("Shared")
        self.rail.add_destination(
            SHARED_RUNTIME, "Runtime", _design_icon("box-blue.svg")
        )
        self.rail.destination_changed.connect(self._on_destination_changed)
        column.addWidget(self.rail, stretch=1)

        footer = QVBoxLayout()
        footer.setContentsMargins(16, 10, 16, 14)
        footer.setSpacing(8)
        self.cache_summary_label = WrappedLabel("")
        self.cache_summary_label.setObjectName("modelManagerRailFootnote")
        footer.addWidget(self.cache_summary_label)
        self.downloads_button = Button("Downloads…")
        self.downloads_button.setObjectName("modelManagerDownloadsButton")
        self.downloads_button.set_base_minimum_size(0, 34)
        self.downloads_button.setToolTip(
            "Download Whisper models and optional components"
        )
        self.downloads_button.clicked.connect(self.downloads_requested.emit)
        footer.addWidget(self.downloads_button)
        column.addLayout(footer)
        return pane

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("modelManagerBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(14)

        self.page_title = QLabel("")
        self.page_title.setObjectName("modelManagerTitle")
        self.page_subtitle = WrappedLabel("")
        self.page_subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(self.page_title)
        layout.addWidget(self.page_subtitle)

        self.stack = QStackedWidget()
        self.stack.setObjectName("modelManagerStack")
        self._pages: Dict[str, QWidget] = {}
        self._headings: Dict[str, tuple] = {}
        self._add_page(
            ONDEMAND_VOICE,
            "On-demand voice",
            "The transcription engine used by Quick Record, hotkey dictation, "
            "and Upload File.",
            self._build_ondemand_voice_page,
        )
        self._add_page(
            ONDEMAND_TEXT,
            "On-demand text cleanup",
            "The chat model that rewrites a finished dictation when AI cleanup "
            "is enabled.",
            self._build_ondemand_text_page,
        )
        self._add_page(
            MEETING_VOICE,
            "Meeting voice",
            "Meetings load their own Whisper instance for live captions and the "
            "optional end-of-meeting re-decode.",
            self._build_meeting_voice_page,
        )
        self._add_page(
            MEETING_TEXT,
            "Meeting intelligence",
            "One chat model runs every Meeting Mode pass: live cards, the note "
            "taker, polish, summaries, and the final report.",
            self._build_meeting_text_page,
        )
        self._add_page(
            SHARED_RUNTIME,
            "Shared runtime",
            "How local Whisper models are executed. Both on-demand and Meeting "
            "Mode inherit these.",
            self._build_runtime_page,
        )
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message_label = WrappedLabel("")
        self.message_label.setObjectName("modelManagerMessage")
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Close")
        close_btn.setObjectName("modelManagerCloseButton")
        self._compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(footer)
        return body

    def _add_page(
        self, key: str, title: str, subtitle: str, builder: Callable
    ) -> None:
        page = QWidget()
        page.setObjectName(f"modelManagerPage_{key}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        builder(layout)
        layout.addStretch()
        self._pages[key] = page
        self._headings[key] = (title, subtitle)
        self.stack.addWidget(page)

    def _field(self, label: str, widget: QWidget) -> QWidget:
        """Wrap a control with the field label the rail destination shows."""
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

    def _build_ondemand_voice_page(self, layout: QVBoxLayout) -> None:
        self.engine_combo = ElidingComboBox()
        self.engine_combo.setObjectName("ondemandEngineCombo")
        self.engine_combo.setMinimumHeight(40)
        for display in config.MODEL_CHOICES:
            self.engine_combo.addItem(display, config.MODEL_VALUE_MAP[display])
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        layout.addWidget(self._field("Recording engine", self.engine_combo))

        self.engine_caption = self._caption("")
        layout.addWidget(self.engine_caption)

        self.ondemand_whisper_picker = LocalModelPicker()
        self.ondemand_whisper_picker.model_changed.connect(
            self._on_set_active_clicked
        )
        self.ondemand_whisper_picker.manage_downloads_requested.connect(
            self.downloads_requested.emit
        )
        layout.addWidget(
            self._field("Local Whisper model", self.ondemand_whisper_picker)
        )

        layout.addWidget(
            self._footnote(
                "Live streaming preview always uses a fixed tiny.en instance, "
                "separate from the model chosen here."
            )
        )

    def _build_ondemand_text_page(self, layout: QVBoxLayout) -> None:
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
        layout.addWidget(self.text_model_picker)

        layout.addWidget(
            self._footnote(
                "Called only when AI cleanup is enabled. API keys are entered "
                "in Settings → API keys; a blank key variable means no auth. "
                "Cleanup behavior, prompts, and learned rules remain in "
                "Settings → Cleanup."
            )
        )

    def _build_meeting_voice_page(self, layout: QVBoxLayout) -> None:
        self.meeting_whisper_picker = LocalModelPicker()
        self.meeting_whisper_picker.model_changed.connect(
            self._on_meeting_set_active_clicked
        )
        self.meeting_whisper_picker.manage_downloads_requested.connect(
            self.downloads_requested.emit
        )
        layout.addWidget(
            self._field("Meeting Whisper model", self.meeting_whisper_picker)
        )

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
        layout.addWidget(
            self._field("Spoken language", self.meeting_language_combo)
        )

        self.meeting_speaker_id_combo = ElidingComboBox()
        self.meeting_speaker_id_combo.setObjectName("meetingSpeakerIdCombo")
        self.meeting_speaker_id_combo.setMinimumHeight(40)
        self.meeting_speaker_id_combo.addItem(
            "On-device (WeSpeaker · Speaker 1, Speaker 2, …)",
            MeetingSpeakerIdBackend.LOCAL,
        )
        self.meeting_speaker_id_combo.addItem(
            "OpenAI (gpt-4o-transcribe-diarize, system audio after End)",
            MeetingSpeakerIdBackend.OPENAI,
        )
        self.meeting_speaker_id_combo.currentIndexChanged.connect(
            self._on_speaker_id_backend_changed
        )
        layout.addWidget(
            self._field(
                "Speaker identification", self.meeting_speaker_id_combo
            )
        )
        self.speaker_id_status = self._caption("")
        layout.addWidget(self.speaker_id_status)

        self.meeting_runtime_label = self._caption("")
        layout.addWidget(self.meeting_runtime_label)

    def _build_meeting_text_page(self, layout: QVBoxLayout) -> None:
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
        layout.addWidget(self.meeting_model_picker)

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
        self.meeting_agent_core_combo.currentIndexChanged.connect(
            self._on_meeting_agent_core_changed
        )
        layout.addWidget(
            self._field("Agent core", self.meeting_agent_core_combo)
        )
        layout.addWidget(
            self._footnote(
                "Agent core decides how the chat model is called, not which "
                "model. The Pi sidecar is installed from Downloads. Cloud "
                "consent, knowledge folder, and report views stay in "
                "Settings → Meeting."
            )
        )

    def _build_runtime_page(self, layout: QVBoxLayout) -> None:
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
        layout.addLayout(runtime_row)

        layout.addWidget(
            self._caption(
                "auto picks CUDA when a supported GPU is present and falls back "
                "to CPU otherwise. Changing either value reloads the local "
                "engine."
            )
        )
        layout.addWidget(
            self._footnote(
                "Downloaded models and optional components are managed in "
                "Downloads. Deleting a model there does not change these "
                "assignments."
            )
        )

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Size a shared button for the dialog's compact footer.

        Uses ``width`` as a preferred size floor, but never caps the maximum
        below the polished sizeHint so text is not clipped on macOS (where theme
        font metrics differ from the Button constructor font).
        """
        button.set_base_minimum_size(width, 34)
        button.ensurePolished()
        height = max(34, button.sizeHint().height())
        button.setMinimumHeight(height)
        button.setMaximumHeight(height)
        fitted = max(width, button.minimumWidth(), button.sizeHint().width())
        button.setMinimumWidth(fitted)
        button.setMaximumWidth(fitted)

    #: Assigned by UIController.
    on_set_active_requested: Optional[Callable[[str], None]] = None
    on_backend_changed: Optional[Callable[[str], None]] = None
    on_runtime_settings_changed: Optional[Callable[[], None]] = None

    # ---- navigation ----

    def select_destination(self, key: str) -> None:
        """Show one rail destination by stable key."""
        if key in self._pages:
            self.rail.select(key)

    def show_ondemand_tab(self) -> None:
        self.select_destination(ONDEMAND_VOICE)

    def show_text_tab(self) -> None:
        self.select_destination(ONDEMAND_TEXT)

    def show_meeting_tab(self) -> None:
        self.select_destination(MEETING_VOICE)

    def show_runtime(self) -> None:
        self.select_destination(SHARED_RUNTIME)

    def _on_destination_changed(self, key: str) -> None:
        """Swap the page, its heading, and load any catalog it needs."""
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        title, subtitle = self._headings[key]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        if key == ONDEMAND_TEXT:
            self._fetch_catalog_models(
                self.text_model_picker.provider, picker=self.text_model_picker
            )
        elif key == MEETING_TEXT:
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
            settings_manager.save_setting(
                SettingsKey.MEETING_WHISPER_MODEL, model_name
            )
        except Exception as exc:
            logger.error("Couldn't set meeting Whisper model: %s", exc)
            self.message_label.setText(
                f"Couldn't set meeting transcription model: {exc}"
            )
            return
        self.message_label.setText(
            f'Meeting transcription model set to "{model_name}"'
        )
        self.refresh()

    def _on_engine_changed(self, _index: int) -> None:
        """Route a recording-engine change through the main-window path."""
        display = self.engine_combo.currentText()
        if self.on_backend_changed:
            self.on_backend_changed(display)
        self._update_engine_caption()
        self._update_ondemand_whisper_enabled()
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
            self.message_label.setText(f"Couldn't save device or quant: {exc}")
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
            self.message_label.setText(f"Couldn't save spoken language: {exc}")

    def _on_meeting_agent_core_changed(self, _index: int) -> None:
        core = self.meeting_agent_core_combo.currentData()
        if core is None:
            return
        try:
            settings_manager.save_setting(SettingsKey.MEETING_AGENT_CORE, core)
        except Exception as exc:
            logger.error("Couldn't save meeting agent core: %s", exc)
            self.message_label.setText(f"Couldn't save agent core: {exc}")

    def _on_speaker_id_backend_changed(self, _index: int = 0) -> None:
        """Ask for audio-upload consent when the user picks OpenAI speaker ID."""
        backend = self.meeting_speaker_id_combo.currentData()
        if backend == MeetingSpeakerIdBackend.OPENAI:
            if not resolve_meeting_audio_upload_consent():
                from ui_qt.dialogs.meeting_audio_consent_dialog import (
                    MeetingAudioConsentDialog,
                )

                dialog = MeetingAudioConsentDialog(self)
                dialog.exec()
                granted = (
                    dialog.result_action == MeetingAudioConsentDialog.RESULT_ENABLE
                )
                if granted:
                    settings_manager.save_setting(
                        SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN, True,
                    )
                else:
                    local_index = self.meeting_speaker_id_combo.findData(
                        MeetingSpeakerIdBackend.LOCAL
                    )
                    blocker = self.meeting_speaker_id_combo.blockSignals(True)
                    self.meeting_speaker_id_combo.setCurrentIndex(
                        max(0, local_index)
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
            self.message_label.setText(
                f"Couldn't save speaker identification: {exc}"
            )
        self._refresh_speaker_id_status()

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
        self.text_model_picker.set_profiles(profiles)
        self.meeting_model_picker.set_profiles(profiles)

    def _add_text_endpoint(self) -> None:
        from ui_qt.dialogs.text_endpoint_dialog import TextEndpointDialog

        dialog = TextEndpointDialog(parent=self)
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
            self.message_label.setText(f"Couldn't add endpoint: {exc}")
            return
        self._refresh_picker_profiles()
        sender = self.sender()
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
        self.message_label.setText(f'Added endpoint "{profile.name}"')

    def _edit_text_endpoint(self, profile_id: str) -> None:
        from ui_qt.dialogs.text_endpoint_dialog import TextEndpointDialog

        profile = get_profile(profile_id, self._settings_snapshot())
        if profile is None or profile.builtin:
            return
        dialog = TextEndpointDialog(profile, parent=self)
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
            self.message_label.setText(f"Couldn't save endpoint: {exc}")
            return
        self._refresh_picker_profiles()
        self.text_model_picker.set_provider(updated.id)
        self.meeting_model_picker.set_provider(
            self.meeting_model_picker.provider
        )
        self.message_label.setText(f'Updated endpoint "{updated.name}"')

    def _delete_text_endpoint(self, profile_id: str) -> None:
        """Delete a custom endpoint that is not currently assigned."""
        settings = self._settings_snapshot()
        profile = get_profile(profile_id, settings)
        if profile is None or profile.builtin:
            return
        cleanup_id = resolve_transcript_cleanup_provider(settings)
        meeting_id = resolve_meeting_llm_provider(settings)
        if profile_id in (cleanup_id, meeting_id):
            self.message_label.setText(
                f'"{profile.name}" is in use. Choose another text model '
                "before deleting this endpoint."
            )
            return
        confirmed = QMessageBox.question(
            self,
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
            self.message_label.setText(f"Couldn't delete endpoint: {exc}")
            return
        self._refresh_picker_profiles()
        self.message_label.setText(f'Deleted endpoint "{profile.name}"')

    # ---- catalog loading ----

    def _on_text_provider_changed(self, provider: str) -> None:
        if self.rail.current_key() == ONDEMAND_TEXT:
            self._fetch_catalog_models(
                provider, picker=self.text_model_picker
            )

    def _on_meeting_provider_changed(self, provider: str) -> None:
        if self.rail.current_key() == MEETING_TEXT:
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

    def _fetch_catalog_models(
        self,
        provider: str,
        picker: TextModelPicker,
        force: bool = False,
    ) -> None:
        """Load one provider's chat-model catalog on a worker thread.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            picker: On-demand or Meeting picker that requested the catalog.
            force: Bypass the in-dialog cache when true.
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
                self._text_models_loaded.emit(provider, sort, models, error)
            except RuntimeError:
                pass  # Dialog was destroyed before the catalog finished.

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
            picker.status_label.setText(f"Couldn't load models: {error}")
            return
        picker.set_models(models)
        picker.status_label.setText(f"{len(models)} models available")

    def _on_text_models_loaded(
        self, provider: str, sort: str, models: list, error: str
    ) -> None:
        """Apply a provider catalog result on the Qt thread."""
        key = (provider, sort)
        self._text_models_loading.discard(key)
        if not error:
            self._text_models_cache[key] = models
        self._apply_catalog_to_picker(
            self.text_model_picker, provider, sort, models, error
        )
        self._apply_catalog_to_picker(
            self.meeting_model_picker, provider, sort, models, error
        )

    def _activate_text_model(self, provider: str) -> None:
        if provider != self.text_model_picker.provider:
            return
        model = self.text_model_picker.model_combo.currentText().strip()
        if not model:
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
            })
        except Exception as exc:
            logger.error("Couldn't activate text model: %s", exc)
            self.message_label.setText(f"Couldn't set text model: {exc}")
            return

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_active_selection(provider, model)
        display_provider = profile_display_name(
            provider, self._settings_snapshot()
        )
        self.message_label.setText(
            f"Text model set to {display_provider} · {model}"
        )
        self._refresh_rail_values()

    def _activate_meeting_llm_model(self, provider: str) -> None:
        if provider != self.meeting_model_picker.provider:
            return
        model = self.meeting_model_picker.model_combo.currentText().strip()
        if not model:
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
            })
        except Exception as exc:
            logger.error("Couldn't activate meeting LLM model: %s", exc)
            self.message_label.setText(f"Couldn't set meeting model: {exc}")
            return

        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        self.meeting_model_picker.set_active_selection(provider, model)
        display_provider = profile_display_name(
            provider, self._settings_snapshot()
        )
        self.message_label.setText(
            f"Meeting intelligence model set to {display_provider} · {model}"
        )
        self._refresh_rail_values()

    # ---- state loading ----

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

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_sort(sort)
        self._refresh_picker_profiles()
        self.text_model_picker.set_provider(provider, model)
        self.text_model_picker.set_active_selection(provider, model)

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

        backend_index = self.meeting_speaker_id_combo.findData(
            resolve_meeting_speaker_id_backend(settings)
        )
        blocker = self.meeting_speaker_id_combo.blockSignals(True)
        self.meeting_speaker_id_combo.setCurrentIndex(max(0, backend_index))
        self.meeting_speaker_id_combo.blockSignals(blocker)
        self._refresh_speaker_id_status()

    def _load_engine_and_runtime(self) -> None:
        try:
            model_value = settings_manager.load_model_selection()
        except Exception:
            model_value = "local_whisper"
        display = _display_name_for_backend(model_value)
        index = self.engine_combo.findText(display)
        blocker = self.engine_combo.blockSignals(True)
        self.engine_combo.setCurrentIndex(max(0, index))
        self.engine_combo.blockSignals(blocker)
        self._update_engine_caption()
        self._update_ondemand_whisper_enabled()

        settings = self._settings_snapshot()
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
        self.engine_caption.setText(_ENGINE_CAPTIONS.get(value, ""))

    def _update_ondemand_whisper_enabled(self) -> None:
        is_local = self.engine_combo.currentData() == "local_whisper"
        self.ondemand_whisper_picker.setEnabled(is_local)
        if not is_local:
            self.ondemand_whisper_picker.set_caption(
                "Local Whisper size is used only when the recording engine "
                "is Local Whisper."
            )

    def _refresh_meeting_runtime_label(self) -> None:
        device = self.device_combo.currentText() or "auto"
        compute = self.compute_combo.currentText() or "auto"
        self.meeting_runtime_label.setText(
            f"Device and quantization come from Shared → Runtime "
            f"({device} · {compute}) and are shared with on-demand Local "
            "Whisper."
        )

    def _refresh_speaker_id_status(self) -> None:
        backend = self.meeting_speaker_id_combo.currentData()
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

        The dialog is non-modal and cached, so ``_pi_payload_available`` cannot
        stay as the value computed in ``__init__``.
        """
        self._pi_payload_available = meeting_agent_payload_dir() is not None
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
        snapshot = settings if settings is not None else self._settings_snapshot()
        core = resolve_meeting_agent_core(snapshot)
        if core == MeetingAgentCore.PI and not self._pi_payload_available:
            core = MeetingAgentCore.DIRECT
        core_index = combo.findData(core)
        blocker = combo.blockSignals(True)
        combo.setCurrentIndex(max(0, core_index))
        combo.blockSignals(blocker)

    def refresh_component_state(self) -> None:
        """Re-read component install state that this dialog reports on."""
        self._sync_pi_core_availability()
        self._refresh_speaker_id_status()

    def refresh(self) -> None:
        self._load_text_settings()
        self._load_meeting_settings()
        self._load_engine_and_runtime()
        if not self._background_cache_scan:
            self._cache_inventory_loading = False
            self._refresh_cached_model_state(
                scan_cached_models(max_age_seconds=30.0)
            )
            return

        cached = peek_cached_models()
        self._cache_inventory_loading = cached is None
        self._refresh_cached_model_state(cached or {})
        self._cache_scan_generation += 1
        generation = self._cache_scan_generation

        def load() -> None:
            result = scan_cached_models(max_age_seconds=30.0)
            self._cache_scan_finished.emit(generation, result)

        threading.Thread(
            target=load,
            name="model-manager-cache-scan",
            daemon=True,
        ).start()

    def _on_cache_scan_finished(self, generation: int, cached) -> None:
        if generation != self._cache_scan_generation:
            return
        self._cache_inventory_loading = False
        self._refresh_cached_model_state(dict(cached or {}))

    def _refresh_cached_model_state(
        self,
        cached: Dict[str, CachedModelInfo],
    ) -> None:
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
        self._update_cache_summary(cached)
        self._refresh_rail_values()

    def set_downloading(self, model_name: str) -> None:
        self.refresh()

    def set_download_progress(self, model_name: str, done: int, total: int) -> None:
        return

    def finish_download(self, model_name: str, success: bool) -> None:
        self.refresh()

    def _update_cache_summary(self, cached: Dict[str, CachedModelInfo]) -> None:
        """Report cache totals in the rail footer, next to the Downloads button."""
        if self._cache_inventory_loading:
            self.cache_summary_label.setText("Checking downloaded models…")
            return
        catalog_repos = {
            resolve_model_repo(model_name)
            for model_name in config.WHISPER_MODEL_CHOICES
            if model_name != "auto"
        }
        present = {
            repo: info for repo, info in cached.items() if repo in catalog_repos
        }
        total_bytes = sum(info.size_bytes for info in present.values())
        self.cache_summary_label.setText(
            f"{len(present)} of {len(catalog_repos)} models downloaded · "
            f"{format_size_bytes(total_bytes)}"
        )

    def _refresh_rail_values(self) -> None:
        """Mirror each destination's current assignment into its rail item."""
        engine_value = self.engine_combo.currentData() or "local_whisper"
        engine_display = self.engine_combo.currentText()
        if engine_value == "local_whisper":
            engine_display = (
                f"Local Whisper · {self.ondemand_whisper_picker.current_model()}"
            )
        self.rail.set_value(ONDEMAND_VOICE, engine_display)
        self.rail.set_value(
            ONDEMAND_TEXT,
            f"{profile_display_name(self._active_text_provider, self._settings_snapshot())}"
            f" · {self._active_text_model}",
        )
        self.rail.set_value(
            MEETING_VOICE, self.meeting_whisper_picker.current_model()
        )
        self.rail.set_value(
            MEETING_TEXT,
            f"{profile_display_name(self._active_meeting_provider, self._settings_snapshot())}"
            f" · {self._active_meeting_llm_model}",
        )
        self.rail.set_value(
            SHARED_RUNTIME,
            f"{self.device_combo.currentText()} · "
            f"{self.compute_combo.currentText()}",
        )
