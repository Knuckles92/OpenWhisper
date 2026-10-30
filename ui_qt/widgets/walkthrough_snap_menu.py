"""The walkthrough column's Snap menu: what Snap takes, from now on.

It opens beside the column, away from the screen's edge, and lists the
kinds of screenshot this desktop can take. Picking one takes it at once and
makes it what the Snap button and the Screenshot shortcut repeat.

Like the column it never takes the focus, so the app being shown keeps the
keyboard (a Qt popup would take it on Windows, even told not to). Without
the focus it can't hear a click in another app, so it closes once the
pointer has been away from it and the column for a moment, as well as on a
pick or another click in the column.
"""

from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import WalkthroughSnapMode
from ui_qt.snap import keep_out_of_screenshots, macos
from ui_qt.utils.palette import token_color
from ui_qt.widgets.walkthrough_icons import paint_icon

WIDTH = 250
PAD = 5
HEADER_H = 26
ITEM_H = 36
RADIUS = 12
#: Room around the menu for its shadow.
SHADOW = 12
#: Its gap from the column's card.
GAP = 10
HEADER = "Snap repeats the one you pick"
#: The pointer away from the menu and the column this long closes it.
AWAY_S = 0.6
WATCH_MS = 100


class SnapMenu(QWidget):
    """The kinds of screenshot to pick from; emits ``chosen`` with a WalkthroughSnapMode."""

    chosen = pyqtSignal(str)

    def __init__(self, modes: tuple[str, ...], current: str, shortcut: str = "") -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
            macos.make_nonactivating(self)
        self.setMouseTracking(True)
        # Where the pointer may go without closing the menu: the column.
        self._near = QRect()
        self._away_since: float | None = None
        self._watch = QTimer(self)
        self._watch.setInterval(WATCH_MS)
        self._watch.timeout.connect(self._check_pointer)
        self.modes = modes
        self.current = current
        self.shortcut = shortcut
        self._hover = ""
        height = PAD + HEADER_H + ITEM_H * len(modes) + PAD
        self.setFixedSize(QSize(WIDTH + 2 * SHADOW, height + 2 * SHADOW))

    def open_beside(self, card: QRect, bottom: int, near: QRect | None = None) -> None:
        """Open left of ``card`` (global), its foot level with ``bottom``; right of it
        where the screen has no room on the left. The pointer may wander over
        ``near`` (the column) without closing it."""
        self._near = near if near is not None else card
        screen = QApplication.screenAt(card.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect()
        x = card.left() - GAP - WIDTH - SHADOW
        if area.isValid() and x + SHADOW < area.left():
            x = card.right() + GAP - SHADOW
        y = bottom - self.height() + SHADOW
        if area.isValid():
            y = max(area.top() - SHADOW, y)
        self.move(x, y)
        self.show()
        self.raise_()
        # A pick closes it just before the screen is grabbed. Elsewhere the
        # snap waits for the column to leave the screen, and the menu with it.
        keep_out_of_screenshots(self, True)
        self._watch.start()

    def _check_pointer(self) -> None:
        pointer = QCursor.pos()
        if self.geometry().contains(pointer) or self._near.contains(pointer):
            self._away_since = None
        elif self._away_since is None:
            self._away_since = time.monotonic()
        elif time.monotonic() - self._away_since >= AWAY_S:
            self.close()

    def _menu(self) -> QRectF:
        return QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)

    def _item(self, index: int) -> QRectF:
        menu = self._menu()
        return QRectF(menu.left() + PAD, menu.top() + PAD + HEADER_H + index * ITEM_H,
                      menu.width() - 2 * PAD, ITEM_H)

    def _at(self, position: QPointF) -> str:
        return next((mode for index, mode in enumerate(self.modes)
                     if self._item(index).contains(position)), "")

    def mouseMoveEvent(self, event) -> None:
        hover = self._at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = ""
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        mode = self._at(event.position())
        if mode:
            self.close()
            self.chosen.emit(mode)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        menu = self._menu()
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(SHADOW, 0, -2):
            painter.setBrush(QColor(0, 0, 0, int(10 * (1 - i / SHADOW) + 3)))
            painter.drawRoundedRect(menu.adjusted(-i, -i + 5, i, i + 5), RADIUS + i, RADIUS + i)
        painter.setBrush(token_color("surface"))
        painter.setPen(QPen(token_color("border-hover"), 1.0))
        painter.drawRoundedRect(menu.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        small = QFont(self.font())
        small.setPixelSize(11)
        painter.setFont(small)
        painter.setPen(token_color("text-secondary"))
        painter.drawText(QRectF(menu.left() + PAD + 10, menu.top() + PAD + 2, menu.width(), HEADER_H - 4),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, HEADER)

        label = QFont(self.font())
        label.setPixelSize(13)
        # The one under the pointer is lit; with none, the one Snap repeats.
        lit = self._hover or self.current
        for index, mode in enumerate(self.modes):
            item = self._item(index)
            if mode == lit:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(token_color("accent-tint-strong"))
                painter.drawRoundedRect(item, 7, 7)
            if mode == self.current:
                paint_icon(painter, "check", QPointF(item.left() + 18, item.center().y()), 14,
                           token_color("accent"))
            text = QRectF(item.left() + 36, item.top(), item.width() - 46, item.height())
            if mode == self.current and self.shortcut:
                painter.setFont(small)
                painter.setPen(token_color("text-secondary"))
                painter.drawText(text, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 self.shortcut)
            painter.setFont(label)
            painter.setPen(token_color("text"))
            painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             WalkthroughSnapMode.LABELS[mode])
        painter.end()
