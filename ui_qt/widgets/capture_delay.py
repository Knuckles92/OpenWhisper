"""A quiet delay chip and a nonactivating preset picker for automatic captures."""

from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import (
    WALKTHROUGH_CAPTURE_DELAYS,
    SettingsKey,
    resolve_walkthrough_capture_delay,
    settings_manager,
)
from ui_qt.snap import affinity, macos
from ui_qt.snap.chrome import chrome_font, paint_panel
from ui_qt.snap.icons import paint_icon
from ui_qt.utils.palette import current_palette


class CaptureDelay(QWidget):
    changed = pyqtSignal(int)
    cancelled = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(max(148, QFontMetrics(chrome_font(self.font(), 12)).horizontalAdvance("Delay · Instant") + 61), 34)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus if parent is not None else Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setToolTip("Wait after each click or mark before taking a screenshot. "
                        "Keep drawing to restart the countdown. Esc while drawing cancels a pending shot.")
        self.seconds = resolve_walkthrough_capture_delay()
        self.remaining = 0.0
        self.total = 0.0
        self._pressed = False
        self._menu: _DelayPicker | None = None
        self._accessible()

    def _accessible(self) -> None:
        self.setAccessibleName(f"Capture delay: {self.seconds} seconds" if self.seconds
                               else "Capture delay: instant")

    def set_seconds(self, seconds: int) -> None:
        self.seconds = seconds
        self._accessible()
        self.update()

    def set_countdown(self, remaining: float, total: float = 0.0) -> None:
        self.remaining, self.total = max(0.0, remaining), total
        self.update()

    def popup_windows(self) -> tuple[QWidget, ...]:
        return (self._menu,) if self._menu is not None else ()

    def close_picker(self) -> None:
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
            self._menu = None

    def hideEvent(self, event) -> None:
        self.close_picker()
        super().hideEvent(event)

    def event(self, event) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        self._pressed = event.button() == Qt.MouseButton.LeftButton

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, False
        if not pressed or event.button() != Qt.MouseButton.LeftButton or not self.rect().contains(event.pos()):
            return
        if self.remaining and event.position().x() >= self.width() - 27:
            self.cancelled.emit()
        elif self._menu is not None and self._menu.isVisible():
            self.close_picker()
        else:
            self.close_picker()
            self._menu = _DelayPicker(self)
            self._menu.chosen.connect(self._choose)
            self._menu.open()

    def _choose(self, seconds: int) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_CAPTURE_DELAY, seconds)
        self.set_seconds(seconds)
        self.close_picker()
        self.changed.emit(seconds)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_picker()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            index = WALKTHROUGH_CAPTURE_DELAYS.index(self.seconds)
            step = 1 if key in (Qt.Key.Key_Right, Qt.Key.Key_Up) else -1
            self._choose(WALKTHROUGH_CAPTURE_DELAYS[max(0, min(len(WALKTHROUGH_CAPTURE_DELAYS) - 1, index + step))])
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        active = self.remaining > 0
        ink = palette.color("accent" if active or self.seconds else "text-secondary")
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        painter.setPen(QPen(palette.color("accent-tint-border" if active or self.hasFocus() else "border"), 1))
        painter.setBrush(palette.color("accent-tint" if active else "surface-hover" if self.underMouse() else "surface"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)
        ring = QRectF(11, 9, 16, 16)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(palette.color("border-strong") if active else ink, 1.6))
        painter.drawEllipse(ring)
        painter.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if active:
            painter.drawArc(ring, 90 * 16, -round(360 * 16 * min(1, self.remaining / max(.001, self.total))))
        else:
            painter.drawLine(QPointF(19, 12), QPointF(19, 17))
            painter.drawLine(QPointF(19, 17), QPointF(22, 19))
        painter.setFont(chrome_font(self.font(), 12))
        painter.setPen(palette.color("text"))
        text = f"Snap in {self.remaining:.1f}s" if active else (
            f"Delay · {self.seconds}s" if self.seconds else "Delay · Instant")
        painter.drawText(QRectF(35, 0, self.width() - 61, self.height()),
                         Qt.AlignmentFlag.AlignVCenter, text)
        if active:
            paint_icon(painter, "drop", QRectF(self.width() - 23, 10, 14, 14), ink)
        else:
            painter.setPen(QPen(ink, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            x = self.width() - 17
            painter.drawLine(QPointF(x - 3, 15), QPointF(x, 18))
            painter.drawLine(QPointF(x, 18), QPointF(x + 3, 15))
        painter.end()


class _DelayPicker(QWidget):
    chosen = pyqtSignal(int)

    def __init__(self, chip: CaptureDelay) -> None:
        super().__init__(chip, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.chip = chip
        self.setFixedSize(264, 170)
        self.setWindowTitle("Automatic screenshot delay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMouseTracking(True)
        self._hover = -1
        self._pressed = -1
        self._away: float | None = None
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._check_pointer)

    def open(self) -> None:
        anchor = self.chip.mapToGlobal(self.chip.rect().bottomLeft())
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(anchor.x(), area.right() - self.width() + 1))
        y = anchor.y() + 7
        if y + self.height() > area.bottom():
            y = anchor.y() - self.chip.height() - self.height() - 7
        self.move(x, max(area.top(), y))
        macos.make_nonactivating(self)
        self.show()
        macos.join_all_spaces(self)
        macos.cover(self)
        macos.set_level(self, macos.PARKED_LEVEL)
        affinity.keep_out_of_screenshots(self, True)
        self.raise_()
        self._watch.start()

    def _check_pointer(self) -> None:
        if self.geometry().contains(QCursor.pos()) or self.chip.window().geometry().contains(QCursor.pos()):
            self._away = None
        elif self._away is None:
            self._away = time.monotonic()
        elif time.monotonic() - self._away > .6:
            self.close()

    def closeEvent(self, event) -> None:
        self._watch.stop()
        super().closeEvent(event)

    def _option(self, index: int) -> QRectF:
        return QRectF(14 + index % 3 * 80, 68 + index // 3 * 42, 76, 36)

    def _at(self, position: QPointF) -> int:
        return next((i for i in range(len(WALKTHROUGH_CAPTURE_DELAYS))
                     if self._option(i).contains(position)), -1)

    def mouseMoveEvent(self, event) -> None:
        self._hover = self._at(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._hover >= 0 else Qt.CursorShape.ArrowCursor)
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        self._pressed = self._at(event.position()) if event.button() == Qt.MouseButton.LeftButton else -1

    def mouseReleaseEvent(self, event) -> None:
        index, self._pressed = self._pressed, -1
        if event.button() == Qt.MouseButton.LeftButton and index >= 0 and index == self._at(event.position()):
            self.chosen.emit(WALKTHROUGH_CAPTURE_DELAYS[index])

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        paint_panel(painter, QRectF(self.rect()).adjusted(2, 2, -2, -2), 14, palette, shadow=0)
        painter.setFont(chrome_font(self.font(), 13))
        painter.setPen(palette.color("text"))
        painter.drawText(QRectF(16, 13, 232, 22), Qt.AlignmentFlag.AlignVCenter, "Screenshot delay")
        painter.setFont(chrome_font(self.font(), 11))
        painter.setPen(palette.color("text-secondary"))
        painter.drawText(QRectF(16, 38, 232, 18), Qt.AlignmentFlag.AlignVCenter,
                         "Restarts after each click or mark")
        for index, seconds in enumerate(WALKTHROUGH_CAPTURE_DELAYS):
            selected = seconds == self.chip.seconds
            rect = self._option(index)
            painter.setPen(QPen(palette.color("accent-border" if selected else "border"), 1))
            painter.setBrush(palette.color("accent-tint-strong" if selected else
                                           "surface-hover" if index == self._hover else "surface"))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(palette.color("accent" if selected else "text"))
            painter.setFont(chrome_font(self.font(), 12))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{seconds}s" if seconds else "Instant")
        painter.end()
