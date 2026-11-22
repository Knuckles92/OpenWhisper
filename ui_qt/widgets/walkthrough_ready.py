"""The card that says a walkthrough is ready: open it, review it, or drag it anywhere.

It sits in the corner of the screen under the pointer without taking focus,
so the user can paste straight into the app they were in. Dragging the file
chip works in every app that takes files, including ones a pasted file
doesn't reach; clicking it opens the document. Copying again and showing the
folder wait behind ``⋯``. Compositor-managed desktops (Wayland) place their own
windows, so there the status line and the overlay's notice say it instead.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QMimeData,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
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

from services.settings import (
    SettingsKey,
    WalkthroughCopyFormat,
    resolve_walkthrough_copy_format,
    settings_manager,
)
from services.walkthrough.document import clock
from ui_qt.utils.desktop import compositor_managed
from ui_qt.utils.file_reveal import reveal_in_file_manager
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.context_menu import context_menu
from ui_qt.widgets.walkthrough_copy_tips import set_copy_tip
from ui_qt.widgets.walkthrough_send import SendButton, relay, start_send

logger = logging.getLogger(__name__)

#: How long the card stays when the pointer never visits it.
SHOW_MS = 14_000
#: How long it stays once the pointer leaves it.
LINGER_MS = 4_000
THUMB_SIZE = QSize(76, 48)
SCREEN_MARGIN = 18
#: Wraps a write-up failure's reason instead of widening the card.
REASON_WIDTH = 300


def show_ready(card: Optional["WalkthroughReadyCard"], result, *, copied: bool):
    """Show ``card`` (made the first time) for ``result``; the card, or None on Wayland."""
    if compositor_managed():
        return None
    if card is None:
        card = WalkthroughReadyCard()
    card.present(result, copied=copied)
    return card


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


class FileChip(QFrame):
    """The document to drag into any app that takes files, or to click and open."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("walkthroughFileChip")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Click to open · drag into any app to attach it")
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

    @property
    def path(self) -> Path | None:
        return self._path

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
        pressed, self._press = self._press, None
        if pressed is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def mime_data(self) -> QMimeData:
        """The file as a drop: a file URL, which apps attach and terminals type as a path."""
        data = QMimeData()
        if self._path is not None:
            data.setUrls([QUrl.fromLocalFile(str(self._path))])
        return data


class WalkthroughReadyCard(QWidget):
    """A corner card for the newest walkthrough; reused for the next one."""

    review_requested = pyqtSignal(object)
    #: The result and the ``WalkthroughCopyFormat`` to copy it in.
    copy_requested = pyqtSignal(object, str)

    def __init__(self):
        super().__init__(None)
        self.setObjectName("walkthroughReady")
        self.setWindowTitle("Walkthrough")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform == "darwin":
            # Keep Open, Copy and the drag chip available in the destination app.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self._result = None
        #: The send in flight for this card's walkthrough, or 0.
        self._send_token = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._build()
        relay().sent.connect(self._sent)

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
        # Why the write-up failed, in full: the status line cuts it off.
        self.reason = QLabel("")
        self.reason.setObjectName("walkthroughReadyReason")
        self.reason.setWordWrap(True)
        self.reason.setMaximumWidth(REASON_WIDTH)
        self.reason.setVisible(False)
        column.addWidget(self.reason)
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(4)
        self.chip = FileChip(self)
        self.chip.clicked.connect(self._open_chip)
        row.addWidget(self.chip)
        row.addStretch(1)
        self.review_button = QToolButton()
        self.review_button.setProperty("walkthroughAction", True)
        self.review_button.setText("Review")
        self.review_button.setToolTip("Edit the words, leave screenshots out, or redo the write-up")
        self.review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.review_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.review_button.clicked.connect(self._review)
        row.addWidget(self.review_button)
        row.addWidget(self._send_button())
        row.addWidget(self._more_button())
        column.addLayout(row)
        return column

    def _send_button(self) -> SendButton:
        """``Send to ⌄``: hand the walkthrough to a coding agent from right here."""
        button = SendButton(name="walkthroughReadySend", chevron_color="accent-soft", chevron_inset=12)
        button.setProperty("walkthroughAction", True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The pointer leaves the card for the menu; don't let the card fade under it.
        button.send_menu.aboutToShow.connect(self._timer.stop)
        button.send_menu.aboutToHide.connect(self._linger)
        button.send_requested.connect(self._send)
        self.send_button = button
        return button

    def _more_button(self) -> QToolButton:
        """``⋯``: what a moment after finishing rarely needs."""
        self.menu = context_menu(self)
        self.menu.setToolTipsVisible(True)
        self.open_action = self._menu_action("Open PDF", self._open)
        self.open_word_action = self._menu_action("Open Word document", self._open_word)
        self._menu_action("Show in folder", self._reveal)
        self.menu.addSeparator()
        self.copy_pdf_action = self._menu_action(
            "Copy PDF", lambda: self._copy(WalkthroughCopyFormat.PDF))
        self.copy_agent_action = self._menu_action(
            "Copy for agent", lambda: self._copy(WalkthroughCopyFormat.AGENT_TASK))
        self.copy_word_action = self._menu_action(
            "Copy Word document", lambda: self._copy(WalkthroughCopyFormat.WORD))
        self.copy_markdown_action = self._menu_action(
            "Copy Markdown", lambda: self._copy(WalkthroughCopyFormat.AGENT))
        for action, copy_format in (
                (self.copy_pdf_action, WalkthroughCopyFormat.PDF),
                (self.copy_agent_action, WalkthroughCopyFormat.AGENT_TASK),
                (self.copy_word_action, WalkthroughCopyFormat.WORD),
                (self.copy_markdown_action, WalkthroughCopyFormat.AGENT)):
            set_copy_tip(action, copy_format)
        # The pointer leaves the card for the menu; don't let the card fade under it.
        self.menu.aboutToShow.connect(self._timer.stop)
        self.menu.aboutToHide.connect(self._linger)
        button = QToolButton()
        button.setObjectName("walkthroughReadyMore")
        button.setIcon(design_icon("dots-gray.svg"))
        button.setIconSize(QSize(16, 16))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAccessibleName("More")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(self.menu)
        self.more_button = button
        return button

    def _menu_action(self, text: str, slot) -> QAction:
        action = self.menu.addAction(text)
        action.triggered.connect(slot)
        return action

    def present(self, result, *, copied: bool) -> None:
        """Fill the card for ``result`` and show it in the corner of the pointer's screen."""
        self._result = result
        # A send still going is the previous walkthrough's; its answer isn't this card's.
        self._send_token = 0
        self.send_button.look_ahead()
        settings = settings_manager.load_all_settings()
        copy_format = resolve_walkthrough_copy_format(settings)
        count = f"{result.shots} screenshot{'' if result.shots == 1 else 's'}"
        if copied:
            self.title.setText(f"{result.name} copied")
            note = "write-up failed, copied as spoken" if result.write_up_error else "paste it into your agent"
            if copy_format == WalkthroughCopyFormat.AGENT_TASK:
                if SettingsKey.AUTO_PASTE.read(settings):
                    note = "preparing to paste PDF, then instructions"
                    if result.write_up_error:
                        note = "write-up failed; preparing to paste as-spoken PDF, then instructions"
                else:
                    note = "paste instructions, then attach the PDF to your chat"
                    if result.write_up_error:
                        note = "write-up failed; paste instructions and attach the as-spoken PDF"
            self.subtitle.setText(f"{count} · {clock(result.duration_s)} — {note}")
        else:
            self.title.setText(f"{result.name} saved")
            self.subtitle.setText(f"{count} · copying failed — drag it from here")
        error = getattr(result, "write_up_error", "")
        self.reason.setText(f"Write-up failed: {error}" if error else "")
        self.reason.setVisible(bool(error))
        thumb = _thumbnail(result.first_shot)
        self.thumb.setVisible(thumb is not None)
        if thumb is not None:
            self.thumb.setPixmap(thumb)
        self._set_chip(result, copy_format)
        has_word = result.docx.is_file()
        self.open_word_action.setEnabled(has_word)
        self.copy_word_action.setEnabled(has_word)
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(SHOW_MS)

    def _set_chip(self, result, copy_format: str) -> None:
        path = {
            WalkthroughCopyFormat.WORD: result.docx,
            WalkthroughCopyFormat.AGENT: getattr(result, "markdown", result.pdf),
        }.get(copy_format, result.pdf)
        self.chip.set_path(path if path.is_file() else result.pdf)

    def show_copy_status(self, result, copy_format: str, *, copied: bool) -> None:
        if self._result is None or self._result.folder != result.folder:
            return
        if copied:
            self._result = result
            self._set_chip(result, copy_format)
        self.subtitle.setText("Copied!" if copied else "Couldn't copy the walkthrough.")
        self.adjustSize()
        self._place()

    def show_paste_status(self, result, status: str) -> None:
        """Replace manual-copy guidance only for the card being pasted."""
        if self._result is None or self._result.pdf != result.pdf:
            return
        self.title.setText(result.name)
        self.subtitle.setText(status)
        self.adjustSize()
        self._place()

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
        if not self.menu.isVisible() and not self.send_button.send_menu.isVisible():
            self._linger()
        super().leaveEvent(event)

    def _linger(self) -> None:
        if self.isVisible() and not self.underMouse():
            self._timer.start(LINGER_MS)

    def _review(self) -> None:
        if self._result is not None:
            self.review_requested.emit(self._result.folder)
            self.hide()

    def _copy(self, copy_format: str) -> None:
        if self._result is not None:
            self.copy_requested.emit(self._result, copy_format)
            self._timer.start(SHOW_MS)

    def _send(self, dest) -> None:
        if self._result is None or self._send_token:
            return
        self.subtitle.setText(f"Sending to {dest.name}…")
        self.reason.setVisible(False)
        self._send_token = start_send(self._result, dest)
        self.adjustSize()
        self._place()
        self._timer.start(SHOW_MS)

    def _sent(self, token: int, ok: bool, message: str) -> None:
        if token != self._send_token:
            return
        self._send_token = 0
        # A failure's reason wraps below; the subtitle line would cut it off.
        self.subtitle.setText(message if ok else "Couldn't send it.")
        self.reason.setText("" if ok else message)
        self.reason.setVisible(not ok)
        self.adjustSize()
        self._place()
        if self.isVisible():
            self._timer.start(SHOW_MS)

    def _open_chip(self) -> None:
        if self.chip.path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.chip.path)))
        self.hide()

    def _open(self) -> None:
        if self._result is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result.pdf)))
        self.hide()

    def _open_word(self) -> None:
        if self._result is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result.docx)))
        self.hide()

    def _reveal(self) -> None:
        if self._result is not None:
            reveal_in_file_manager(str(self._result.pdf))
        self.hide()
