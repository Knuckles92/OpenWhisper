"""Audio file upload and transcription tab."""
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QMimeData, QSize, Qt, QTimer, pyqtSignal
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
from services.batch_upload import BatchItem, BatchRelation, BatchUploadRequest
from services.format_utils import format_audio_duration, format_sample_rate
from services.runtime.transcription import EMPTY_ASR_MESSAGE
from services.settings import (
    SettingsKey,
    resolve_transcript_batch_custom_combine,
    resolve_transcript_batch_custom_instructions,
    resolve_transcript_batch_relation,
    settings_manager,
)
from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.buttons import Button, PrimaryButton
from ui_qt.widgets.decode_label import DecodeLabel
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.transcription_progress import (
    ProgressStage,
    TranscriptionProgressPanel,
)
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = ('.wav', '.mp3', '.m4a', '.ogg', '.flac', '.wma')
AUDIO_FILTERS = (
    "Audio Files (*.wav *.mp3 *.m4a *.ogg *.flac *.wma);;"
    "WAV Files (*.wav);;MP3 Files (*.mp3);;All Files (*.*)"
)

#: How long the finished progress panel stays up before the action row returns.
RESULT_HOLD_MS = 1400

#: Combo text, then the hint shown under it, per relation preset.
RELATION_OPTIONS = (
    (
        BatchRelation.SEPARATE,
        "Separate recordings",
        "Each file is cleaned on its own and saved as its own transcript.",
    ),
    (
        BatchRelation.SEQUENTIAL,
        "Parts of one recording, in order",
        "Stitched into one transcript; overlap at the seams is removed.",
    ),
    (
        BatchRelation.CUSTOM,
        "Custom…",
        "Describe the files and how the cleanup should treat them.",
    ),
)

_ROW_STATE_TEXT = {
    "reading": "Reading…",
    "pending": "Queued",
    "active": "Working",
    "done": "Done",
    "failed": "Failed",
}


def _tabler_pixmap(name: str, size: int) -> QPixmap:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / name
    return QIcon(str(path)).pixmap(QSize(size, size))


def _tabler_icon(name: str) -> QIcon:
    return QIcon(str(Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / name))


def _repolish(widget: QWidget, prop: str, value: str) -> None:
    widget.setProperty(prop, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _is_supported_audio(path: str) -> bool:
    return path.lower().endswith(SUPPORTED_EXTENSIONS)


def _audio_paths_from_mime(mime: QMimeData) -> tuple[list[str], int]:
    """Local audio paths in drop order, and how many dropped URLs were not."""
    paths: list[str] = []
    skipped = 0
    if not mime.hasUrls():
        return paths, skipped
    for url in mime.urls():
        path = url.toLocalFile()
        if path and _is_supported_audio(path):
            paths.append(path)
        else:
            skipped += 1
    return paths, skipped


def _run_in_thread(target: Callable[[], None], name: str) -> None:
    """Start a daemon worker. Tests replace this to run the target inline."""
    threading.Thread(target=target, name=name, daemon=True).start()


class DropZoneWidget(QFrame):
    """Drag-and-drop zone that also opens a file browser on click.

    Styles itself because the border and fill change with the drag state, and
    each variant has to carry the child rules too: a stylesheet set on a widget
    replaces, rather than layers over, the one it had before.
    """

    #: (audio paths in drop order, count of dropped items that were not audio)
    files_selected = pyqtSignal(list, int)

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

        title = QLabel("Drop audio files here")
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        title.setStyleSheet("color: #f5f5f7;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("or click to browse  ·  several files become a queue")
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

        # Why the last attempt landed back here (unreadable file, nothing
        # droppable); the tab has no status line of its own.
        self.notice = QLabel()
        self.notice.setObjectName("dropZoneNotice")
        self.notice.setFont(QFont("Segoe UI", 11))
        self.notice.setStyleSheet("color: #ff453a;")
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _is_valid_audio(self, path: str) -> bool:
        return _is_supported_audio(path)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            paths, _ = _audio_paths_from_mime(event.mimeData())
            if paths:
                event.acceptProposedAction()
                self.setStyleSheet(self._HOVER_STYLE)
                return
            self.setStyleSheet(self._REJECT_STYLE)
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._IDLE_STYLE)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet(self._IDLE_STYLE)
        paths, skipped = _audio_paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_selected.emit(paths, skipped)
            return
        event.ignore()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_file_browser()

    def open_file_browser(self):
        audio_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Audio Files", "", AUDIO_FILTERS
        )
        if audio_paths:
            self.files_selected.emit(list(audio_paths), 0)


@dataclass
class QueueItem:
    """One file waiting on the Upload File tab."""

    path: str
    preview: Optional[AudioFilePreview] = None
    error: Optional[str] = None
    #: reading | pending | active | done | failed
    state: str = "reading"
    #: This file's own finished transcript, when the job produced one per file.
    transcript: str = ""

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.abspath(self.path))

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


class UploadQueueRow(QFrame):
    """One file of a multi-file upload: name, facts, state, and its controls."""

    remove_clicked = pyqtSignal(str)
    move_up_clicked = pyqtSignal(str)
    move_down_clicked = pyqtSignal(str)
    copy_clicked = pyqtSignal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadQueueRow")
        self.path = path
        self._state = ""
        self._transcript = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(10)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("uploadQueueRowIcon")
        self.icon_label.setFixedSize(30, 30)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(_tabler_pixmap("file-music-blue.svg", 16))
        layout.addWidget(self.icon_label)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(4)
        self.name_label = ElidingLabel(os.path.basename(path))
        self.name_label.setObjectName("uploadQueueRowName")
        text_column.addWidget(self.name_label)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        self.size_chip = self._chip()
        self.duration_chip = self._chip()
        self.chunk_chip = self._chip("uploadChunkChip")
        for chip in (self.size_chip, self.duration_chip, self.chunk_chip):
            chips.addWidget(chip)
        chips.addStretch()
        text_column.addLayout(chips)
        layout.addLayout(text_column, stretch=1)

        # Copies this file's transcript alone; the card's Copy all takes the
        # whole result. Hidden until the file has finished text of its own.
        self.copy_btn = self._icon_button(
            "copy-gray.svg", "uploadRowCopyButton", "Copy this file's transcript"
        )
        self.copy_btn.clicked.connect(lambda: self.copy_clicked.emit(self.path))
        self.copy_btn.hide()
        layout.addWidget(self.copy_btn)

        self.state_chip = QLabel()
        self.state_chip.setObjectName("uploadRowState")
        self.state_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.state_chip)

        self.up_btn = self._icon_button(
            "chevron-up-gray.svg", "uploadRowMoveButton", "Move up"
        )
        self.up_btn.clicked.connect(lambda: self.move_up_clicked.emit(self.path))
        layout.addWidget(self.up_btn)

        self.down_btn = self._icon_button(
            "chevron-down-gray.svg", "uploadRowMoveButton", "Move down"
        )
        self.down_btn.clicked.connect(lambda: self.move_down_clicked.emit(self.path))
        layout.addWidget(self.down_btn)

        self.remove_btn = self._icon_button(
            "x-gray.svg", "uploadRowRemoveButton", "Remove from queue"
        )
        self.remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self.path))
        layout.addWidget(self.remove_btn)

        self.set_loading()

    @staticmethod
    def _chip(object_name: str = "uploadMetaChip") -> QLabel:
        chip = QLabel()
        chip.setObjectName(object_name)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return chip

    @staticmethod
    def _icon_button(icon: str, object_name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setIcon(_tabler_icon(icon))
        button.setIconSize(QSize(14, 14))
        button.setFixedSize(26, 26)
        button.setFlat(True)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @property
    def state(self) -> str:
        return self._state

    @property
    def transcript(self) -> str:
        return self._transcript

    def set_transcript(self, text: str) -> None:
        self._transcript = text or ""
        self.copy_btn.setVisible(bool(self._transcript))

    def set_loading(self) -> None:
        self.size_chip.setText("Reading…")
        self.duration_chip.setText("…")
        self.chunk_chip.hide()
        self.set_state("reading")

    def set_preview(self, preview: AudioFilePreview) -> None:
        self.size_chip.setText(preview.file_size_formatted)
        self.duration_chip.setText(preview.duration_formatted)
        self.chunk_chip.show()
        if preview.needs_splitting:
            self.chunk_chip.setText(f"{preview.estimated_chunks} chunks")
            _repolish(self.chunk_chip, "tone", "warn")
        else:
            self.chunk_chip.setText("One pass")
            _repolish(self.chunk_chip, "tone", "ok")
        self.setToolTip(self.path)

    def set_error(self, message: str) -> None:
        self.size_chip.setText("Could not read")
        self.duration_chip.setText("…")
        self.chunk_chip.hide()
        self.setToolTip(message)
        self.set_state("failed")

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_chip.setText(_ROW_STATE_TEXT.get(state, state))
        _repolish(self.state_chip, "state", state)
        _repolish(self, "state", state)

    def set_position(self, index: int, count: int) -> None:
        self.up_btn.setEnabled(index > 0)
        self.down_btn.setEnabled(index < count - 1)

    def set_locked(self, locked: bool) -> None:
        for button in (self.up_btn, self.down_btn, self.remove_btn):
            button.setVisible(not locked)


class RelationPicker(QWidget):
    """The "how these files relate" preset and its one-line explanation."""

    relation_changed = pyqtSignal(str)
    edit_custom_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadRelationRow")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.label = QLabel("How these files relate")
        self.label.setObjectName("uploadRelationLabel")
        row.addWidget(self.label)

        self.combo = NoWheelComboBox()
        self.combo.setObjectName("uploadRelationCombo")
        for key, text, _hint in RELATION_OPTIONS:
            self.combo.addItem(text, key)
        self.combo.currentIndexChanged.connect(self._on_index_changed)
        row.addWidget(self.combo, stretch=1)

        self.edit_btn = QPushButton("Edit description")
        self.edit_btn.setObjectName("uploadRelationEditButton")
        self.edit_btn.setFlat(True)
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.clicked.connect(self.edit_custom_requested.emit)
        self.edit_btn.hide()
        row.addWidget(self.edit_btn)
        layout.addLayout(row)

        self.hint = WrappedLabel()
        self.hint.setObjectName("uploadRelationHint")
        layout.addWidget(self.hint)

        self._apply_hint(self.relation())

    def relation(self) -> str:
        return self.combo.currentData() or BatchRelation.SEPARATE

    def set_relation(self, value: str) -> None:
        """Show a preset as selected without announcing it."""
        index = self.combo.findData(value)
        if index < 0:
            return
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self._apply_hint(value)

    def set_custom_summary(self, instructions: str, combine: bool) -> None:
        shape = "One combined transcript" if combine else "One transcript per file"
        text = " ".join(instructions.split())
        if len(text) > 90:
            text = text[:87].rstrip() + "…"
        self.hint.setText(f"{shape}  ·  {text}" if text else shape)
        self.edit_btn.show()

    def set_locked(self, locked: bool) -> None:
        self.combo.setEnabled(not locked)
        self.edit_btn.setEnabled(not locked)

    def _apply_hint(self, value: str) -> None:
        for key, _text, hint in RELATION_OPTIONS:
            if key == value:
                self.hint.setText(hint)
                break
        self.edit_btn.setVisible(value == BatchRelation.CUSTOM)

    def _on_index_changed(self, _index: int) -> None:
        value = self.relation()
        self._apply_hint(value)
        self.relation_changed.emit(value)


class UploadQueueWidget(QWidget):
    """The rows of a multi-file upload plus the controls that act on the set.

    Rows are keyed by path and rebuilt from the tab's model, so reordering and
    previews that arrive out of order need no index bookkeeping here.
    """

    add_files_clicked = pyqtSignal()
    clear_all_clicked = pyqtSignal()
    remove_clicked = pyqtSignal(str)
    move_up_clicked = pyqtSignal(str)
    move_down_clicked = pyqtSignal(str)
    copy_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadQueue")
        self._rows: dict[str, UploadQueueRow] = {}
        self._order: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.title_label = QLabel()
        self.title_label.setObjectName("uploadQueueTitle")
        header.addWidget(self.title_label)

        # What the set still needs or what the last drop left out, beside the
        # count; there is no status line under the card to say it.
        self.note_label = ElidingLabel()
        self.note_label.setObjectName("uploadQueueNote")
        self.note_label.hide()
        header.addWidget(self.note_label, stretch=1)

        self.add_btn = QPushButton("Add files…")
        self.add_btn.setObjectName("uploadAddFilesButton")
        self.add_btn.setIcon(_tabler_icon("plus-blue.svg"))
        self.add_btn.setIconSize(QSize(14, 14))
        self.add_btn.setFlat(True)
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(self.add_files_clicked.emit)
        header.addWidget(self.add_btn)

        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.setObjectName("uploadRemoveButton")
        self.clear_btn.setFlat(True)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_all_clicked.emit)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        layout.addLayout(self._rows_layout)

        self.picker = RelationPicker()
        layout.addWidget(self.picker)

    def render(self, items: list[QueueItem]) -> None:
        keys = [item.key for item in items]
        if keys != self._order:
            self._rebuild(items)
        for index, item in enumerate(items):
            row = self._rows[item.key]
            if item.error:
                row.set_error(item.error)
            elif item.preview is None:
                row.set_loading()
            else:
                row.set_preview(item.preview)
                row.set_state(item.state)
            row.set_transcript(item.transcript)
            row.set_position(index, len(items))
        count = len(items)
        self.title_label.setText(f"{count} file{'s' if count != 1 else ''}")

    def set_note(self, text: str, warn: bool = False) -> None:
        self.note_label.setText(f"·  {text}" if text else "")
        self.note_label.setToolTip(text)
        self.note_label.setVisible(bool(text))
        _repolish(self.note_label, "tone", "warn" if warn else "")

    def _rebuild(self, items: list[QueueItem]) -> None:
        for row in self._rows.values():
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows = {}
        self._order = []
        for item in items:
            row = UploadQueueRow(item.path)
            row.remove_clicked.connect(self.remove_clicked)
            row.move_up_clicked.connect(self.move_up_clicked)
            row.move_down_clicked.connect(self.move_down_clicked)
            row.copy_clicked.connect(self.copy_clicked)
            self._rows_layout.addWidget(row)
            self._rows[item.key] = row
            self._order.append(item.key)

    def row_for(self, path: str) -> Optional[UploadQueueRow]:
        return self._rows.get(QueueItem(path).key)

    def rows(self) -> list[UploadQueueRow]:
        return [self._rows[key] for key in self._order]

    def set_locked(self, locked: bool) -> None:
        self.add_btn.setEnabled(not locked)
        self.clear_btn.setEnabled(not locked)
        self.picker.set_locked(locked)
        for row in self._rows.values():
            row.set_locked(locked)


class FileInfoCard(QFrame):
    """The loaded file, or the queue of them, and a footer that is the action
    row or, while a job runs, the inline progress panel.

    The footer is a QStackedWidget so the card keeps one height whichever page
    is showing; swapping visible widgets instead would jog everything below it
    twice per job. One file shows the classic header; two or more swap it for
    the queue, so the single-file path runs the same widgets it always did.
    """

    transcribe_clicked = pyqtSignal()
    remove_clicked = pyqtSignal()
    copy_clicked = pyqtSignal()
    cancel_clicked = pyqtSignal()
    #: (audio paths, skipped count) dropped onto the card itself.
    files_dropped = pyqtSignal(list, int)
    #: True while the footer shows the progress panel rather than the actions.
    progress_shown = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadFileCard")
        self.setAcceptDrops(True)
        self._preview: AudioFilePreview | None = None
        self._transcribing = False
        self._ready = True
        self._mode = "single"

        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(RESULT_HOLD_MS)
        self._settle_timer.timeout.connect(self._show_actions)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        self.header_row = QWidget()
        self.header_row.setObjectName("uploadFileHeader")
        header = QHBoxLayout(self.header_row)
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
        layout.addWidget(self.header_row)

        self.queue = UploadQueueWidget()
        self.queue.hide()
        layout.addWidget(self.queue)

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
        self.progress.stop_clicked.connect(self.cancel_clicked.emit)
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

    @property
    def mode(self) -> str:
        return self._mode

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._transcribing or not event.mimeData().hasUrls():
            event.ignore()
            return
        paths, _ = _audio_paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths, skipped = _audio_paths_from_mime(event.mimeData())
        if paths and not self._transcribing:
            event.acceptProposedAction()
            self.files_dropped.emit(paths, skipped)
            return
        event.ignore()

    def set_mode(self, mode: str) -> None:
        """Show the single-file header or the multi-file queue."""
        self._mode = mode
        self.header_row.setVisible(mode == "single")
        self.queue.setVisible(mode == "queue")
        self.copy_btn.setText("Copy all" if mode == "queue" else "Copy")

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

    def set_loading(self, file_name: str) -> None:
        """Name a file whose preview is still being read off the UI thread."""
        self._preview = None
        self.filename_label.setText(file_name)
        for chip in (self.size_chip, self.duration_chip, self.rate_chip, self.channels_chip):
            chip.setText("…")
        self.chunk_label.setText("Reading…")
        _repolish(self.chunk_label, "tone", "")

    def set_ready(self, ready: bool) -> None:
        """Whether every queued file has been read and can be transcribed."""
        self._ready = ready
        self._apply_transcribe_enabled()

    def _apply_transcribe_enabled(self) -> None:
        self.transcribe_btn.setEnabled(self._ready and not self._transcribing)

    def set_transcribing(
        self, active: bool, with_cleanup: bool = False, total_files: int = 1
    ):
        """Lock the card for a job, or return it to idle at once.

        Args:
            active: True when a job is starting, False to reset immediately
                (used when the file is removed).
            with_cleanup: Whether the AI cleanup pass is on, so the stepper
                knows whether to show a third step.
            total_files: Files in the job; more than one makes the progress
                bar determinate.
        """
        self._settle_timer.stop()
        self._transcribing = active
        self._apply_transcribe_enabled()
        self.remove_btn.setEnabled(not active)
        self.queue.set_locked(active)
        if active:
            self.copy_btn.set_active(False)
            self.clear_result()
            self.progress.start(with_cleanup, total_files)
            self._show_progress()
        else:
            self._show_actions()

    def finish_transcribing(self, success: bool):
        """End the job. The result stays up briefly before the actions return.

        A panel already at Canceled keeps saying so: the runtime follows a
        cancel with an error transcript, which must not relabel it Failed.
        """
        was_running = self._transcribing
        self._transcribing = False
        self._apply_transcribe_enabled()
        self.remove_btn.setEnabled(True)
        self.queue.set_locked(False)
        if was_running:
            if self.progress.stage is ProgressStage.CANCELED:
                self.progress.set_stop_enabled(False)
            else:
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
    """Tab widget for uploading and transcribing audio files.

    One file takes the same path it always has (``upload_requested``); two or
    more become a queue and go out as one ``BatchUploadRequest``.
    """

    upload_requested = pyqtSignal(str, float)
    upload_files_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal()
    copy_requested = pyqtSignal(str)
    #: (path, AudioFilePreview or error message) from the preview worker.
    _preview_ready = pyqtSignal(str, object)

    CONTENT_OBJECT_NAME = "uploadFileContent"
    TRANSCRIPT_PLACEHOLDER = (
        "Transcription will appear here...\n"
        "Upload an audio file to begin."
    )
    # A multi-file job heads each file's text with its name.
    TRANSCRIPT_MARKDOWN = True

    def __init__(self, parent=None):
        super().__init__(parent)
        # This tab has no status line: the drop zone, the card, and the
        # progress panel each say their own state, so a line under the card
        # only repeated one of them.
        self.status_label.hide()

        self._viewer = None
        self.expand_btn = QPushButton()
        self.expand_btn.setObjectName("transcriptExpandButton")
        self.expand_btn.setIcon(_tabler_icon("arrows-diagonal-gray.svg"))
        self.expand_btn.setIconSize(QSize(16, 16))
        self.expand_btn.setFixedSize(26, 26)
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.expand_btn.setToolTip("Open in a reading window")
        self.expand_btn.clicked.connect(self.open_transcript_viewer)
        self.expand_btn.hide()
        self.transcript_pane.set_corner_widget(self.expand_btn)

        # State (safe to set after the base constructor: _setup_ui never
        # reads it, and no signals can fire during init)
        self._items: list[QueueItem] = []
        # Mirrors of the single queued file, kept for the single-file callers.
        self._audio_path: str | None = None
        self._preview: AudioFilePreview | None = None
        self._cancel_pending = False
        # What the last drop left out (non-audio, duplicates) or what the last
        # Transcribe removed; shown in the queue header until the next drop,
        # clear, or job.
        self._queue_note = ""

        settings = settings_manager.load_all_settings()
        self._relation = resolve_transcript_batch_relation(settings)
        self._custom_instructions = resolve_transcript_batch_custom_instructions(settings)
        self._custom_combine = resolve_transcript_batch_custom_combine(settings)
        self._apply_relation_to_picker()

    def _build_content_before_status(self, layout: QVBoxLayout):
        self.drop_zone = DropZoneWidget()
        layout.addWidget(self.drop_zone)

        self.file_info_card = FileInfoCard()
        self.file_info_card.hide()
        layout.addWidget(self.file_info_card)

    def _connect_signals(self):
        super()._connect_signals()
        self.drop_zone.files_selected.connect(self._on_files_selected)
        card = self.file_info_card
        card.transcribe_clicked.connect(self._on_transcribe)
        card.remove_clicked.connect(self.clear_file)
        card.copy_clicked.connect(self._on_copy)
        card.cancel_clicked.connect(self._on_cancel)
        card.files_dropped.connect(self._on_files_selected)
        queue = card.queue
        queue.add_files_clicked.connect(self.open_file_browser)
        queue.clear_all_clicked.connect(self.clear_file)
        queue.remove_clicked.connect(self._remove)
        queue.move_up_clicked.connect(lambda path: self._move(path, -1))
        queue.move_down_clicked.connect(lambda path: self._move(path, 1))
        queue.copy_clicked.connect(self._on_row_copy)
        queue.picker.relation_changed.connect(self._on_relation_changed)
        queue.picker.edit_custom_requested.connect(self._on_edit_custom)
        self._preview_ready.connect(self._on_preview_ready)

    @property
    def is_transcribing(self) -> bool:
        """Whether a job started from this tab is still running."""
        return self.file_info_card.is_transcribing

    @property
    def queued_paths(self) -> list[str]:
        return [item.path for item in self._items]

    def set_status(self, status_text: str):
        """Show a runtime message in the progress panel while a job is up.

        Outside a job the message is dropped: the card and the drop zone
        already say what state the tab is in.
        """
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
        if state is OverlayState.CANCELING:
            self._cancel_pending = True
        self.file_info_card.progress.apply_overlay_state(state)

    def set_large_file_stage(self, file_size_mb: float, is_splitting: bool) -> None:
        if self.is_transcribing:
            self.file_info_card.progress.set_large_file(file_size_mb, is_splitting)

    def set_batch_progress(self, position: int, total: int, source_name: str) -> None:
        """A file of the running batch is starting (1-based position)."""
        if not self.is_transcribing:
            return
        index = position - 1
        if 0 <= index < len(self._items):
            self._items[index].state = "active"
            self.file_info_card.queue.render(self._items)
        self.file_info_card.progress.set_batch_position(position, total, source_name)

    def set_batch_item_finished(
        self, position: int, success: bool, transcript: str = ""
    ) -> None:
        """A file of the running batch has ended, with its own text if any."""
        if not self.is_transcribing:
            return
        index = position - 1
        if 0 <= index < len(self._items):
            item = self._items[index]
            item.state = "done" if success else "failed"
            item.transcript = (transcript or "").strip() if success else ""
            self.file_info_card.queue.render(self._items)

    # Queue management

    def _on_file_selected(self, path: str):
        self._on_files_selected([path], 0)

    def _on_files_selected(self, paths: list, skipped: int = 0):
        if self.is_transcribing:
            # Every way in is disabled while a job runs; a late drop is ignored.
            return
        existing = {item.key for item in self._items}
        added: list[str] = []
        duplicates = 0
        for path in paths:
            item = QueueItem(path)
            if item.key in existing:
                duplicates += 1
                continue
            existing.add(item.key)
            self._items.append(item)
            added.append(item.path)

        if not self._items:
            if skipped:
                self.drop_zone.set_notice("None of the dropped items are audio files")
            return

        self._queue_note = self._drop_note_for(skipped, duplicates)
        self.drop_zone.set_notice("")
        self.drop_zone.hide()
        self.file_info_card.show()
        self._render()
        if added:
            self._start_previews(added)

    @staticmethod
    def _drop_note_for(skipped: int, duplicates: int) -> str:
        parts = []
        if duplicates:
            parts.append(f"{duplicates} already queued")
        if skipped:
            parts.append(f"{skipped} skipped (not audio)")
        return "  ·  ".join(parts)

    def _start_previews(self, paths: list[str]) -> None:
        """Read file facts off the UI thread; results come back by path.

        ``preview_file`` decodes the whole file, so a run of long files would
        otherwise freeze the window once per file. One worker per drop keeps
        a ten-file drop from decoding ten files at once.
        """
        def worker() -> None:
            for path in paths:
                try:
                    result: object = audio_processor.preview_file(path)
                except FileNotFoundError:
                    result = "File not found"
                except ValueError as exc:
                    result = f"Invalid audio file: {exc}"
                except Exception as exc:
                    result = f"Error: {exc}"
                try:
                    self._preview_ready.emit(path, result)
                except RuntimeError:
                    # The tab was destroyed while the worker was still reading.
                    return

        _run_in_thread(worker, "upload-preview")

    def _on_preview_ready(self, path: str, result: object) -> None:
        item = self._item_for(path)
        if item is None:
            return
        if isinstance(result, AudioFilePreview):
            item.preview = result
            item.error = None
            item.state = "pending"
            logger.info(f"File loaded: {result.file_name}")
        else:
            message = str(result)
            logger.error(f"Could not read {path}: {message}")
            if len(self._items) == 1:
                # Alone, an unreadable file goes back to the drop zone, which
                # says why.
                self.clear_file()
                self.drop_zone.set_notice(message)
                return
            item.preview = None
            item.error = message
            item.state = "failed"
        self._render()

    def _item_for(self, path: str) -> Optional[QueueItem]:
        key = QueueItem(path).key
        for item in self._items:
            if item.key == key:
                return item
        return None

    def _render(self) -> None:
        card = self.file_info_card
        count = len(self._items)
        if count == 0:
            self.clear_file()
            return
        if count == 1:
            item = self._items[0]
            card.set_mode("single")
            self._audio_path = item.path
            self._preview = item.preview
            if item.preview is not None:
                card.set_preview(item.preview)
            else:
                card.set_loading(item.name)
        else:
            card.set_mode("queue")
            self._audio_path = None
            self._preview = None
            card.queue.render(self._items)
            self._apply_relation_to_picker()
        self._refresh_ready()

    def _refresh_ready(self) -> None:
        items = self._items
        failed = [item for item in items if item.error]
        ready = bool(items) and not failed and all(
            item.preview is not None for item in items
        )
        self.file_info_card.set_ready(ready)
        notes = []
        if failed:
            count = len(failed)
            notes.append(
                f"{count} could not be read — "
                f"remove {'them' if count != 1 else 'it'} to continue"
            )
        if self._queue_note:
            notes.append(self._queue_note)
        self.file_info_card.queue.set_note("  ·  ".join(notes), warn=bool(failed))

    def _move(self, path: str, delta: int) -> None:
        if self.is_transcribing:
            return
        item = self._item_for(path)
        if item is None:
            return
        index = self._items.index(item)
        target = index + delta
        if 0 <= target < len(self._items):
            self._items[index], self._items[target] = self._items[target], item
            self._render()

    def _remove(self, path: str) -> None:
        if self.is_transcribing:
            return
        item = self._item_for(path)
        if item is None:
            return
        self._items.remove(item)
        self._render()

    # Relation preset

    def _apply_relation_to_picker(self) -> None:
        picker = self.file_info_card.queue.picker
        picker.set_relation(self._relation)
        if self._relation == BatchRelation.CUSTOM:
            picker.set_custom_summary(self._custom_instructions, self._custom_combine)

    def _on_relation_changed(self, value: str) -> None:
        previous = self._relation
        if value == BatchRelation.CUSTOM and not self._edit_custom_description():
            self.file_info_card.queue.picker.set_relation(previous)
            return
        self._relation = value
        self._apply_relation_to_picker()
        self._persist(SettingsKey.TRANSCRIPT_BATCH_RELATION, value)

    def _on_edit_custom(self) -> None:
        if self._edit_custom_description():
            self._apply_relation_to_picker()

    def _edit_custom_description(self) -> bool:
        """Open the Custom dialog; True when the user kept a description."""
        from ui_qt.dialogs.batch_relation_dialog import BatchRelationDialog

        dialog = BatchRelationDialog(
            [item.name for item in self._items],
            self._custom_instructions,
            self._custom_combine,
            parent=self.window(),
        )
        if not dialog.exec():
            return False
        self._custom_instructions = dialog.instructions_text()
        self._custom_combine = dialog.combine_checked()
        self._persist(
            SettingsKey.TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS, self._custom_instructions
        )
        self._persist(SettingsKey.TRANSCRIPT_BATCH_CUSTOM_COMBINE, self._custom_combine)
        return True

    @staticmethod
    def _persist(key: str, value) -> None:
        try:
            settings_manager.save_setting(key, value)
        except Exception as exc:
            logger.warning("Could not save %s: %s", key, exc)

    @property
    def relation(self) -> str:
        return self._relation

    # Job lifecycle

    def _on_transcribe(self):
        if self.is_transcribing or not self._items:
            return
        if len(self._items) == 1:
            if not self._audio_path or not os.path.exists(self._audio_path):
                self.clear_file()
                self.drop_zone.set_notice("That file no longer exists — drop it again")
                return
            self.file_info_card.set_transcribing(
                True, with_cleanup=self.cleanup_check.isChecked()
            )
            self.set_backend_enabled(False)
            self.local_engine.set_busy(True)
            duration = self._preview.duration_seconds if self._preview else 0.0
            self.upload_requested.emit(self._audio_path, duration)
            return

        missing = [item for item in self._items if not os.path.exists(item.path)]
        if missing:
            for item in missing:
                self._items.remove(item)
            count = len(missing)
            self._queue_note = (
                f"{count} file{'s' if count != 1 else ''} no longer exist"
                f"{'s' if count == 1 else ''} and {'was' if count == 1 else 'were'} removed"
            )
            self._render()
            return

        self._queue_note = ""
        for item in self._items:
            item.state = "pending"
            item.transcript = ""
        total = len(self._items)
        self.file_info_card.set_transcribing(
            True, with_cleanup=self.cleanup_check.isChecked(), total_files=total
        )
        self.file_info_card.queue.render(self._items)
        self.file_info_card.queue.set_note("")
        self.set_backend_enabled(False)
        self.local_engine.set_busy(True)
        request = BatchUploadRequest(
            items=tuple(
                BatchItem(
                    item.path,
                    item.preview.duration_seconds if item.preview else None,
                )
                for item in self._items
            ),
            relation=self._relation,
            custom_instructions=self._custom_instructions,
            custom_combine=self._custom_combine,
        )
        self.upload_files_requested.emit(request)

    def _on_cancel(self):
        if not self.is_transcribing:
            return
        # The tab stays locked until the runtime's terminal signal arrives;
        # unlocking here would let a second job start under the first.
        self.file_info_card.progress.set_stopping()
        self.cancel_requested.emit()

    def set_transcript(self, text: str, raw=None):
        super().set_transcript(text, raw=raw)
        stripped = (text or "").strip()
        failed = stripped.startswith("Error:")
        copyable = bool(stripped) and stripped != EMPTY_ASR_MESSAGE and not failed
        self.file_info_card.finish_transcribing(success=not failed)
        self._cancel_pending = False
        self._unlock_engine()
        self.file_info_card.set_copy_enabled(copyable)
        self.expand_btn.setVisible(copyable)
        self._sync_viewer()

    def clear_transcription(self):
        super().clear_transcription()
        self.expand_btn.hide()
        self._sync_viewer()

    def open_transcript_viewer(self):
        """Show the current transcript in the reading window, creating it once."""
        if self._viewer is None:
            from ui_qt.dialogs.transcript_viewer_dialog import TranscriptViewerDialog

            self._viewer = TranscriptViewerDialog(self.window())
            self._viewer.copy_requested.connect(self.copy_requested)
        self._sync_viewer()
        self._viewer.show()
        self._viewer.raise_()
        self._viewer.activateWindow()

    def _sync_viewer(self):
        if self._viewer is not None:
            self._viewer.set_transcript(
                self._fixed_text, self._raw_text, title=self._viewer_title()
            )

    def _viewer_title(self) -> str:
        names = [os.path.basename(item.path) for item in self._items]
        if len(names) == 1:
            return names[0]
        if names:
            return f"{len(names)} files"
        return ""

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

    def _on_row_copy(self, path: str) -> None:
        item = self._item_for(path)
        if item is not None and item.transcript:
            self.copy_requested.emit(item.transcript)

    def clear_file(self):
        self._items = []
        self._audio_path = None
        self._preview = None
        self._cancel_pending = False
        self._queue_note = ""
        card = self.file_info_card
        card.hide()
        card.set_transcribing(False)
        card.set_copy_enabled(False)
        card.queue.render([])
        card.queue.set_note("")
        card.set_mode("single")
        card.set_ready(True)
        self.drop_zone.show()
        self._unlock_engine()

    def set_file(self, audio_path: str):
        self._on_file_selected(audio_path)

    def set_files(self, audio_paths: list[str]):
        self._on_files_selected(list(audio_paths), 0)

    def open_file_browser(self):
        self.drop_zone.open_file_browser()
