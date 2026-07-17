"""
Upload File Tab widget.
Provides drag-and-drop and browse-based audio file upload with inline
file preview on top of the shared transcription tab scaffolding (model
selection, status, transcript).
"""
import logging
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QDragEnterEvent, QDropEvent, QMouseEvent

from services.audio_processor import AudioFilePreview, audio_processor
from ui_qt.widgets.cards import Card
from ui_qt.widgets.buttons import PrimaryButton, Button
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase

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
            background-color: #2c2c2e;
            border: 2px dashed #48484a;
            border-radius: 16px;
        }
        QFrame#dropZone:hover {
            border-color: #0a84ff;
            background-color: #3a3a3c;
        }
    """
    _HOVER_STYLE = """
        QFrame#dropZone {
            background-color: rgba(10, 132, 255, 0.15);
            border: 2px solid #0a84ff;
            border-radius: 16px;
        }
    """
    _REJECT_STYLE = """
        QFrame#dropZone {
            background-color: rgba(255, 69, 58, 0.12);
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
        title.setStyleSheet(f"color: #f5f5f7; {self._LABEL_RESET}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("or click to browse")
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setStyleSheet(f"color: #8e8e93; {self._LABEL_RESET}")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        formats = QLabel("WAV  ·  MP3  ·  M4A  ·  OGG  ·  FLAC  ·  WMA")
        formats.setFont(QFont("Segoe UI", 10))
        formats.setStyleSheet(f"color: #636366; {self._LABEL_RESET}")
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
        self.filename_label.setStyleSheet("color: #0a84ff;")
        self.filename_label.setWordWrap(True)
        self.layout.addWidget(self.filename_label)

        self.details_label = QLabel()
        self.details_label.setFont(QFont("Segoe UI", 11))
        self.details_label.setStyleSheet("color: #f5f5f7;")
        self.layout.addWidget(self.details_label)

        self.audio_info_label = QLabel()
        self.audio_info_label.setFont(QFont("Segoe UI", 10))
        self.audio_info_label.setStyleSheet("color: #8e8e93;")
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
                "color: #ff9f0a; font-size: 11px; font-weight: bold;"
            )
            self.chunk_label.show()
        else:
            self.chunk_label.setText("Will be transcribed in one pass")
            self.chunk_label.setStyleSheet(
                "color: #30d158; font-size: 11px; font-weight: bold;"
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


class UploadFileTab(TranscriptionTabBase):
    """Tab widget for uploading and transcribing audio files."""

    upload_requested = pyqtSignal(str)

    CONTENT_OBJECT_NAME = "uploadFileContent"
    INITIAL_STATUS = "Select an audio file to transcribe"
    TRANSCRIPT_PLACEHOLDER = (
        "Transcription will appear here...\n"
        "Upload an audio file to begin."
    )

    def __init__(self, parent=None):
        super().__init__(parent)

        # State (safe to set after the base constructor: _setup_ui never
        # reads it, and no signals can fire during init)
        self._audio_path: str | None = None
        self._preview: AudioFilePreview | None = None

    def _build_content_before_status(self, layout: QVBoxLayout):
        """Build the drop zone and file info card above the status label."""
        self.drop_zone = DropZoneWidget()
        layout.addWidget(self.drop_zone)

        # File info card (hidden until a file is selected)
        self.file_info_card = FileInfoCard()
        self.file_info_card.hide()
        layout.addWidget(self.file_info_card)

    def _connect_signals(self):
        """Connect file-selection signals in addition to the shared ones."""
        super()._connect_signals()
        self.drop_zone.file_selected.connect(self._on_file_selected)
        self.file_info_card.transcribe_clicked.connect(self._on_transcribe)
        self.file_info_card.remove_clicked.connect(self.clear_file)

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
        self.local_engine.set_busy(True)
        self.set_status("Transcribing...")
        self.upload_requested.emit(self._audio_path)

    # ── Public API ─────────────────────────────────────────────────

    def set_transcript(self, text: str, raw=None):
        """Set the transcript text and reset transcribing state."""
        super().set_transcript(text, raw=raw)
        self.file_info_card.set_transcribing(False)
        self.model_combo.setEnabled(True)
        self.local_engine.set_busy(False)

    def clear_file(self):
        """Reset to the empty drop-zone state."""
        self._audio_path = None
        self._preview = None
        self.file_info_card.hide()
        self.file_info_card.set_transcribing(False)
        self.drop_zone.show()
        self.model_combo.setEnabled(True)
        self.local_engine.set_busy(False)
        self.set_status("Select an audio file to transcribe")

    def set_file(self, audio_path: str):
        """Programmatically set a file (e.g., from File menu redirect)."""
        self._on_file_selected(audio_path)

    def open_file_browser(self):
        """Open the file browser dialog."""
        self.drop_zone.open_file_browser()
