"""
Upload File Tab widget.
Provides drag-and-drop and browse-based audio file upload with inline
file preview, transcription controls, and transcript display.
"""
import logging
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTextEdit, QFileDialog, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData
from PyQt6.QtGui import QFont, QDragEnterEvent, QDropEvent, QMouseEvent

from config import config
from services.audio_processor import AudioFilePreview, audio_processor
from ui_qt.widgets.cards import Card, HeaderCard
from ui_qt.widgets.buttons import PrimaryButton, DangerButton, Button
from ui_qt.widgets.stats_display import TranscriptionStatsWidget

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = ('.wav', '.mp3', '.m4a', '.ogg', '.flac', '.wma')
AUDIO_FILTERS = (
    "Audio Files (*.wav *.mp3 *.m4a *.ogg *.flac *.wma);;"
    "WAV Files (*.wav);;MP3 Files (*.mp3);;All Files (*.*)"
)


class DropZoneWidget(QFrame):
    """Drag-and-drop zone that also opens a file browser on click."""

    file_selected = pyqtSignal(str)

    _LABEL_RESET = "background: transparent; border: none;"

    _IDLE_STYLE = """
        QFrame#dropZone {
            background-color: #222236;
            border: 2px dashed #4a4a6a;
            border-radius: 16px;
        }
        QFrame#dropZone:hover {
            border-color: #6366f1;
            background-color: #28284a;
        }
    """
    _HOVER_STYLE = """
        QFrame#dropZone {
            background-color: #2a2a50;
            border: 2px solid #0a84ff;
            border-radius: 16px;
        }
    """
    _REJECT_STYLE = """
        QFrame#dropZone {
            background-color: #2e2232;
            border: 2px dashed #ff453a;
            border-radius: 16px;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(190)
        self.setStyleSheet(self._IDLE_STYLE)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(24, 28, 24, 28)
        layout.setSpacing(4)

        icon_label = QLabel("\U0001F3B5")
        icon_label.setFont(QFont("Segoe UI Emoji", 36))
        icon_label.setStyleSheet(self._LABEL_RESET)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)

        layout.addSpacing(6)

        title = QLabel("Drag and drop audio file here")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: #e0e0ff; {self._LABEL_RESET}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("or click to browse")
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setStyleSheet(f"color: #7d7d9a; {self._LABEL_RESET}")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        formats = QLabel("WAV  ·  MP3  ·  M4A  ·  OGG  ·  FLAC  ·  WMA")
        formats.setFont(QFont("Segoe UI", 10))
        formats.setStyleSheet(f"color: #55556e; {self._LABEL_RESET}")
        formats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(formats)

    def _is_valid_audio(self, path: str) -> bool:
        return path.lower().endswith(SUPPORTED_EXTENSIONS)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if self._is_valid_audio(url.toLocalFile()):
                    event.acceptProposedAction()
                    self.setStyleSheet(self._HOVER_STYLE)
                    return
            self.setStyleSheet(self._REJECT_STYLE)
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._IDLE_STYLE)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet(self._IDLE_STYLE)
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if self._is_valid_audio(path):
                self.file_selected.emit(path)
                return
        event.ignore()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_file_browser()

    def open_file_browser(self):
        """Open the native file dialog for audio file selection."""
        audio_path, _ = QFileDialog.getOpenFileName(
            self, "Select Audio File", "", AUDIO_FILTERS
        )
        if audio_path:
            self.file_selected.emit(audio_path)


class FileInfoCard(Card):
    """Inline file information display with Transcribe and Remove buttons."""

    transcribe_clicked = pyqtSignal()
    remove_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview: AudioFilePreview | None = None
        self._setup_ui()

    def _setup_ui(self):
        self.filename_label = QLabel()
        self.filename_label.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self.filename_label.setStyleSheet("color: #00d4ff;")
        self.filename_label.setWordWrap(True)
        self.layout.addWidget(self.filename_label)

        self.details_label = QLabel()
        self.details_label.setFont(QFont("Segoe UI", 11))
        self.details_label.setStyleSheet("color: #e0e0ff;")
        self.layout.addWidget(self.details_label)

        self.audio_info_label = QLabel()
        self.audio_info_label.setFont(QFont("Segoe UI", 10))
        self.audio_info_label.setStyleSheet("color: #a0a0c0;")
        self.layout.addWidget(self.audio_info_label)

        self.chunk_label = QLabel()
        self.chunk_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self.chunk_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.chunk_label.hide()
        self.layout.addWidget(self.chunk_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.remove_btn = Button("Remove")
        self.remove_btn.clicked.connect(self.remove_clicked.emit)
        btn_layout.addWidget(self.remove_btn)

        btn_layout.addStretch()

        self.transcribe_btn = PrimaryButton("Transcribe")
        self.transcribe_btn.setMinimumWidth(120)
        self.transcribe_btn.clicked.connect(self.transcribe_clicked.emit)
        btn_layout.addWidget(self.transcribe_btn)

        self.layout.addLayout(btn_layout)

    def set_preview(self, preview: AudioFilePreview):
        """Populate the card with file preview data."""
        self._preview = preview
        self.filename_label.setText(preview.file_name)
        self.details_label.setText(
            f"Size: {preview.file_size_formatted}    "
            f"Duration: {preview.duration_formatted}"
        )
        stereo_mono = "Stereo" if preview.channels == 2 else "Mono"
        self.audio_info_label.setText(
            f"{preview.sample_rate} Hz, {stereo_mono}"
        )

        if preview.needs_splitting:
            self.chunk_label.setText(
                f"⚠ Will be split into {preview.estimated_chunks} chunks"
            )
            self.chunk_label.setStyleSheet(
                "color: #fbbf24; font-size: 11px; font-weight: bold;"
            )
            self.chunk_label.show()
        else:
            self.chunk_label.setText("Will be transcribed in one pass")
            self.chunk_label.setStyleSheet(
                "color: #34d399; font-size: 11px; font-weight: bold;"
            )
            self.chunk_label.show()

    def set_transcribing(self, active: bool):
        """Toggle button states during transcription."""
        self.transcribe_btn.setEnabled(not active)
        self.remove_btn.setEnabled(not active)
        if active:
            self.transcribe_btn.setText("Transcribing...")
        else:
            self.transcribe_btn.setText("Transcribe")


class UploadFileTab(QWidget):
    """Tab widget for uploading and transcribing audio files."""

    upload_requested = pyqtSignal(str)
    model_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._audio_path: str | None = None
        self._preview: AudioFilePreview | None = None
        self.current_model = config.MODEL_CHOICES[0]

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        content_container = QWidget()
        content_container.setObjectName("uploadFileContent")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 16, 24, 24)
        content_layout.setSpacing(16)
        content_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()

        content_container.setMaximumWidth(700)
        content_container.setMinimumWidth(500)

        main_layout.addLayout(center_wrapper)

        # Model selection card
        model_card = Card()

        model_label = QLabel("Transcription Model")
        model_label.setObjectName("headerLabel")
        model_label.setFont(QFont("Segoe UI", 13))
        model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.model_combo = QComboBox()
        self.model_combo.addItems(config.MODEL_CHOICES)
        self.model_combo.setMinimumHeight(40)
        self.model_combo.setFont(QFont("Segoe UI", 12))

        model_card.layout.addWidget(model_label)
        model_card.layout.addWidget(self.model_combo)
        content_layout.addWidget(model_card)

        # Drop zone
        self.drop_zone = DropZoneWidget()
        content_layout.addWidget(self.drop_zone)

        # File info card (hidden until a file is selected)
        self.file_info_card = FileInfoCard()
        self.file_info_card.hide()
        content_layout.addWidget(self.file_info_card)

        # Status label
        self.status_label = QLabel("Select an audio file to transcribe")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        content_layout.addWidget(self.status_label)

        # Transcription display card
        transcription_card = HeaderCard("Transcription")

        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setMinimumHeight(250)
        self.transcription_text.setFont(QFont("Segoe UI", 13))
        self.transcription_text.setPlaceholderText(
            "Transcription will appear here...\n"
            "Upload an audio file to begin."
        )

        transcription_card.layout.addWidget(self.transcription_text)
        content_layout.addWidget(transcription_card)

        # Stats widget (hidden by default)
        self.stats_widget = TranscriptionStatsWidget()
        content_layout.addWidget(self.stats_widget)

        content_layout.addStretch()

    def _connect_signals(self):
        self.drop_zone.file_selected.connect(self._on_file_selected)
        self.file_info_card.transcribe_clicked.connect(self._on_transcribe)
        self.file_info_card.remove_clicked.connect(self.clear_file)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)

    # ── Internal handlers ──────────────────────────────────────────

    def _on_file_selected(self, path: str):
        """Analyze the dropped/browsed file and show its info card."""
        try:
            preview = audio_processor.preview_file(path)
        except FileNotFoundError:
            logger.error(f"File not found: {path}")
            self.set_status("File not found")
            return
        except ValueError as e:
            logger.error(f"Invalid audio file: {e}")
            self.set_status(f"Invalid audio file: {e}")
            return
        except Exception as e:
            logger.error(f"Error analyzing file: {e}")
            self.set_status(f"Error: {e}")
            return

        self._audio_path = path
        self._preview = preview

        self.drop_zone.hide()
        self.file_info_card.set_preview(preview)
        self.file_info_card.show()
        self.set_status("Ready to transcribe")
        logger.info(f"File loaded: {preview.file_name}")

    def _on_transcribe(self):
        if not self._audio_path or not os.path.exists(self._audio_path):
            self.set_status("File no longer exists — please select again")
            self.clear_file()
            return
        self.file_info_card.set_transcribing(True)
        self.model_combo.setEnabled(False)
        self.set_status("Transcribing...")
        self.upload_requested.emit(self._audio_path)

    def _on_model_changed(self, model_name: str):
        self.current_model = model_name
        self.model_changed.emit(model_name)

    # ── Public API ─────────────────────────────────────────────────

    def set_status(self, text: str):
        """Update the status label."""
        self.status_label.setText(text)

    def set_transcript(self, text: str):
        """Set the transcript text and reset transcribing state."""
        self.transcription_text.setText(text)
        self.file_info_card.set_transcribing(False)
        self.model_combo.setEnabled(True)

    def clear_transcription(self):
        """Clear the transcript text."""
        self.transcription_text.clear()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
    ):
        """Forward stats to the stats widget."""
        self.stats_widget.set_stats(transcription_time, audio_duration, file_size)

    def clear_transcription_stats(self):
        """Clear and hide the stats display."""
        self.stats_widget.clear()

    def clear_file(self):
        """Reset to the empty drop-zone state."""
        self._audio_path = None
        self._preview = None
        self.file_info_card.hide()
        self.file_info_card.set_transcribing(False)
        self.drop_zone.show()
        self.model_combo.setEnabled(True)
        self.set_status("Select an audio file to transcribe")

    def set_file(self, audio_path: str):
        """Programmatically set a file (e.g., from File menu redirect)."""
        self._on_file_selected(audio_path)

    def open_file_browser(self):
        """Open the file browser dialog."""
        self.drop_zone.open_file_browser()

    def get_model_value(self) -> str:
        """Get the model value key."""
        return config.MODEL_VALUE_MAP.get(self.current_model, "local_whisper")

    def set_model_selection(self, model_value: str):
        """Set the model selection by internal value."""
        for display_name, internal_value in config.MODEL_VALUE_MAP.items():
            if internal_value == model_value:
                index = self.model_combo.findText(display_name)
                if index >= 0:
                    self.model_combo.blockSignals(True)
                    self.model_combo.setCurrentIndex(index)
                    self.current_model = display_name
                    self.model_combo.blockSignals(False)
                break
