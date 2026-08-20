"""Model Manager dialog for on-demand and meeting model assignment.

Two mode tabs (On-demand, Meeting Mode) assign the models each product
surface uses. The Library tab owns the shared Whisper cache, optional
components, and the device/quantization runtime both modes inherit.

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

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QTabWidget,
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
    get_hf_cache_dir,
    resolve_model_repo,
    scan_cached_models,
)
from services.model_catalog import get_model_details
from services.settings import (
    MeetingAgentCore,
    MeetingLanguage,
    MeetingSpeakerIdBackend,
    SettingsKey,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
    is_hf_hub_offline_env_set,
    resolve_meeting_agent_core,
    resolve_meeting_audio_upload_consent,
    resolve_meeting_language,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    settings_manager,
)
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, NoWheelComboBox
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets.local_model_picker import LocalModelPicker
from ui_qt.widgets.model_row_widget import ModelRowWidget
from ui_qt.widgets.text_model_picker import TextModelPicker
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

_ENGINE_CAPTIONS = {
    "local_whisper": "Local faster-whisper on this computer.",
    "api_whisper": "OpenAI cloud model whisper-1.",
    "api_gpt4o": "OpenAI cloud model gpt-4o-transcribe.",
    "api_gpt4o_mini": "OpenAI cloud model gpt-4o-mini-transcribe.",
}


def _design_icon(filename: str) -> QIcon:
    """Load a bundled Tabler icon used by the Model Manager."""
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    # Preserve the semantic icon color for disabled current-state buttons.
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def _display_name_for_backend(model_value: str) -> str:
    """Return the main-window combo label for an internal backend value."""
    for display, value in config.MODEL_VALUE_MAP.items():
        if value == model_value:
            return display
    return config.MODEL_CHOICES[0]


class _CompactStat(QWidget):
    """Small inline statistic used in the Model Manager summary."""

    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        self.setObjectName("modelManagerStat")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.value = QLabel(value)
        self.value.setObjectName("modelManagerStatValue")
        caption = QLabel(label)
        caption.setObjectName("modelManagerStatLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)

    def set_value(self, value: str) -> None:
        """Update the displayed statistic value."""
        self.value.setText(value)


class ModelManagerDialog(QDialog):
    """Non-modal home for on-demand, meeting, and library model selection."""

    DEFAULT_SIZE = QSize(900, 620)
    MINIMUM_SIZE = QSize(720, 480)
    COMPUTE_CHOICES = ("auto", "float16", "float32", "int8")

    # Re-emitted for the controller; the dialog never installs anything itself.
    component_install_requested = pyqtSignal(str)
    component_cancel_requested = pyqtSignal(str)
    component_remove_requested = pyqtSignal(str)
    _text_models_loaded = pyqtSignal(str, str, list, str)

    def __init__(
        self,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        parent=None,
    ):
        """Initialize the Model Manager.

        Args:
            get_loaded_model: Provider returning the model name currently
                loaded by the engine (or None). Used to disable Delete on the
                in-use model, whose files are memory-mapped.
        """
        super().__init__(parent)
        self._get_loaded_model = get_loaded_model
        self._downloading_model: Optional[str] = None
        self._component_rows: Dict[str, ComponentRowWidget] = {}
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
        self.setMinimumSize(self.MINIMUM_SIZE)

        self._setup_ui()
        self.resize(self.DEFAULT_SIZE)
        self._text_models_loaded.connect(self._on_text_models_loaded)
        self.refresh()

    # ── Construction ───────────────────────────────────────────────

    def _build_components_section(self) -> QVBoxLayout:
        """Build the optional-components group shown above the model list.

        Components live here rather than in Settings because this dialog is
        deliberately non-modal — a multi-gigabyte download must not lock the
        user out of the app — and because Settings commits on accept, which
        does not compose with an in-flight install.

        Returns an empty layout on platforms with no installable components, so
        no heading advertises a section with nothing in it.
        """
        section = QVBoxLayout()
        section.setSpacing(6)

        infos = component_coordinator.list_components()
        if not infos:
            return section

        heading = QLabel("Components")
        heading.setObjectName("headerLabel")
        section.addWidget(heading)

        caption = QLabel(
            "Optional add-ons. These are downloaded on demand so the "
            "installer stays small."
        )
        caption.setObjectName("infoLabel")
        caption.setWordWrap(True)
        section.addWidget(caption)

        for info in infos:
            row = ComponentRowWidget(info.component_id)
            row.install_clicked.connect(self.component_install_requested)
            row.cancel_clicked.connect(self.component_cancel_requested)
            row.remove_clicked.connect(self._confirm_component_removal)
            self._component_rows[info.component_id] = row
            section.addWidget(row)

        return section

    def refresh_components(self) -> None:
        """Re-read component state from disk and re-render every row."""
        for component_id, row in self._component_rows.items():
            row.update_state(
                component_coordinator.describe(component_id),
                component_coordinator.is_installing(component_id),
            )
        self._refresh_speaker_id_status()

    def set_component_progress(
        self, component_id: str, phase: str, done: int, total: int
    ) -> None:
        """Forward install progress to the matching row."""
        row = self._component_rows.get(component_id)
        if row is not None:
            row.set_progress(phase, done, total)

    def finish_component_install(
        self, component_id: str, success: bool, message: str
    ) -> None:
        """Render the outcome of an install attempt."""
        self.refresh_components()
        if message:
            self.message_label.setText(message)

    def _confirm_component_removal(self, component_id: str) -> None:
        """Ask before deleting a multi-gigabyte component."""
        info = component_coordinator.describe(component_id)
        confirmed = QMessageBox.question(
            self,
            "Remove component",
            f"Remove {info.display_name} "
            f"({format_size_bytes(info.install_bytes)})?\n\n"
            "You can install it again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed == QMessageBox.StandardButton.Yes:
            self.component_remove_requested.emit(component_id)

    def _setup_ui(self) -> None:
        """Build the shared shell and the On-demand / Meeting / Library tabs."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        brand_icon = QLabel()
        brand_icon.setObjectName("modelManagerHeaderIcon")
        brand_icon.setPixmap(app_icon().pixmap(44, 44))
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setFixedSize(52, 52)
        header.addWidget(brand_icon)

        title_block = QVBoxLayout()
        title_block.setSpacing(3)
        title = QLabel("Model Manager")
        title.setObjectName("modelManagerTitle")
        subtitle = QLabel(
            "Assign models for on-demand dictation and Meeting Mode. "
            "Downloads live in Library."
        )
        subtitle.setObjectName("modelManagerSubtitle")
        title_block.addStretch()
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_block.addStretch()
        header.addLayout(title_block, stretch=1)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("modelManagerTabs")
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setFixedWidth(750)
        self.tabs.setIconSize(QSize(20, 20))
        self.ondemand_tab = self._build_ondemand_tab()
        self.meeting_tab = self._build_meeting_tab()
        self.library_tab = self._build_library_tab()
        self.tabs.addTab(
            self.ondemand_tab,
            _design_icon("microphone-blue.svg"),
            "On-demand",
        )
        self.tabs.addTab(
            self.meeting_tab,
            _design_icon("stack-slate.svg"),
            "Meeting Mode",
        )
        self.tabs.addTab(
            self.library_tab,
            _design_icon("box-blue.svg"),
            "Library",
        )
        self.tabs.currentChanged.connect(self._on_manager_tab_changed)
        layout.addWidget(self.tabs, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message_label = QLabel("")
        self.message_label.setObjectName("modelManagerMessage")
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Close")
        close_btn.setObjectName("modelManagerCloseButton")
        self._compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _make_scroll_page(self, object_name: str):
        """Return a tab, its scroll area, and the inner content layout."""
        tab = QWidget()
        tab.setObjectName(object_name)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content.setObjectName(f"{object_name}Content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 12, 8, 4)
        layout.setSpacing(14)
        scroll.setWidget(content)
        tab_layout.addWidget(scroll)
        return tab, scroll, layout

    def _make_mode_card(
        self, title: str, subtitle: str, accent_name: str
    ) -> tuple:
        """Build a titled card used by the mode pages.

        Returns:
            The card frame and the body layout to populate.
        """
        card = QFrame()
        card.setObjectName("modelManagerModeCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        accent = QFrame()
        accent.setObjectName(accent_name)
        accent.setFixedSize(3, 38)
        heading.addWidget(accent)
        heading_copy = QVBoxLayout()
        heading_copy.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("modelManagerModeCardTitle")
        subtitle_label = WrappedLabel(subtitle)
        subtitle_label.setObjectName("modelManagerModeCardSubtitle")
        heading_copy.addWidget(title_label)
        heading_copy.addWidget(subtitle_label)
        heading.addLayout(heading_copy, stretch=1)
        card_layout.addLayout(heading)
        return card, card_layout

    def _labeled_combo(self, label: str, combo: QComboBox) -> QWidget:
        """Return a field label stacked over a combo."""
        wrapper = QWidget()
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("textModelFieldLabel")
        col.addWidget(caption)
        col.addWidget(combo)
        return wrapper

    def _build_ondemand_tab(self) -> QWidget:
        """Build Voice and Text assignment for Quick Record / Upload."""
        tab, scroll, layout = self._make_scroll_page("modelManagerOndemandTab")
        self.ondemand_scroll_area = scroll
        self.text_scroll_area = scroll

        heading = QLabel("On-demand")
        heading.setObjectName("textTabTitle")
        layout.addWidget(heading)
        intro = WrappedLabel(
            "Models used by Quick Record, hotkey dictation, and Upload File."
        )
        intro.setObjectName("textTabSubtitle")
        layout.addWidget(intro)

        voice_card, voice_layout = self._make_mode_card(
            "Voice",
            "Quick Record, hotkey dictation, and Upload File",
            "textTabAccent",
        )

        self.engine_combo = NoWheelComboBox()
        self.engine_combo.setObjectName("ondemandEngineCombo")
        self.engine_combo.setMinimumHeight(36)
        for display in config.MODEL_CHOICES:
            self.engine_combo.addItem(display, config.MODEL_VALUE_MAP[display])
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        voice_layout.addWidget(
            self._labeled_combo("Recording engine", self.engine_combo)
        )

        self.engine_caption = WrappedLabel("")
        self.engine_caption.setObjectName("infoLabel")
        voice_layout.addWidget(self.engine_caption)

        self.ondemand_whisper_picker = LocalModelPicker()
        self.ondemand_whisper_picker.model_changed.connect(
            self._on_set_active_clicked
        )
        self.ondemand_whisper_picker.manage_downloads_requested.connect(
            self.show_library_tab
        )
        voice_layout.addWidget(self.ondemand_whisper_picker)

        stream_note = WrappedLabel(
            "Live streaming preview always uses a fixed tiny.en instance, "
            "separate from the model chosen here."
        )
        stream_note.setObjectName("infoLabel")
        voice_layout.addWidget(stream_note)
        layout.addWidget(voice_card)

        self.text_card, text_layout = self._make_mode_card(
            "Text",
            "Chat model used by AI transcript cleanup after dictation.",
            "textTabAccent",
        )
        self.text_model_picker = TextModelPicker()
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
        text_layout.addWidget(self.text_model_picker)

        footnote_card = QFrame()
        footnote_card.setObjectName("textModelFootnoteCard")
        footnote_layout = QHBoxLayout(footnote_card)
        footnote_layout.setContentsMargins(14, 8, 14, 8)
        footnote_layout.setSpacing(12)
        footnote_icon = QLabel()
        footnote_icon.setObjectName("textModelFootnoteIcon")
        footnote_icon.setFixedSize(20, 20)
        footnote_icon.setPixmap(_design_icon("info-blue.svg").pixmap(18, 18))
        footnote_layout.addWidget(footnote_icon)
        note = WrappedLabel(
            "Text models are called only when AI cleanup is enabled. Cleanup "
            "behavior, prompts, and learned rules remain in Settings → Cleanup."
        )
        note.setObjectName("textModelFootnote")
        footnote_layout.addWidget(note, stretch=1)
        text_layout.addWidget(footnote_card)
        layout.addWidget(self.text_card)
        layout.addStretch()
        return tab

    def _build_meeting_tab(self) -> QWidget:
        """Build meeting ASR, speaker ID, and intelligence assignment."""
        tab, scroll, layout = self._make_scroll_page("modelManagerMeetingTab")
        self.meeting_scroll_area = scroll

        heading = QLabel("Meeting Mode")
        heading.setObjectName("meetingTabTitle")
        layout.addWidget(heading)
        intro = WrappedLabel(
            "Meetings use their own Whisper instance and one chat model for "
            "every intelligence pass."
        )
        intro.setObjectName("meetingTabSubtitle")
        layout.addWidget(intro)

        voice_card, voice_layout = self._make_mode_card(
            "Voice",
            "Live captions and the optional end-of-meeting re-decode.",
            "meetingTabAccent",
        )

        asr_caption = WrappedLabel(
            "Meetings load a second Whisper instance next to dictation, from "
            "the same cache. A large model can exhaust GPU memory."
        )
        asr_caption.setObjectName("infoLabel")
        voice_layout.addWidget(asr_caption)

        self.meeting_whisper_picker = LocalModelPicker()
        self.meeting_whisper_picker.model_changed.connect(
            self._on_meeting_set_active_clicked
        )
        self.meeting_whisper_picker.manage_downloads_requested.connect(
            self.show_library_tab
        )
        voice_layout.addWidget(self.meeting_whisper_picker)

        self.meeting_language_combo = NoWheelComboBox()
        self.meeting_language_combo.setObjectName("meetingLanguageCombo")
        self.meeting_language_combo.setMinimumHeight(36)
        for code, label in MeetingLanguage.CHOICES:
            self.meeting_language_combo.addItem(label, code)
        self.meeting_language_combo.setToolTip(
            "Choose the meeting language when known. This avoids unreliable "
            "language detection on short chunks and strong accents."
        )
        self.meeting_language_combo.currentIndexChanged.connect(
            self._on_meeting_language_changed
        )
        voice_layout.addWidget(
            self._labeled_combo("Spoken language", self.meeting_language_combo)
        )

        self.meeting_runtime_label = WrappedLabel("")
        self.meeting_runtime_label.setObjectName("infoLabel")
        voice_layout.addWidget(self.meeting_runtime_label)
        runtime_link = Button("Open shared runtime")
        runtime_link.setObjectName("modelManagerInlineLink")
        runtime_link.setFlat(True)
        runtime_link.setCursor(Qt.CursorShape.PointingHandCursor)
        runtime_link.clicked.connect(self.show_library_tab)
        voice_layout.addWidget(runtime_link, alignment=Qt.AlignmentFlag.AlignLeft)

        self.meeting_speaker_id_combo = NoWheelComboBox()
        self.meeting_speaker_id_combo.setObjectName("meetingSpeakerIdCombo")
        self.meeting_speaker_id_combo.setMinimumHeight(36)
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
        voice_layout.addWidget(
            self._labeled_combo(
                "Speaker identification", self.meeting_speaker_id_combo
            )
        )
        self.speaker_id_status = WrappedLabel("")
        self.speaker_id_status.setObjectName("infoLabel")
        voice_layout.addWidget(self.speaker_id_status)
        layout.addWidget(voice_card)

        text_card, text_layout = self._make_mode_card(
            "Text",
            "One chat model for every Meeting Mode intelligence pass.",
            "meetingTabAccent",
        )
        uses = WrappedLabel(
            "This model runs live cards, the note taker, polish, topic and "
            "rolling summary, and the final consolidation report. There is "
            "no separate fast-live or big-final model."
        )
        uses.setObjectName("infoLabel")
        text_layout.addWidget(uses)

        self.meeting_model_picker = TextModelPicker(
            idle_status="Open Meeting Mode to load the model catalog."
        )
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
        text_layout.addWidget(self.meeting_model_picker)

        self.meeting_agent_core_combo = NoWheelComboBox()
        self.meeting_agent_core_combo.setObjectName("meetingAgentCoreCombo")
        self.meeting_agent_core_combo.setMinimumHeight(36)
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
        text_layout.addWidget(
            self._labeled_combo("Agent core", self.meeting_agent_core_combo)
        )
        core_caption = WrappedLabel(
            "How the chat model is called, not which model. Install Meeting "
            "Intelligence Agent in Library for the Pi sidecar."
        )
        core_caption.setObjectName("infoLabel")
        text_layout.addWidget(core_caption)

        footnote_card = QFrame()
        footnote_card.setObjectName("textModelFootnoteCard")
        footnote_layout = QHBoxLayout(footnote_card)
        footnote_layout.setContentsMargins(14, 8, 14, 8)
        footnote_layout.setSpacing(12)
        footnote_icon = QLabel()
        footnote_icon.setObjectName("textModelFootnoteIcon")
        footnote_icon.setFixedSize(20, 20)
        footnote_icon.setPixmap(_design_icon("info-blue.svg").pixmap(18, 18))
        footnote_layout.addWidget(footnote_icon)
        note = WrappedLabel(
            "Cloud consent, knowledge folder, and report views stay in "
            "Settings → Meeting."
        )
        note.setObjectName("textModelFootnote")
        footnote_layout.addWidget(note, stretch=1)
        text_layout.addWidget(footnote_card)
        layout.addWidget(text_card)
        layout.addStretch()
        return tab

    def _build_library_tab(self) -> QWidget:
        """Build the shared Whisper cache, components, and runtime tab."""
        tab = QWidget()
        tab.setObjectName("modelManagerLibraryTab")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 12, 4, 4)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("Library")
        title.setObjectName("headerLabel")
        subtitle = QLabel("Download local Whisper models and optional components")
        subtitle.setObjectName("infoLabel")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header_row.addLayout(title_block)
        header_row.addStretch()
        open_folder_btn = Button("Open Folder")
        self._compact_button(open_folder_btn, 110)
        open_folder_btn.setToolTip(
            "Open the folder where downloaded models are stored"
        )
        open_folder_btn.clicked.connect(self._on_open_cache_folder)
        header_row.addWidget(open_folder_btn)
        layout.addLayout(header_row)

        cache_path = get_hf_cache_dir()
        cache_path_label = QLabel(f"Cache: {cache_path}")
        cache_path_label.setObjectName("modelManagerCachePath")
        cache_path_label.setToolTip(cache_path)
        layout.addWidget(cache_path_label)

        self.env_banner = QLabel(
            "Downloads are disabled by the HF_HUB_OFFLINE environment "
            "variable set outside this application."
        )
        self.env_banner.setObjectName("modelManagerEnvBanner")
        self.env_banner.setWordWrap(True)
        self.env_banner.setVisible(False)
        layout.addWidget(self.env_banner)

        runtime_card, runtime_layout = self._make_mode_card(
            "Shared runtime",
            "Device and quantization used by both on-demand and meeting "
            "Whisper loads.",
            "textTabAccent",
        )
        runtime_card.setObjectName("modelManagerRuntimeCard")
        runtime_row = QHBoxLayout()
        runtime_row.setSpacing(10)
        device_choices = (
            ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
        )
        self.device_combo = NoWheelComboBox()
        self.device_combo.setObjectName("libraryDeviceCombo")
        self.device_combo.addItems(device_choices)
        self.device_combo.setMinimumHeight(32)
        self.device_combo.currentTextChanged.connect(self._on_runtime_changed)
        self.compute_combo = NoWheelComboBox()
        self.compute_combo.setObjectName("libraryComputeCombo")
        self.compute_combo.addItems(self.COMPUTE_CHOICES)
        self.compute_combo.setMinimumHeight(32)
        self.compute_combo.currentTextChanged.connect(self._on_runtime_changed)
        runtime_row.addWidget(self._labeled_combo("Device", self.device_combo))
        runtime_row.addWidget(self._labeled_combo("Quant", self.compute_combo))
        runtime_layout.addLayout(runtime_row)
        layout.addWidget(runtime_card)

        layout.addLayout(self._build_components_section())

        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self.downloaded_stat = _CompactStat("downloaded", "0")
        self.disk_stat = _CompactStat("used", "0 B")
        stats_row.addWidget(self.downloaded_stat)
        divider = QLabel("•")
        divider.setObjectName("modelManagerStatLabel")
        stats_row.addWidget(divider)
        stats_row.addWidget(self.disk_stat)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("modelManagerSearch")
        self.filter_edit.setPlaceholderText("Search models")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter_edit, stretch=1)

        self.status_filter_combo = QComboBox()
        self.status_filter_combo.setObjectName("modelManagerStatusFilter")
        self.status_filter_combo.addItem("All", "all")
        self.status_filter_combo.addItem("Downloaded", "downloaded")
        self.status_filter_combo.addItem("Not downloaded", "not_downloaded")
        self.status_filter_combo.setToolTip("Filter by download status")
        self.status_filter_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.status_filter_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("modelManagerSort")
        self.sort_combo.addItem("Recommended", "recommended")
        self.sort_combo.addItem("Downloaded first", "downloaded")
        self.sort_combo.addItem("Smallest first", "size")
        self.sort_combo.addItem("Name A-Z", "name")
        self.sort_combo.setToolTip("Sort model list")
        self.sort_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.sort_combo)
        layout.addLayout(toolbar)

        self.library_scroll_area = QScrollArea()
        self.library_scroll_area.setObjectName("modelManagerLibraryScroll")
        self.library_scroll_area.setWidgetResizable(True)
        self.library_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.library_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.voice_scroll_area = self.library_scroll_area

        list_container = QWidget()
        self.list_layout = QVBoxLayout(list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)

        self.rows: Dict[str, ModelRowWidget] = {}
        for model_name in config.WHISPER_MODEL_CHOICES:
            if model_name == "auto":
                continue
            row = ModelRowWidget(model_name)
            row.download_clicked.connect(self._on_download_clicked)
            row.delete_clicked.connect(self._on_delete_clicked)
            row.set_active_clicked.connect(self._on_set_active_clicked)
            row.details_requested.connect(self._on_details_requested)
            self.rows[model_name] = row
            self.list_layout.addWidget(row)

        self.empty_label = QLabel("No models match")
        self.empty_label.setObjectName("infoLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self.list_layout.addWidget(self.empty_label)
        self.list_layout.addStretch()

        self.library_scroll_area.setWidget(list_container)
        layout.addWidget(self.library_scroll_area, stretch=1)
        return tab

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Size a shared button for the dialog's compact toolbar/footer.

        Uses ``width`` as a preferred size floor, but never caps maxWidth below
        the polished sizeHint so text like "Open Folder" is not clipped on
        macOS (where theme font metrics differ from the Button constructor font).
        """
        button.set_base_minimum_size(width, 34)
        button.setMinimumHeight(34)
        button.setMaximumHeight(34)
        button.ensurePolished()
        fitted = max(width, button.minimumWidth(), button.sizeHint().width())
        button.setMinimumWidth(fitted)
        button.setMaximumWidth(fitted)

    # ── Callback plumbing (dialog signals) ─────────────────────────

    #: Assigned by UIController; called with the model name.
    on_download_requested: Optional[Callable[[str], None]] = None
    on_delete_requested: Optional[Callable[[str], None]] = None
    on_set_active_requested: Optional[Callable[[str], None]] = None
    on_backend_changed: Optional[Callable[[str], None]] = None
    on_runtime_settings_changed: Optional[Callable[[], None]] = None

    def _on_download_clicked(self, model_name: str):
        if self.on_download_requested:
            self.on_download_requested(model_name)

    def _on_delete_clicked(self, model_name: str):
        reply = QMessageBox.question(
            self,
            "Delete Model",
            f'Delete the downloaded files for "{model_name}"?\n\n'
            "You can download the model again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self.on_delete_requested:
            self.on_delete_requested(model_name)

    def _on_set_active_clicked(self, model_name: str):
        if self.on_set_active_requested:
            self.on_set_active_requested(model_name)
        self.refresh()

    def _on_meeting_set_active_clicked(self, model_name: str) -> None:
        """Persist the Meeting Mode Whisper ASR selection."""
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

    def _on_runtime_changed(self, _text: str = "") -> None:
        """Persist shared device/quant and ask the controller to reload."""
        try:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.WHISPER_DEVICE] = self.device_combo.currentText()
            settings[SettingsKey.WHISPER_COMPUTE_TYPE] = (
                self.compute_combo.currentText()
            )
            settings_manager.save_all_settings(settings)
        except Exception as exc:
            logger.error("Couldn't save shared Whisper runtime: %s", exc)
            self.message_label.setText(f"Couldn't save device or quant: {exc}")
            return
        if self.on_runtime_settings_changed:
            self.on_runtime_settings_changed()
        self._refresh_meeting_runtime_label()

    def _on_meeting_language_changed(self, _index: int) -> None:
        """Persist the meeting spoken-language pin."""
        language = self.meeting_language_combo.currentData()
        if language is None:
            return
        try:
            settings_manager.save_setting(SettingsKey.MEETING_LANGUAGE, language)
        except Exception as exc:
            logger.error("Couldn't save meeting language: %s", exc)
            self.message_label.setText(f"Couldn't save spoken language: {exc}")

    def _on_meeting_agent_core_changed(self, _index: int) -> None:
        """Persist the meeting agent core (how the chat model is called)."""
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

    def _on_details_requested(self, model_name: str) -> None:
        """Open the bundled technical profile for a selected model."""
        if model_name == "auto":
            return
        details = get_model_details(model_name)
        dialog = ModelDetailsDialog(details, parent=self)
        dialog.exec()

    def _on_open_cache_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_hf_cache_dir()))

    def _settings_snapshot(self) -> dict:
        """Load settings, or an empty dict when the store is unavailable."""
        try:
            return settings_manager.load_all_settings()
        except Exception:
            return {}

    def _load_text_settings(self) -> None:
        """Load the active cleanup choice into the on-demand text picker."""
        provider = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER,
            config.TRANSCRIPT_CLEANUP_PROVIDER,
        )
        if provider not in TranscriptCleanupProvider.ALL:
            provider = config.TRANSCRIPT_CLEANUP_PROVIDER

        model = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL,
            default_transcript_cleanup_model(provider),
        )
        if not isinstance(model, str) or not model.strip():
            model = default_transcript_cleanup_model(provider)
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
        self.text_model_picker.set_provider(provider, model)
        self.text_model_picker.set_active_selection(provider, model)

    def _load_meeting_settings(self) -> None:
        """Load meeting Whisper/LLM/extra choices into the Meeting tab."""
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

        core = resolve_meeting_agent_core(settings)
        if core == MeetingAgentCore.PI and not self._pi_payload_available:
            core = MeetingAgentCore.DIRECT
        core_index = self.meeting_agent_core_combo.findData(core)
        blocker = self.meeting_agent_core_combo.blockSignals(True)
        self.meeting_agent_core_combo.setCurrentIndex(max(0, core_index))
        self.meeting_agent_core_combo.blockSignals(blocker)

        backend_index = self.meeting_speaker_id_combo.findData(
            resolve_meeting_speaker_id_backend(settings)
        )
        blocker = self.meeting_speaker_id_combo.blockSignals(True)
        self.meeting_speaker_id_combo.setCurrentIndex(max(0, backend_index))
        self.meeting_speaker_id_combo.blockSignals(blocker)
        self._refresh_speaker_id_status()

    def _load_engine_and_runtime(self) -> None:
        """Refresh the on-demand engine combo and shared device/quant."""
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
        """Explain the concrete model behind the selected recording engine."""
        value = self.engine_combo.currentData() or "local_whisper"
        self.engine_caption.setText(_ENGINE_CAPTIONS.get(value, ""))

    def _update_ondemand_whisper_enabled(self) -> None:
        """Disable the local-size picker when the engine is not Local Whisper."""
        is_local = self.engine_combo.currentData() == "local_whisper"
        self.ondemand_whisper_picker.setEnabled(is_local)
        if not is_local:
            self.ondemand_whisper_picker.set_caption(
                "Local Whisper size is used only when the recording engine "
                "is Local Whisper."
            )

    def _refresh_meeting_runtime_label(self) -> None:
        """Show the shared device/quant that meeting ASR inherits."""
        device = self.device_combo.currentText() or "auto"
        compute = self.compute_combo.currentText() or "auto"
        self.meeting_runtime_label.setText(
            f"Device and quantization come from Library ({device} · {compute}) "
            "and are shared with on-demand Local Whisper."
        )

    def _refresh_speaker_id_status(self) -> None:
        """Describe the selected speaker-ID backend and component state."""
        backend = self.meeting_speaker_id_combo.currentData()
        if backend == MeetingSpeakerIdBackend.OPENAI:
            self.speaker_id_status.setText(
                "Uploads system audio after End and relabels speakers on the "
                "local transcript. Requires OPENAI_API_KEY. Microphone audio "
                "stays on this computer."
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
                "Speaker Identification in Library if live labels are missing."
            )

    def show_text_tab(self) -> None:
        """Select On-demand and scroll to the cleanup text card."""
        self.tabs.setCurrentWidget(self.ondemand_tab)
        self.ondemand_scroll_area.ensureWidgetVisible(self.text_card)

    def show_meeting_tab(self) -> None:
        """Select the Meeting Mode tab."""
        self.tabs.setCurrentWidget(self.meeting_tab)

    def show_library_tab(self) -> None:
        """Select the Library tab (downloads and shared runtime)."""
        self.tabs.setCurrentWidget(self.library_tab)

    def show_ondemand_tab(self) -> None:
        """Select the On-demand tab."""
        self.tabs.setCurrentWidget(self.ondemand_tab)

    def _on_manager_tab_changed(self, index: int) -> None:
        """Load the selected provider catalog when a mode tab opens."""
        widget = self.tabs.widget(index)
        if widget is self.ondemand_tab:
            self._fetch_catalog_models(
                self.text_model_picker.provider,
                picker=self.text_model_picker,
            )
        elif widget is self.meeting_tab:
            self._fetch_catalog_models(
                self.meeting_model_picker.provider,
                picker=self.meeting_model_picker,
            )

    def _on_text_provider_changed(self, provider: str) -> None:
        """Load a newly selected provider in the cleanup model picker."""
        if self.tabs.currentWidget() is self.ondemand_tab:
            self._fetch_catalog_models(
                provider, picker=self.text_model_picker
            )

    def _on_meeting_provider_changed(self, provider: str) -> None:
        """Load a newly selected provider in the meeting LLM picker."""
        if self.tabs.currentWidget() is self.meeting_tab:
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
        """Persist one provider/model pair as the cleanup model."""
        if provider != self.text_model_picker.provider:
            return
        model = self.text_model_picker.model_combo.currentText().strip()
        if not model:
            return
        try:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] = provider
            settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] = model
            settings_manager.save_all_settings(settings)
        except Exception as exc:
            logger.error("Couldn't activate text model: %s", exc)
            self.message_label.setText(f"Couldn't set text model: {exc}")
            return

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_active_selection(provider, model)
        display_provider = "OpenAI" if provider == "openai" else "OpenRouter"
        self.message_label.setText(
            f"Text model set to {display_provider} · {model}"
        )

    def _activate_meeting_llm_model(self, provider: str) -> None:
        """Persist one provider/model pair as the meeting intelligence model."""
        if provider != self.meeting_model_picker.provider:
            return
        model = self.meeting_model_picker.model_combo.currentText().strip()
        if not model:
            return
        try:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.MEETING_LLM_PROVIDER] = provider
            settings[SettingsKey.MEETING_LLM_MODEL] = model
            settings_manager.save_all_settings(settings)
        except Exception as exc:
            logger.error("Couldn't activate meeting LLM model: %s", exc)
            self.message_label.setText(f"Couldn't set meeting model: {exc}")
            return

        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        self.meeting_model_picker.set_active_selection(provider, model)
        display_provider = "OpenAI" if provider == "openai" else "OpenRouter"
        self.message_label.setText(
            f"Meeting intelligence model set to {display_provider} · {model}"
        )

    def _usage_for(
        self, model_name: str, dictation_model: str, meeting_model: str
    ) -> str:
        """Return the Library usage chip for one catalog row."""
        uses = []
        if model_name == dictation_model:
            uses.append("On-demand")
        if model_name == meeting_model:
            uses.append("Meetings")
        return " · ".join(uses)

    # ── State updates ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Refresh cache state and every mode-page assignment."""
        self._load_text_settings()
        self._load_meeting_settings()
        self._load_engine_and_runtime()
        self.refresh_components()
        cached = scan_cached_models()
        settings = self._settings_snapshot()
        active_model = settings_manager.get(
            SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL
        )
        if active_model not in config.WHISPER_MODEL_CHOICES:
            active_model = config.DEFAULT_WHISPER_MODEL
        meeting_model = resolve_meeting_whisper_model(settings)
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None
        dictation_resolved = active_model
        if active_model == "auto" and loaded_model:
            dictation_resolved = loaded_model
        loaded_repo = resolve_model_repo(loaded_model) if loaded_model else None
        downloads_blocked = is_hf_hub_offline_env_set()
        self.env_banner.setVisible(downloads_blocked)

        self.ondemand_whisper_picker.set_options(
            cached, active_model, resolved=loaded_model
        )
        self._update_ondemand_whisper_enabled()
        self.meeting_whisper_picker.set_options(cached, meeting_model)

        seen_repos: Dict[str, CachedModelInfo] = {}
        for model_name, row in self.rows.items():
            info = cached.get(row.repo_id)
            if info is not None:
                seen_repos[row.repo_id] = info
            row.update_state(
                info,
                is_active=False,
                is_loaded=(row.repo_id == loaded_repo),
                downloading=(model_name == self._downloading_model),
                downloads_blocked=downloads_blocked,
                download_slot_busy=(self._downloading_model is not None),
            )
            row.set_active_button.setVisible(False)
            row.set_usage(
                self._usage_for(model_name, dictation_resolved, meeting_model)
            )

        self.downloaded_stat.set_value(str(len(seen_repos)))
        total_bytes = sum(info.size_bytes for info in seen_repos.values())
        self.disk_stat.set_value(format_size_bytes(total_bytes))
        self._apply_filter(self.filter_edit.text())

    def set_downloading(self, model_name: str) -> None:
        """Mark a model as downloading (badge + disabled buttons)."""
        self._downloading_model = model_name
        self.message_label.setText(f'Downloading "{model_name}"…')
        self.refresh()

    def finish_download(self, model_name: str, success: bool) -> None:
        """Clear the downloading state once a download ends."""
        if self._downloading_model == model_name:
            self._downloading_model = None
        self.message_label.setText(
            "" if success else f'Download of "{model_name}" failed'
        )
        self.refresh()

    def show_delete_result(self, model_name: str, success: bool, error: str) -> None:
        """Report a delete outcome (row refresh arrives via cache-changed)."""
        if success:
            self.message_label.setText(f'Deleted "{model_name}"')
        else:
            self.message_label.setText(f"Could not delete: {error}")

    # ── Filter ─────────────────────────────────────────────────────

    def _apply_filter(self, _value=None):
        """Filter and sort rows using the current toolbar selections."""
        text = self.filter_edit.text()
        needle = text.strip().lower()
        status = self.status_filter_combo.currentData()
        any_visible = False
        rows = sorted(self.rows.values(), key=self._sort_key)
        for index, row in enumerate(rows):
            self.list_layout.insertWidget(index, row)
            visible = row.matches_filter(needle) if needle else True
            if status == "downloaded":
                visible = visible and row.is_cached
            elif status == "not_downloaded":
                visible = visible and not row.is_cached
            row.setVisible(visible)
            any_visible = any_visible or visible
        self.empty_label.setVisible(not any_visible)

    def _sort_key(self, row: ModelRowWidget):
        """Return a stable sort key for the selected built-in ordering."""
        mode = self.sort_combo.currentData()
        name = row.model_name.casefold()
        if mode == "downloaded":
            return (not row.is_cached, name)
        if mode == "size":
            return (row.sort_size_bytes, name)
        if mode == "name":
            return (name,)
        # Recommended: downloaded first, then smallest — keep order stable when
        # the active model changes so Set Active does not jump the row.
        return (not row.is_cached, row.sort_size_bytes, name)
