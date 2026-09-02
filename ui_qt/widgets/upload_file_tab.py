"""Audio file upload and transcription tab."""
import logging
import os
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFont, QIcon, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root
from services.audio_processor import AudioFilePreview, audio_processor
from services.format_utils import format_audio_duration, format_sample_rate
from services.runtime.transcription import EMPTY_ASR_MESSAGE
from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.buttons import Button, PrimaryButton
from ui_qt.widgets.decode_label import DecodeLabel
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.transcription_progress import TranscriptionProgressPanel
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = ('.wav', '.mp3', '.m4a', '.ogg', '.flac', '.wma')
AUDIO_FILTERS = (
    "Audio Files (*.wav *.mp3 *.m4a *.ogg *.flac *.wma);;"
    "WAV Files (*.wav);;MP3 Files (*.mp3);;All Files (*.*)"
)

#: How long the finished progress panel stays up before the action row returns.
RESULT_HOLD_MS = 1400


def _tabler_pixmap(name: str, size: int) -> QPixmap:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / name
    return QIcon(str(path)).pixmap(QSize(size, size))


def _repolish(widget: QWidget, prop: str, value: str) -> None:
    widget.setProperty(prop, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class DropZoneWidget(QFrame):
    """Drag-and-drop zone that also opens a file browser on click.

    Styles itself because the border and fill change with the drag state, and
    each variant has to carry the child rules too: a stylesheet set on a widget
    replaces, rather than layers over, the one it had before.
    """

    file_selected = pyqtSignal(str)

    # The icon rule repeats the ancestor so it outranks the blanket label rule;
    # on its own, ``QLabel#dropZoneIcon`` is the less specific selector.
    _CHILD_STYLE = """
        QFrame#dropZone QLabel {
            background-color: transparent;
            border: none;
        }
        QFrame#dropZone QLabel#dropZoneIcon {
            background-color: rgba(10, 132, 255, 0.14);
            border-radius: 16px;
        }
    """
    _IDLE_STYLE = """
        QFrame#dropZone {
            background-color: rgba(255, 255, 255, 0.025);
            border: 2px dashed #48484a;
            border-radius: 16px;
        }
        QFrame#dropZone:hover {
            border-color: #0a84ff;
            background-color: rgba(10, 132, 255, 0.06);
        }
    """ + _CHILD_STYLE
    _HOVER_STYLE = """
        QFrame#dropZone {
            background-color: rgba(10, 132, 255, 0.12);
            border: 2px solid #0a84ff;
            border-radius: 16px;
        }
        QFrame#dropZone QLabel#dropZoneIcon {
            background-color: rgba(10, 132, 255, 0.28);
        }
    """ + _CHILD_STYLE
    _REJECT_STYLE = """
        QFrame#dropZone {
            background-color: rgba(255, 69, 58, 0.10);
            border: 2px dashed #ff453a;
            border-radius: 16px;
        }
    """ + _CHILD_STYLE

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(150)
        self.setStyleSheet(self._IDLE_STYLE)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(4)

        icon_label = QLabel()
        icon_label.setObjectName("dropZoneIcon")
        icon_label.setFixedSize(52, 52)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(_tabler_pixmap("cloud-upload-blue.svg", 26))
        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(8)

        title = QLabel("Drop an audio file here")
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        title.setStyleSheet("color: #f5f5f7;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("or click to browse")
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setStyleSheet("color: #8e8e93;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        formats = QLabel("WAV  ·  MP3  ·  M4A  ·  OGG  ·  FLAC  ·  WMA")
        formats.setFont(QFont("Segoe UI", 10))
        formats.setStyleSheet("color: #636366;")
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
        audio_path, _ = QFileDialog.getOpenFileName(
            self, "Select Audio File", "", AUDIO_FILTERS
        )
        if audio_path:
            self.file_selected.emit(audio_path)


class FileInfoCard(QFrame):
    """The loaded file: its name, its facts as chips, and a footer that is the
    action row or, while a job runs, the inline progress panel.

    The footer is a QStackedWidget so the card keeps one height whichever page
    is showing; swapping visible widgets instead would jog everything below it
    twice per job.
    """

    transcribe_clicked = pyqtSignal()
    remove_clicked = pyqtSignal()
    copy_clicked = pyqtSignal()
    #: True while the footer shows the progress panel rather than the actions.
    progress_shown = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadFileCard")
        self._preview: AudioFilePreview | None = None
        self._transcribing = False

        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(RESULT_HOLD_MS)
        self._settle_timer.timeout.connect(self._show_actions)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(14)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("uploadFileIcon")
        self.icon_label.setFixedSize(44, 44)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(_tabler_pixmap("file-music-blue.svg", 24))
        header.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 2, 0, 0)
        text_column.setSpacing(7)

        self.filename_label = ElidingLabel()
        self.filename_label.setObjectName("uploadFileName")
        text_column.addWidget(self.filename_label)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        self.size_chip = self._chip()
        self.duration_chip = self._chip()
        self.rate_chip = self._chip()
        self.channels_chip = self._chip()
        self.chunk_label = self._chip("uploadChunkChip")
        for chip in (
            self.size_chip,
            self.duration_chip,
            self.rate_chip,
            self.channels_chip,
            self.chunk_label,
        ):
            chips.addWidget(chip)
        chips.addStretch()
        text_column.addLayout(chips)
        header.addLayout(text_column, stretch=1)

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("uploadRemoveButton")
        self.remove_btn.setFlat(True)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.clicked.connect(self.remove_clicked.emit)
        header.addWidget(self.remove_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        divider = QFrame()
        divider.setObjectName("uploadCardDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        self.footer = QStackedWidget()
        self.footer.setObjectName("uploadFooter")

        self.actions_page = QWidget()
        self.actions_page.setObjectName("uploadActions")
        actions = QHBoxLayout(self.actions_page)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)

        self.copy_btn = Button("Copy")
        self.copy_btn.setObjectName("uploadCopyButton")
        self.copy_btn.set_base_minimum_size(96, 42)
        self.copy_btn.set_active(False)
        self.copy_btn.clicked.connect(self.copy_clicked.emit)
        actions.addWidget(self.copy_btn)

        actions.addStretch()
        # Duration and size already sit in the chips, so the only new fact a
        # finished job brings is how long it took; it lives between the actions
        # rather than in the stats strip the Quick Record tab uses.
        self.result_label = DecodeLabel()
        self.result_label.setObjectName("uploadResultNote")
        self.result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_label.hide()
        actions.addWidget(self.result_label)
        actions.addStretch()

        self.transcribe_btn = PrimaryButton("Transcribe")
        self.transcribe_btn.set_base_minimum_size(150, 44)
        self.transcribe_btn.clicked.connect(self.transcribe_clicked.emit)
        actions.addWidget(self.transcribe_btn)
        self.footer.addWidget(self.actions_page)

        self.progress = TranscriptionProgressPanel()
        self.footer.addWidget(self.progress)

        layout.addWidget(self.footer)

    @staticmethod
    def _chip(object_name: str = "uploadMetaChip") -> QLabel:
        chip = QLabel()
        chip.setObjectName(object_name)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return chip

    @property
    def is_transcribing(self) -> bool:
        return self._transcribing

    @property
    def is_progress_shown(self) -> bool:
        return self.footer.currentWidget() is self.progress

    def set_preview(self, preview: AudioFilePreview):
        self._preview = preview
        self.filename_label.setText(preview.file_name)
        self.size_chip.setText(preview.file_size_formatted)
        self.duration_chip.setText(preview.duration_formatted)
        self.rate_chip.setText(format_sample_rate(preview.sample_rate))
        if preview.channels == 1:
            channels = "Mono"
        elif preview.channels == 2:
            channels = "Stereo"
        else:
            channels = f"{preview.channels} ch"
        self.channels_chip.setText(channels)

        if preview.needs_splitting:
            self.chunk_label.setText(f"{preview.estimated_chunks} chunks")
            _repolish(self.chunk_label, "tone", "warn")
        else:
            self.chunk_label.setText("One pass")
            _repolish(self.chunk_label, "tone", "ok")

    def set_transcribing(self, active: bool, with_cleanup: bool = False):
        """Lock the card for a job, or return it to idle at once.

        Args:
            active: True when a job is starting, False to reset immediately
                (used when the file is removed).
            with_cleanup: Whether the AI cleanup pass is on, so the stepper
                knows whether to show a third step.
        """
        self._settle_timer.stop()
        self._transcribing = active
        self.transcribe_btn.setEnabled(not active)
        self.remove_btn.setEnabled(not active)
        if active:
            self.copy_btn.set_active(False)
            self.clear_result()
            self.progress.start(with_cleanup)
            self._show_progress()
        else:
            self._show_actions()

    def finish_transcribing(self, success: bool):
        """End the job. The result stays up briefly before the actions return."""
        was_running = self._transcribing
        self._transcribing = False
        self.transcribe_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)
        if was_running:
            self.progress.finish(success)
            self._settle_timer.start()
        elif self.is_progress_shown:
            self._show_actions()

    def set_copy_enabled(self, enabled: bool):
        """Enable Copy only when a non-empty transcript is available."""
        self.copy_btn.set_active(enabled)

    def set_result(self, transcription_time: float, audio_duration: float):
        """Show how long the job took, and how that compares to the audio."""
        segments = [
            ("Transcribed in ", False),
            (format_audio_duration(transcription_time), True),
        ]
        if transcription_time > 0 and audio_duration > 0:
            speed = audio_duration / transcription_time
            segments += [
                ("  ·  ", False),
                (f"{speed:.1f}×", True),
                (" realtime", False),
            ]
        # The reveal runs when the action row next comes into view, which is
        # after the progress panel has held its Done state.
        self.result_label.set_segments(segments)
        self.result_label.show()

    def clear_result(self):
        self.result_label.clear()
        self.result_label.hide()

    def _show_progress(self):
        if not self.is_progress_shown:
            self.footer.setCurrentWidget(self.progress)
            self.progress_shown.emit(True)

    def _show_actions(self):
        if self.is_progress_shown:
            self.footer.setCurrentWidget(self.actions_page)
            self.progress_shown.emit(False)


class UploadFileTab(TranscriptionTabBase):
    """Tab widget for uploading and transcribing audio files."""

    upload_requested = pyqtSignal(str, float)
    copy_requested = pyqtSignal(str)

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
        self.drop_zone = DropZoneWidget()
        layout.addWidget(self.drop_zone)

        self.file_info_card = FileInfoCard()
        self.file_info_card.hide()
        layout.addWidget(self.file_info_card)

    def _connect_signals(self):
        super()._connect_signals()
        self.drop_zone.file_selected.connect(self._on_file_selected)
        self.file_info_card.transcribe_clicked.connect(self._on_transcribe)
        self.file_info_card.remove_clicked.connect(self.clear_file)
        self.file_info_card.copy_clicked.connect(self._on_copy)
        self.file_info_card.progress_shown.connect(self._on_progress_shown)

    @property
    def is_transcribing(self) -> bool:
        """Whether a job started from this tab is still running."""
        return self.file_info_card.is_transcribing

    def _on_progress_shown(self, shown: bool):
        # The panel carries the status line while it is up; keeping the
        # standalone label as well would print the same text twice.
        self.status_label.setVisible(not shown)

    def set_status(self, status_text: str):
        super().set_status(status_text)
        if self.file_info_card.is_progress_shown:
            self.file_info_card.progress.set_detail(status_text)

    def set_progress_state(self, state: OverlayState) -> None:
        """Take a stage that hotkey dictation would have shown on the overlay.

        ``NONE`` while the job is still marked running means it ended without a
        transcript reaching this tab, so the card is released as a failure.
        """
        if not self.is_transcribing:
            return
        if state is OverlayState.NONE:
            self.file_info_card.finish_transcribing(success=False)
            self._unlock_engine()
            return
        self.file_info_card.progress.apply_overlay_state(state)

    def set_large_file_stage(self, file_size_mb: float, is_splitting: bool) -> None:
        if self.is_transcribing:
            self.file_info_card.progress.set_large_file(file_size_mb, is_splitting)

    def _on_file_selected(self, path: str):
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
        self.file_info_card.set_transcribing(
            True, with_cleanup=self.cleanup_check.isChecked()
        )
        self.set_backend_enabled(False)
        self.local_engine.set_busy(True)
        duration = self._preview.duration_seconds if self._preview else 0.0
        self.upload_requested.emit(self._audio_path, duration)

    def set_transcript(self, text: str, raw=None):
        super().set_transcript(text, raw=raw)
        stripped = (text or "").strip()
        failed = stripped.startswith("Error:")
        copyable = bool(stripped) and stripped != EMPTY_ASR_MESSAGE and not failed
        self.file_info_card.finish_transcribing(success=not failed)
        self._unlock_engine()
        self.file_info_card.set_copy_enabled(copyable)

    def _unlock_engine(self):
        self.set_backend_enabled(True)
        self.local_engine.set_busy(False)

    def set_transcription_stats(
        self, transcription_time: float, audio_duration: float, file_size: int
    ):
        """Report the result in the card; this tab never shows the stats strip."""
        self.file_info_card.set_result(transcription_time, audio_duration)

    def clear_transcription_stats(self):
        self.file_info_card.clear_result()

    def _on_copy(self):
        """Emit the current display transcript for the shared clipboard path."""
        text = (self._fixed_text or "").strip()
        if text:
            self.copy_requested.emit(text)

    def clear_file(self):
        self._audio_path = None
        self._preview = None
        self.file_info_card.hide()
        self.file_info_card.set_transcribing(False)
        self.file_info_card.set_copy_enabled(False)
        self.drop_zone.show()
        self._unlock_engine()
        self.set_status(self.INITIAL_STATUS)

    def set_file(self, audio_path: str):
        self._on_file_selected(audio_path)

    def open_file_browser(self):
        self.drop_zone.open_file_browser()
