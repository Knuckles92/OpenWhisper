"""The column at the side of the screen that writes a walkthrough as it records.

While a walkthrough records, the user's voice shows as rows of ticks, like
lines of writing, and each screenshot they keep drops in below them, so
they can see what the document holds before it exists. At Done the ticks
close up into lines while the document is written, the header says what it
came to, and the column fades once the document is ready.

The column never takes focus, and it is left out of screenshots. It can be
dragged aside; each walkthrough opens it at the screen's edge again.
Compositor-managed desktops (Wayland) place their own windows, so there is
no column there.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QWidget

from services.walkthrough.document import clock
from ui_qt.utils.desktop import compositor_managed
from ui_qt.utils.drag_move import DragToMove
from ui_qt.utils.loudness import level_to_height
from ui_qt.utils.palette import token_color

#: The column's width, and its gap from the screen's right edge.
WIDTH = 296
SCREEN_MARGIN = 22
#: The column's share of the screen's height, and the most it grows to.
HEIGHT_SHARE = 0.74
MAX_HEIGHT = 820
#: Room around the card for its shadow.
SHADOW = 14
PADDING = 22
RADIUS = 18

#: One tick per this many seconds of speech: about a syllable.
TICK_S = 0.075
#: Loudness under this is the room, not the voice: it writes no tick.
SPEECH_GATE = 0.14
TICK_W = 2.0
TICK_STEP = 4.0
TICK_MIN = 3.0
TICK_MAX = 12.0
ROW_PITCH = 18.0
#: A finished row's line, once its ticks have closed up.
LINE_H = 5.0
GAP = 16.0
SHOT_MAX_H = 190.0
#: Screenshots are kept this many pixels wide: sharp at 2x.
THUMB_PX = 2 * WIDTH

FRAME_MS = 16
TICK_GROW_S = 0.12
SHOT_IN_S = 0.5
MORPH_S = 0.7
FADE_S = 0.32
#: How long the finished column says what the walkthrough came to.
HOLD_S = 2.6
#: The document never came (it failed without saying so): stop waiting.
#: Past the slowest real job: a long walkthrough transcribed on the CPU,
#: then an AI write-up, which may take up to 5 minutes.
GIVE_UP_S = 600.0


@dataclass
class Paragraph:
    """What was said between two screenshots: one height (0-1) per tick."""

    ticks: list[float] = field(default_factory=list)
    #: When each tick was written, to grow it in.
    born: list[float] = field(default_factory=list)


@dataclass
class ShotItem:
    number: int
    image: QImage
    born: float


class WalkthroughColumn:
    """What the column shows, without any painting: paragraphs and screenshots.

    ``hear`` takes microphone levels as they arrive; ``advance`` turns the
    loudest of them into a tick every ``TICK_S`` while the user speaks.
    """

    def __init__(self) -> None:
        self.items: list[Paragraph | ShotItem] = [Paragraph()]
        self.shots = 0
        self.speech_s = 0.0
        self._peak = 0.0
        self._carry = 0.0

    def hear(self, level: float) -> None:
        self._peak = max(self._peak, level_to_height(level))

    def advance(self, dt: float, now: float) -> None:
        self._carry += dt
        while self._carry >= TICK_S:
            self._carry -= TICK_S
            peak, self._peak = self._peak, 0.0
            if peak >= SPEECH_GATE:
                # Heights spread over the voice's range, not the room's.
                self._paragraph().ticks.append((peak - SPEECH_GATE) / (1.0 - SPEECH_GATE))
                self._paragraph().born.append(now)
                self.speech_s += TICK_S

    def add_shot(self, number: int, image: QImage, now: float) -> None:
        self.shots = max(self.shots, number)
        last = self.items[-1]
        if isinstance(last, Paragraph) and not last.ticks:
            # Nothing was said since the last screenshot: no empty paragraph.
            self.items.pop()
        self.items.append(ShotItem(number, image, now))
        self.items.append(Paragraph())

    def _paragraph(self) -> Paragraph:
        return self.items[-1]  # add_shot always ends on a paragraph


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t: float) -> float:
    t = min(1.0, max(0.0, t))
    c = 1.4
    return 1.0 + (c + 1.0) * (t - 1.0) ** 3 + c * (t - 1.0) ** 2


class WalkthroughPanel(QWidget):
    """The live column for the walkthrough being recorded; reused for the next."""

    RECORDING, WRITING, DONE, HIDDEN = "recording", "writing", "done", "hidden"

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("walkthroughPanel")
        self.setWindowTitle("Walkthrough")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform == "darwin":
            # The user works in another app while this column records.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self._drag = DragToMove(self)
        self.state = self.HIDDEN
        self.column = WalkthroughColumn()
        self._clock = QElapsedTimer()
        self._recorded_s = 0.0
        self._summary = ""
        self._state_at = 0.0
        self._last = 0.0
        self._scroll = 0.0
        self._opacity = 0.0
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)
        self._frame.timeout.connect(self._advance)

    # ---- what the walkthrough does ----

    def begin(self) -> None:
        """A walkthrough started recording: an empty column on the pointer's screen."""
        self.column = WalkthroughColumn()
        self._scroll = 0.0
        self._summary = ""
        self._recorded_s = 0.0
        self._clock.start()
        self._enter(self.RECORDING)
        self._drag.reset()
        self._place()
        self.show()
        self.raise_()
        self._last = time.monotonic()
        self._frame.start()

    def hear(self, level: float) -> None:
        if self.state == self.RECORDING:
            self.column.hear(level)

    def add_shot(self, number: int, image: QImage) -> None:
        if self.state == self.RECORDING and not image.isNull():
            # A thumbnail is all the column draws; a full screen is many MB.
            small = image.scaledToWidth(THUMB_PX, Qt.TransformationMode.SmoothTransformation)
            self.column.add_shot(number, small, self._now())

    def finish(self) -> None:
        """The recording stopped: close the ticks into lines while it is written."""
        if self.state != self.RECORDING:
            return
        self._recorded_s = self._clock.elapsed() / 1000.0
        self._enter(self.WRITING)

    def complete(self, result) -> None:
        """The document is ready: say what it came to, then fade."""
        if self.state not in (self.RECORDING, self.WRITING):
            return
        count = f"{result.shots} screenshot{'' if result.shots == 1 else 's'}"
        self._summary = f"{clock(result.duration_s)} · {count}"
        self._enter(self.DONE)

    def dismiss(self) -> None:
        """Canceled or failed: fade at once."""
        if self.state in (self.RECORDING, self.WRITING, self.DONE):
            self._summary = ""
            self._enter(self.DONE)
            # Start the fade now instead of after the hold.
            self._state_at -= HOLD_S

    # ---- frames ----

    def _now(self) -> float:
        return time.monotonic()

    def _enter(self, state: str) -> None:
        self.state = state
        self._state_at = self._now()

    def _since(self) -> float:
        return self._now() - self._state_at

    def _advance(self) -> None:
        now = self._now()
        dt, self._last = min(0.1, now - self._last), now
        if self.state == self.RECORDING:
            self.column.advance(dt, now)
        target = 1.0
        if self.state == self.DONE:
            target = 0.0 if self._since() >= HOLD_S else 1.0
        elif self.state == self.WRITING and self._since() >= GIVE_UP_S:
            target = 0.0
        step = dt / FADE_S
        self._opacity = min(target, self._opacity + step) if target > self._opacity \
            else max(target, self._opacity - step)
        if target == 0.0 and self._opacity <= 0.0:
            self._frame.stop()
            self.state = self.HIDDEN
            self.hide()
            return
        self.update()

    def _place(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        height = int(min(MAX_HEIGHT, area.height() * HEIGHT_SHARE)) + 2 * SHADOW
        width = WIDTH + 2 * SHADOW
        self.setFixedSize(width, height)
        self.move(area.right() - width - SCREEN_MARGIN + SHADOW + 1,
                  area.top() + (area.height() - height) // 2)

    def mousePressEvent(self, event) -> None:
        if not self._drag.press(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self._drag.move(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if not self._drag.release(event):
            super().mouseReleaseEvent(event)

    # ---- painting ----

    def _card(self) -> QRectF:
        return QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(ease_out(self._opacity))
        card = self._card()
        # Shadow, then the card.
        for i in range(SHADOW, 0, -2):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(7 * (1 - i / SHADOW) + 2)))
            painter.drawRoundedRect(card.adjusted(-i, -i + 4, i, i + 4), RADIUS + i, RADIUS + i)
        painter.setBrush(token_color("surface"))
        painter.setPen(QPen(token_color("border-strong"), 1.0))
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        inner = card.adjusted(PADDING, PADDING, -PADDING, -PADDING)
        top = self._paint_header(painter, inner)
        body = QRectF(inner.left(), top, inner.width(), inner.bottom() - top)
        self._paint_body(painter, body)
        painter.end()

    def _paint_header(self, painter: QPainter, inner: QRectF) -> float:
        title = QFont(self.font())
        title.setPixelSize(21)
        title.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title)
        painter.setPen(token_color("text"))
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width(), 28),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Walkthrough")

        sub = QFont(self.font())
        sub.setPixelSize(13)
        painter.setFont(sub)
        y = inner.top() + 36
        x = inner.left()
        if self.state == self.RECORDING:
            pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.cos(self._clock.elapsed() / 1000.0 * math.tau / 1.6))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("danger", int(255 * pulse)))
            painter.drawEllipse(QPointF(x + 5, y + 9), 4.5, 4.5)
            x += 16
            shots = self.column.shots
            text = f"Recording  {clock(self._clock.elapsed() / 1000.0)}"
            if shots:
                text += f"  ·  {shots} shot{'' if shots == 1 else 's'}"
        elif self.state == self.WRITING:
            dots = "." * (1 + int(self._since() / 0.4) % 3)
            text = f"Writing it up{dots}"
        else:
            text = self._summary or "Walkthrough"
        painter.setPen(token_color("text-secondary"))
        painter.drawText(QRectF(x, y, inner.right() - x, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        rule = y + 28
        painter.setPen(QPen(token_color("border"), 1.0))
        painter.drawLine(QPointF(inner.left(), rule), QPointF(inner.right(), rule))
        return rule + 14

    def _layout(self, width: float) -> list[tuple[Paragraph | ShotItem, float, float]]:
        """Each item with its top and height, from the body's top."""
        laid = []
        y = 0.0
        per_row = max(1, int((width + TICK_STEP - TICK_W) // TICK_STEP))
        for item in self.column.items:
            if isinstance(item, Paragraph):
                if not item.ticks:
                    # The paragraph being spoken: room for its caret.
                    if item is self.column.items[-1] and self.state == self.RECORDING:
                        laid.append((item, y, ROW_PITCH))
                        y += ROW_PITCH + GAP
                    continue
                rows = math.ceil(len(item.ticks) / per_row)
                height = rows * ROW_PITCH
            else:
                height = self._shot_size(item, width)[1]
            laid.append((item, y, height))
            y += height + GAP
        return laid

    def _shot_size(self, shot: ShotItem, width: float) -> tuple[float, float]:
        ratio = shot.image.height() / max(1, shot.image.width())
        h = min(SHOT_MAX_H, width * ratio)
        return h / ratio, h

    def _paint_body(self, painter: QPainter, body: QRectF) -> None:
        laid = self._layout(body.width())
        content_h = laid[-1][1] + laid[-1][2] if laid else 0.0
        # Keep the newest at the bottom in view, gliding there.
        target = max(0.0, content_h - body.height())
        self._scroll += (target - self._scroll) * min(1.0, FRAME_MS / 1000.0 / 0.18)
        painter.save()
        clip = QPainterPath()
        clip.addRect(body.adjusted(-SHADOW, 0, SHADOW, 0))
        painter.setClipPath(clip)
        now = self._now()
        morph = ease_out(self._since() / MORPH_S) if self.state in (self.WRITING, self.DONE) else 0.0
        for item, top, height in laid:
            y = body.top() + top - self._scroll
            if y + height < body.top() - 20 or y > body.bottom() + 20:
                continue
            if isinstance(item, Paragraph):
                self._paint_paragraph(painter, item, body.left(), y, body.width(), now, morph)
            else:
                self._paint_shot(painter, item, body.left(), y, body.width(), now)
        painter.restore()
        # Fade the top edge where older items scroll away.
        if self._scroll > 1.0:
            painter.save()
            fade = QRectF(body.left(), body.top(), body.width(), 22)
            gradient = QLinearGradient(fade.topLeft(), fade.bottomLeft())
            gradient.setColorAt(0.0, token_color("surface"))
            surface = token_color("surface")
            surface.setAlpha(0)
            gradient.setColorAt(1.0, surface)
            painter.fillRect(fade, gradient)
            painter.restore()

    def _paint_paragraph(self, painter: QPainter, paragraph: Paragraph, left: float,
                         top: float, width: float, now: float, morph: float) -> None:
        per_row = max(1, int((width + TICK_STEP - TICK_W) // TICK_STEP))
        color = token_color("text-soft")
        line = token_color("text-faint")
        painter.setPen(Qt.PenStyle.NoPen)
        for row_start in range(0, len(paragraph.ticks), per_row):
            row = paragraph.ticks[row_start:row_start + per_row]
            centre = top + (row_start // per_row) * ROW_PITCH + ROW_PITCH / 2
            if morph >= 1.0:
                painter.setBrush(line)
                length = (len(row) - 1) * TICK_STEP + TICK_W
                painter.drawRoundedRect(QRectF(left, centre - LINE_H / 2, length, LINE_H),
                                        LINE_H / 2, LINE_H / 2)
                continue
            # Ticks widen until they touch and settle at the line's height.
            w = TICK_W + (TICK_STEP - TICK_W) * morph
            mixed = QColor(
                round(color.red() + (line.red() - color.red()) * morph),
                round(color.green() + (line.green() - color.green()) * morph),
                round(color.blue() + (line.blue() - color.blue()) * morph),
            )
            painter.setBrush(mixed)
            for i, height in enumerate(row):
                born = paragraph.born[row_start + i]
                grow = ease_out((now - born) / TICK_GROW_S)
                tick_h = (TICK_MIN + (TICK_MAX - TICK_MIN) * height) * grow
                h = tick_h + (LINE_H - tick_h) * morph
                x = left + i * TICK_STEP
                painter.drawRoundedRect(QRectF(x, centre - h / 2, w, h), 1.0, 1.0)
        if self.state == self.RECORDING and paragraph is self.column.items[-1]:
            self._paint_caret(painter, paragraph, left, top, per_row)

    def _paint_caret(self, painter: QPainter, paragraph: Paragraph, left: float,
                     top: float, per_row: int) -> None:
        count = len(paragraph.ticks)
        row, col = divmod(count, per_row)
        x = left + col * TICK_STEP + 2
        centre = top + row * ROW_PITCH + ROW_PITCH / 2
        blink = 0.5 + 0.5 * math.cos(self._clock.elapsed() / 1000.0 * math.tau / 1.1)
        painter.setBrush(token_color("accent", int(90 + 165 * blink)))
        painter.drawRoundedRect(QRectF(x, centre - 7, 2, 14), 1, 1)

    def _paint_shot(self, painter: QPainter, shot: ShotItem, left: float, top: float,
                    width: float, now: float) -> None:
        w, h = self._shot_size(shot, width)
        t = (now - shot.born) / SHOT_IN_S
        scale = 0.82 + 0.18 * ease_out_back(t)
        rect = QRectF(left + (width - w) / 2, top, w, h)
        centre = rect.center()
        painter.save()
        painter.setOpacity(painter.opacity() * ease_out(t * 1.6))
        painter.translate(centre.x() + 24 * (1 - ease_out(t)), centre.y())
        painter.scale(scale, scale)
        painter.translate(-centre.x(), -centre.y())
        painter.setPen(Qt.PenStyle.NoPen)
        for i in (6, 4, 2):
            painter.setBrush(QColor(0, 0, 0, 16))
            painter.drawRoundedRect(rect.adjusted(-i / 2, -i / 2 + 3, i / 2, i / 2 + 3), 6 + i, 6 + i)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 6, 6)
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, shot.image)
        painter.restore()


def make_panel() -> WalkthroughPanel | None:
    """A column, or None where the compositor places windows (Wayland)."""
    if compositor_managed():
        return None
    return WalkthroughPanel()
