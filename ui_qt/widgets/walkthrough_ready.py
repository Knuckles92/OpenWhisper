"""The card that says a walkthrough is ready: open it, find it, or drag it anywhere.

It sits in the corner of the screen under the pointer without taking focus,
so the user can paste straight into the app they were in. Dragging the file
chip works in every app that takes files, including ones a pasted file
doesn't reach. Compositor-managed desktops (Wayland) place their own
windows, so there the status line and the overlay's notice say it instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QMimeData, QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QCursor,
    QDesktopServices,
    QDrag,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.desktop import compositor_managed
from ui_qt.utils.file_reveal import reveal_in_file_manager
from ui_qt.utils.icons import design_icon

logger = logging.getLogger(__name__)

#: How long the card stays when the pointer never visits it.
SHOW_MS = 14_000
#: How long it stays once the pointer leaves it.
LINGER_MS = 4_000
THUMB_SIZE = QSize(76, 48)
SCREEN_MARGIN = 18


def show_ready(card: Optional["WalkthroughReadyCard"], result, *, copied: bool):
    """Show ``card`` (made the first time) for ``result``; the card, or None on Wayland."""
    if compositor_managed():
        return None
    if card is None:
        card = WalkthroughReadyCard()
    card.present(result, copied=copied)
    return card


def _duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _thumbnail(path: Optional[Path]) -> Optional[QPixmap]:
    """The first screenshot cropped to the thumbnail's shape, with rounded corners."""
    image = QImage(str(path)) if path is not None else QImage()
    if image.isNull():
        return None
    scaled = image.scaled(THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    x = (scaled.width() - THUMB_SIZE.width()) // 2
    y = (scaled.height() - THUMB_SIZE.height()) // 2
    cropped = scaled.copy(QRect(QPoint(x, y), THUMB_SIZE))
    rounded = QPixmap(THUMB_SIZE)
    rounded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(rounded.rect()), 7, 7)
    painter.setClipPath(clip)
    painter.drawImage(0, 0, cropped)
    painter.end()
    return rounded


class _FileChip(QFrame):
    """The document as something to drag into any app that takes files."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("walkthroughFileChip")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag into any app to attach it")
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 5, 11, 5)
        row.setSpacing(6)
        icon = QLabel()
        icon.setObjectName("walkthroughFileIcon")
        icon.setPixmap(design_icon("file-text-blue.svg").pixmap(14, 14))
        row.addWidget(icon)
        self.name = QLabel("")
        self.name.setObjectName("walkthroughFileName")
        row.addWidget(self.name)
        self._path: Optional[Path] = None
        self._press: Optional[QPoint] = None

    def set_path(self, path: Path) -> None:
        self._path = path
        self.name.setText(path.name)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press is None or self._path is None:
            return
        if (event.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._press = None
        drag = QDrag(self)
        drag.setMimeData(self.mime_data())
        drag.exec(Qt.DropAction.CopyAction)

    def mouseReleaseEvent(self, event) -> None:
        self._press = None
        super().mouseReleaseEvent(event)

    def mime_data(self) -> QMimeData:
        """The file as a drop: a file URL, which apps attach and terminals type as a path."""
        data = QMimeData()
        if self._path is not None:
            data.setUrls([QUrl.fromLocalFile(str(self._path))])
        return data


class WalkthroughReadyCard(QWidget):
    """A corner card for the newest walkthrough; reused for the next one."""

    def __init__(self):
        super().__init__(None)
        self.setObjectName("walkthroughReady")
        self.setWindowTitle("Walkthrough")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self._result = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("walkthroughReadySurface")
        outer.addWidget(surface)
        layout = QHBoxLayout(surface)
        layout.setContentsMargins(12, 12, 10, 12)
        layout.setSpacing(12)
        self.thumb = QLabel()
        self.thumb.setObjectName("walkthroughThumb")
        self.thumb.setFixedSize(THUMB_SIZE)
        layout.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(self._details(), 1)
        close = QToolButton()
        close.setObjectName("walkthroughReadyClose")
        close.setIcon(design_icon("x-gray.svg"))
        close.setIconSize(QSize(13, 13))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setAccessibleName("Close")
        close.clicked.connect(self.hide)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

    def _details(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(3)
        self.title = QLabel("")
        self.title.setObjectName("walkthroughReadyTitle")
        column.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("walkthroughReadySubtitle")
        column.addWidget(self.subtitle)
        row = QHBoxLayout()
        row.setContentsMargins(0, 6, 0, 0)
        row.setSpacing(4)
        self.chip = _FileChip(self)
        row.addWidget(self.chip)
        row.addStretch(1)
        self.open_button = self._action("Open", self._open)
        row.addWidget(self.open_button)
        self.folder_button = self._action("Show in folder", self._reveal)
        row.addWidget(self.folder_button)
        column.addLayout(row)
        return column

    def _action(self, text: str, slot) -> QToolButton:
        button = QToolButton()
        button.setProperty("walkthroughAction", True)
        button.setText(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    def present(self, result, *, copied: bool) -> None:
        """Fill the card for ``result`` and show it in the corner of the pointer's screen."""
        self._result = result
        count = f"{result.shots} screenshot{'' if result.shots == 1 else 's'}"
        if copied:
            self.title.setText("Walkthrough copied")
            self.subtitle.setText(f"{count} · {_duration(result.duration_s)} — paste it into your agent")
        else:
            self.title.setText("Walkthrough saved")
            self.subtitle.setText(f"{count} · copying failed — drag it from here")
        thumb = _thumbnail(result.first_shot)
        self.thumb.setVisible(thumb is not None)
        if thumb is not None:
            self.thumb.setPixmap(thumb)
        self.chip.set_path(result.pdf)
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(SHOW_MS)

    def _place(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = self.sizeHint()
        self.move(area.right() - size.width() - SCREEN_MARGIN + 1,
                  area.bottom() - size.height() - SCREEN_MARGIN + 1)

    def enterEvent(self, event) -> None:
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self.isVisible():
            self._timer.start(LINGER_MS)
        super().leaveEvent(event)

    def _open(self) -> None:
        if self._result is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result.pdf)))
        self.hide()

    def _reveal(self) -> None:
        if self._result is not None:
            reveal_in_file_manager(str(self._result.pdf))
        self.hide()
