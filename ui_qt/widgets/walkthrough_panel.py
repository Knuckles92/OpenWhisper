"""The column at the side of the screen that writes a walkthrough as it records.

While a walkthrough records, the user's voice shows as rows of ticks, like
lines of writing, and each screenshot they keep drops in below them,
numbered, so they can see what the document holds before it exists. The
section being spoken sits in an open slot until the next screenshot closes
it. Hovering a screenshot offers to move it up or down or remove it; its
words always go with it, since a section is the words and then the
screenshot they led to. The footer takes screenshots (its menu picks the
kind Snap repeats), undoes the last one, cancels and finishes. While Hold
for note is held, what the user says is a note for the AI write-up, not
the document: it shows as a purple row. At Done the ticks close up into lines while the
document is written, the header says what it came to, and the column fades
once the document is ready.

The column never takes focus, so the app being shown keeps the keyboard,
and it is left out of screenshots. It can be dragged aside by anything
that isn't a button; each walkthrough opens it at the screen's edge again.
Compositor-managed desktops (Wayland) place their own windows, so there is
no column there: the recording overlay's Snap and Done stand in for it.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
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
from PyQt6.QtWidgets import QApplication, QToolTip, QWidget

from services.settings import WalkthroughSnapMode
from services.walkthrough.document import clock
from ui_qt.snap import macos, pointed
from ui_qt.utils.desktop import compositor_managed
from ui_qt.utils.drag_move import DragToMove
from ui_qt.utils.loudness import level_to_height
from ui_qt.utils.palette import token_color
from ui_qt.widgets.walkthrough_icons import paint_icon

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
GAP = 14.0
SHOT_MAX_H = 190.0
#: Screenshots are kept this many pixels wide: sharp at 2x.
THUMB_PX = 2 * WIDTH

#: The open slot of the section being spoken.
SLOT_PAD_X = 10.0
SLOT_PAD_TOP = 8.0
SLOT_PAD_BOTTOM = 10.0
SLOT_BADGE = 20.0
SLOT_TIME_W = 34.0
SLOT_HINT_H = 16.0
SLOT_HINT = "Talking… Snap to close this section"
#: The tools that hover over a screenshot, and its number badge.
TOOL = 28.0
TOOL_GAP = 4.0
BADGE_H = 18.0
#: Chips drawn on a screenshot stay dark in either theme: the picture is
#: what they sit on.
ON_IMAGE = QColor(28, 28, 30, 220)
ON_IMAGE_HOVER = QColor(72, 72, 74, 235)
ON_IMAGE_TEXT = QColor("#f5f5f7")
#: The footer's buttons.
FOOT_H = 40.0
FOOT_GAP = 6.0
#: A square footer button, holding only an icon.
FOOT_ICON_W = 40.0
#: The Snap button's part that opens its menu.
SNAP_MENU_W = 26.0
#: A note's purple row: its padding, and its "Note for the agent" line.
NOTE_PAD_X = 8.0
NOTE_PAD_TOP = 6.0
NOTE_HEAD_H = 15.0
NOTE_PAD_BOTTOM = 7.0
NOTE_HINT = "Note for the agent · not in the document"

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
#: One mouse-wheel notch scrolls the column this far.
WHEEL_STEP = 60.0

#: What each control says when the pointer rests on it.
TOOLTIPS = {
    "snap": "Take a screenshot: {mode}",
    "snap-menu": "Choose what Snap takes",
    "note": "Hold while you talk: it goes to the AI write-up as an instruction, "
            "not into the document",
    "note-off": "Turn on AI write-up on the Walkthrough tab to leave notes for it",
    "undo": "Undo the last screenshot; its words stay",
    "cancel": "Cancel the walkthrough: nothing is kept",
    "done": "Finish the walkthrough",
    "up": "Move up, with its words",
    "down": "Move down, with its words",
    "remove": "Remove the screenshot; its words join the next one's",
}


@dataclass
class Paragraph:
    """What was said between two screenshots: one height (0-1) per tick."""

    ticks: list[float] = field(default_factory=list)
    #: When each tick was written, to grow it in.
    born: list[float] = field(default_factory=list)
    #: Said while Hold for note was held: for the agent, not the document.
    note: bool = False
    #: A note's span in seconds of recording; it ends None while held.
    started_s: float = 0.0
    ended_s: float | None = None


@dataclass
class ShotItem:
    #: The screenshot's number as kept: it never changes, whatever its place.
    number: int
    image: QImage
    born: float
    #: Seconds into the recording when it was kept.
    kept_at_s: float = 0.0


Item = Paragraph | ShotItem


class WalkthroughColumn:
    """What the column shows, without any painting: paragraphs and screenshots.

    ``hear`` takes microphone levels as they arrive; ``advance`` turns the
    loudest of them into a tick every ``TICK_S`` while the user speaks.
    A section is the paragraphs up to and including a screenshot; the last
    one, still being spoken, has no screenshot yet.
    """

    def __init__(self) -> None:
        self.items: list[Item] = [Paragraph()]
        #: Hold for note is held: the voice writes a note.
        self.holding = False
        self.speech_s = 0.0
        self._peak = 0.0
        self._carry = 0.0

    @property
    def shots(self) -> int:
        return sum(1 for item in self.items if isinstance(item, ShotItem))

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

    def add_shot(self, number: int, image: QImage, now: float, kept_at_s: float = 0.0) -> None:
        last = self.items[-1]
        if isinstance(last, Paragraph) and not last.ticks:
            # Nothing was said since the last screenshot: no empty paragraph.
            self.items.pop()
        elif isinstance(last, Paragraph) and last.note and self.holding:
            last.ended_s = kept_at_s
        self.items.append(ShotItem(number, image, now, kept_at_s))
        # A note still held goes on after the screenshot.
        self.items.append(Paragraph(note=self.holding, started_s=kept_at_s))

    def hold_note(self, on: bool, at_s: float) -> None:
        """Hold for note was pressed (``on``) or let go, ``at_s`` into the recording."""
        if on == self.holding:
            return
        self.holding = on
        last = self.items[-1]
        if on:
            if isinstance(last, Paragraph) and not last.ticks:
                self.items.pop()
            self.items.append(Paragraph(note=True, started_s=at_s))
            return
        last.ended_s = at_s
        if not last.ticks:
            # Held in silence: nothing to show.
            self.items.pop()
        self.items.append(Paragraph())

    def sections(self) -> list[list[Item]]:
        """The items in sections; the last is the one being spoken."""
        sections: list[list[Item]] = [[]]
        for item in self.items:
            sections[-1].append(item)
            if isinstance(item, ShotItem):
                sections.append([])
        return sections

    def place(self, number: int) -> int:
        """Screenshot ``number``'s place on the board, from 1; 0 once it is gone."""
        shots = [item.number for item in self.items if isinstance(item, ShotItem)]
        return shots.index(number) + 1 if number in shots else 0

    def remove_shot(self, number: int) -> bool:
        """Take screenshot ``number`` out; its words open the next section."""
        sections = self.sections()
        index = self.place(number) - 1
        if index < 0:
            return False
        removed = sections.pop(index)
        sections[index][:0] = removed[:-1]
        self._rebuild(sections)
        return True

    def move_shot(self, number: int, step: int) -> bool:
        """Move screenshot ``number`` and its words ``step`` sections (-1 up, 1 down)."""
        sections = self.sections()
        index = self.place(number) - 1
        # The section being spoken stays last.
        if index < 0 or not 0 <= index + step < len(sections) - 1:
            return False
        sections[index], sections[index + step] = sections[index + step], sections[index]
        self._rebuild(sections)
        return True

    def _rebuild(self, sections: list[list[Item]]) -> None:
        items: list[Item] = []
        for item in (item for section in sections for item in section):
            if (isinstance(item, Paragraph) and items and isinstance(items[-1], Paragraph)
                    and not item.note and not items[-1].note):
                # Words that now meet run on as one paragraph; notes stay apart.
                items[-1].ticks += item.ticks
                items[-1].born += item.born
            else:
                items.append(item)
        if not items or not isinstance(items[-1], Paragraph):
            items.append(Paragraph())
        self.items = items

    def _paragraph(self) -> Paragraph:
        return self.items[-1]  # every change ends the items on a paragraph


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t: float) -> float:
    t = min(1.0, max(0.0, t))
    c = 1.4
    return 1.0 + (c + 1.0) * (t - 1.0) ** 3 + c * (t - 1.0) ** 2


def _per_row(width: float) -> int:
    return max(1, int((width + TICK_STEP - TICK_W) // TICK_STEP))


def _rows(paragraph: Paragraph, width: float) -> int:
    return math.ceil(len(paragraph.ticks) / _per_row(width))


class WalkthroughPanel(QWidget):
    """The live column for the walkthrough being recorded; reused for the next."""

    RECORDING, WRITING, DONE, HIDDEN = "recording", "writing", "done", "hidden"

    #: The footer's buttons, while it records (Cancel also while it is written).
    snap_requested = pyqtSignal()
    #: A pick from the Snap menu: a WalkthroughSnapMode, to take now and repeat.
    snap_mode_chosen = pyqtSignal(str)
    #: Hold for note pressed (True) or let go (False).
    note_held = pyqtSignal(bool)
    undo_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    finish_requested = pyqtSignal()
    #: A screenshot's hover tools: its number as kept, and -1 (up) or 1 (down).
    move_requested = pyqtSignal(int, int)
    remove_requested = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("walkthroughPanel")
        self.setWindowTitle("Walkthrough")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Its buttons explain themselves though the column is never active.
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform == "darwin":
            # The user works in another app while this column records.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
            # A click on a button leaves that app in front, as on Windows.
            macos.make_nonactivating(self)
        self.setMouseTracking(True)
        self._drag = DragToMove(self)
        self.state = self.HIDDEN
        self.column = WalkthroughColumn()
        self._clock = QElapsedTimer()
        self._recorded_s = 0.0
        self._summary = ""
        self._state_at = 0.0
        self._last = 0.0
        self._scroll = 0.0
        # How far the user scrolled back from the newest, in pixels.
        self._scrolled_back = 0.0
        self._opacity = 0.0
        # The control under the pointer and the one being pressed: "snap",
        # "undo", "cancel", "done", or a screenshot's "up:N", "down:N",
        # "remove:N".
        self._hover = ""
        self._pressed = ""
        # The screenshot under the pointer (its number as kept), or 0.
        self._hover_shot = 0
        # Where the last frame drew each screenshot, by number as kept.
        self._shot_rects: dict[int, QRectF] = {}
        # What Snap takes, the Screenshot shortcut's keys, and the seconds
        # left before an "In 3 seconds" snap (0: none counting down).
        self.snap_mode = WalkthroughSnapMode.REGION
        self._snap_shortcut = ""
        # Whether a note has anyone to read it: the AI write-up is on.
        self._notes_on = True
        self._countdown = 0
        self._menu = None
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)
        self._frame.timeout.connect(self._advance)

    # ---- what the walkthrough does ----

    def begin(self) -> None:
        """A walkthrough started recording: an empty column on the pointer's screen."""
        self.column = WalkthroughColumn()
        self._scroll = self._scrolled_back = 0.0
        self._summary = ""
        self._recorded_s = 0.0
        self._hover = self._pressed = ""
        self._hover_shot = 0
        self._shot_rects = {}
        self._countdown = 0
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

    def add_shot(self, number: int, image: QImage, kept_at_s: float = 0.0) -> None:
        if self.state == self.RECORDING and not image.isNull():
            # A thumbnail is all the column draws; a full screen is many MB.
            small = image.scaledToWidth(THUMB_PX, Qt.TransformationMode.SmoothTransformation)
            self.column.add_shot(number, small, self._now(), kept_at_s)
            # A new screenshot brings the newest back into view.
            self._scrolled_back = 0.0

    def remove_shot(self, number: int) -> None:
        """Screenshot ``number`` (as kept) left the walkthrough; its words stay."""
        if self.column.remove_shot(number):
            self._settle_hover()

    def move_shot(self, number: int, step: int) -> None:
        """Screenshot ``number`` (as kept) moved ``step`` sections, with its words."""
        if self.column.move_shot(number, step):
            self._settle_hover()

    def set_snap_mode(self, mode: str, shortcut: str | None = None) -> None:
        """What Snap takes, and the keys of the Screenshot shortcut, which repeats it."""
        self.snap_mode = mode
        if shortcut is not None:
            self._snap_shortcut = shortcut
        self.update()

    def set_countdown(self, seconds: int) -> None:
        """Seconds until an "In 3 seconds" snap, shown on Snap; 0 when none is coming."""
        self._countdown = seconds
        self.update()

    def set_notes_on(self, on: bool) -> None:
        """Whether Hold for note works: only an AI write-up reads notes."""
        self._notes_on = on
        self.update()

    def snap_modes(self) -> tuple[str, ...]:
        """The kinds of screenshot this desktop can take."""
        return tuple(mode for mode in WalkthroughSnapMode.ALL
                     if mode != WalkthroughSnapMode.WINDOW or pointed.supported())

    def finish(self) -> None:
        """The recording stopped: close the ticks into lines while it is written."""
        self._close_menu()
        self._countdown = 0
        if self.state != self.RECORDING:
            return
        self._hold(False)
        self._recorded_s = self._recorded()
        self._hover_shot = 0
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
        self._close_menu()
        self._hold(False)
        if self.state in (self.RECORDING, self.WRITING, self.DONE):
            self._summary = ""
            self._enter(self.DONE)
            # Start the fade now instead of after the hold.
            self._state_at -= HOLD_S

    # ---- frames ----

    def _now(self) -> float:
        return time.monotonic()

    def _recorded(self) -> float:
        """Seconds recorded so far."""
        if self.state != self.RECORDING:
            return self._recorded_s
        return self._clock.elapsed() / 1000.0

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

    # ---- the pointer ----

    def _controls(self) -> dict[str, QRectF]:
        """Every control that takes a click now, by key, where it was last drawn."""
        controls = {key: rect for key, (rect, enabled) in self._footer().items() if enabled}
        if self.state == self.RECORDING and self._hover_shot in self._shot_rects:
            for key, (rect, enabled) in self._shot_tools(self._hover_shot).items():
                if enabled:
                    controls[key] = rect
        return controls

    def _hit(self, position: QPointF) -> str:
        for key, rect in self._controls().items():
            if rect.contains(position):
                return key
        return ""

    def _set_hover(self, position: QPointF | None) -> None:
        shot = 0
        if position is not None and self.state == self.RECORDING:
            # The tools sit inside their screenshot, so hovering them keeps it.
            shot = next((number for number, rect in self._shot_rects.items()
                         if rect.adjusted(-4, -4, 4, 4).contains(position)), 0)
        if shot != self._hover_shot:
            self._hover_shot = shot
        key = self._hit(position) if position is not None else ""
        if key != self._hover:
            self._hover = key
            self.setCursor(Qt.CursorShape.PointingHandCursor if key
                           else Qt.CursorShape.OpenHandCursor)
        self.update()

    def _settle_hover(self) -> None:
        """The board changed under the pointer: find what it is over once redrawn."""
        self._shot_rects = {}
        self._hover_shot = 0
        self._hover = ""
        self.repaint()
        self._set_hover(self.mapFromGlobal(QCursor.pos()).toPointF())

    def mousePressEvent(self, event) -> None:
        key = self._hit(event.position()) if event.button() == Qt.MouseButton.LeftButton else ""
        if key != "snap-menu":
            # Any other click in the column puts the menu away.
            self._close_menu()
        if key:
            self._pressed = key
            if key == "note":
                self._hold(True)
            event.accept()
            self.update()
            return
        if not self._drag.press(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag.move(event):
            return
        if self.column.holding and not event.buttons() & Qt.MouseButton.LeftButton:
            # The button came up somewhere this window never heard of.
            self._pressed = ""
            self._hold(False)
        self._set_hover(event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, ""
        if pressed == "note":
            # Let go anywhere: the note ends.
            self._hold(False)
        elif pressed and self._hit(event.position()) == pressed:
            self._click(pressed)
        if pressed:
            event.accept()
            self.update()
            return
        if not self._drag.release(event):
            super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(None)
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        notches = event.angleDelta().y() / 120.0
        self._scrolled_back = max(0.0, self._scrolled_back + notches * WHEEL_STEP)
        event.accept()
        self.update()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            key = self._hit(QPointF(event.pos())).split(":")[0]
            note = self._footer()["note"][0]
            if not key and not self._notes_on and note.contains(QPointF(event.pos())):
                key = "note-off"
            text = TOOLTIPS.get(key, "").format(
                mode=WalkthroughSnapMode.LABELS[self.snap_mode].lower())
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def _click(self, key: str) -> None:
        name, _, number = key.partition(":")
        if name == "up":
            self.move_requested.emit(int(number), -1)
        elif name == "down":
            self.move_requested.emit(int(number), 1)
        elif name == "remove":
            self.remove_requested.emit(int(number))
        elif name == "snap-menu":
            if self._menu is None:
                self._open_menu()
            else:
                self._close_menu()
        else:
            {
                "snap": self.snap_requested,
                "undo": self.undo_requested,
                "cancel": self.cancel_requested,
                "done": self.finish_requested,
            }[name].emit()

    def _hold(self, on: bool) -> None:
        """Start or end a note; the walkthrough hears of it through ``note_held``."""
        if on == self.column.holding or (on and self.state != self.RECORDING):
            return
        self.column.hold_note(on, self._recorded())
        self.note_held.emit(on)
        self.update()

    def _open_menu(self) -> None:
        from ui_qt.widgets.walkthrough_snap_menu import SnapMenu

        menu = self._menu = SnapMenu(self.snap_modes(), self.snap_mode, self._snap_shortcut)
        menu.chosen.connect(self.snap_mode_chosen)
        menu.destroyed.connect(self._menu_gone)
        card = self._card().toRect().translated(self.mapToGlobal(self.rect().topLeft()))
        snap = self._footer()["snap"][0]
        menu.open_beside(card, self.mapToGlobal(snap.bottomLeft().toPoint()).y(),
                         self.frameGeometry())

    def _menu_gone(self) -> None:
        self._menu = None

    def _close_menu(self) -> None:
        if self._menu is not None:
            self._menu.close()

    # ---- painting ----

    def _card(self) -> QRectF:
        return QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)

    def _inner(self) -> QRectF:
        return self._card().adjusted(PADDING, PADDING, -PADDING, -PADDING)

    def _footer_top(self) -> float:
        """Where the footer's buttons begin; the rule above them is 12 px higher."""
        return self._inner().bottom() - 2 * FOOT_H - FOOT_GAP

    def _cancel_widening(self) -> float:
        """0 while it records; then Cancel grows to fill its row, the others gone."""
        if self.state == self.WRITING:
            return ease_out(self._since() / FADE_S)
        return 0.0 if self.state == self.RECORDING else 1.0

    def _footer(self) -> dict[str, tuple[QRectF, bool]]:
        """Each footer button's place, and whether it takes a click now.

        While the walkthrough is written only Cancel is left, filling its row.
        """
        inner = self._inner()
        top = self._footer_top()
        bottom = top + FOOT_H + FOOT_GAP
        recording = self.state == self.RECORDING
        half = (inner.width() - FOOT_GAP) / 2
        snap = QRectF(inner.left(), top, half - SNAP_MENU_W, FOOT_H)
        snap_menu = QRectF(snap.right(), top, SNAP_MENU_W, FOOT_H)
        note = QRectF(snap_menu.right() + FOOT_GAP, top, half, FOOT_H)
        undo = QRectF(inner.left(), bottom, FOOT_ICON_W, FOOT_H)
        cancel = QRectF(undo.right() + FOOT_GAP, bottom, FOOT_ICON_W, FOOT_H)
        done = QRectF(cancel.right() + FOOT_GAP, bottom, inner.right() - cancel.right() - FOOT_GAP,
                      FOOT_H)
        widen = self._cancel_widening()
        if widen:
            cancel = QRectF(cancel.left() + (inner.left() - cancel.left()) * widen, bottom,
                            FOOT_ICON_W + (inner.width() - FOOT_ICON_W) * widen, FOOT_H)
        return {
            "snap": (snap, recording),
            "snap-menu": (snap_menu, recording),
            "note": (note, recording and self._notes_on),
            "undo": (undo, recording and self.column.shots > 0),
            "cancel": (cancel, self.state in (self.RECORDING, self.WRITING)),
            "done": (done, recording),
        }

    def _shot_tools(self, number: int) -> dict[str, tuple[QRectF, bool]]:
        """A screenshot's hover tools at its top right, and whether each can act."""
        rect = self._shot_rects[number]
        place, count = self.column.place(number), self.column.shots
        right = rect.right() - 8
        tools = {}
        for name, enabled in (("remove", True), ("down", place < count), ("up", place > 1)):
            tools[f"{name}:{number}"] = (QRectF(right - TOOL, rect.top() + 8, TOOL, TOOL), enabled)
            right -= TOOL + TOOL_GAP
        return tools

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

        inner = self._inner()
        top = self._paint_header(painter, inner)
        foot = self._footer_top() - 12
        body = QRectF(inner.left(), top, inner.width(), foot - 14 - top)
        self._paint_body(painter, body)
        painter.setPen(QPen(token_color("border"), 1.0))
        painter.drawLine(QPointF(inner.left(), foot), QPointF(inner.right(), foot))
        self._paint_footer(painter)
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
            text = f"Recording  {clock(self._recorded())}"
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

    def _slot_ticks_width(self, width: float) -> float:
        """How wide the open slot's rows of ticks are, inside a body ``width`` wide."""
        return width - 2 * SLOT_PAD_X - SLOT_BADGE - 6 - 6 - SLOT_TIME_W

    def _layout(self, width: float) -> list[tuple[str, Item | list[Item], float, float]]:
        """Each block with its top and height, from the body's top.

        A block is ``("words", Paragraph)``, ``("note", Paragraph)``,
        ``("shot", ShotItem)`` or, while it records, ``("slot", [items])`` for
        the section being spoken.
        """
        laid = []
        y = 0.0
        sections = self.column.sections()
        open_section = sections.pop() if self.state == self.RECORDING else []
        for item in (item for section in sections for item in section):
            if isinstance(item, Paragraph) and item.note:
                height = (NOTE_PAD_TOP + NOTE_HEAD_H + 3 + max(1, _rows(item, width - 2 * NOTE_PAD_X))
                          * ROW_PITCH + NOTE_PAD_BOTTOM)
                laid.append(("note", item, y, height))
            elif isinstance(item, Paragraph):
                if not item.ticks:
                    continue
                height = _rows(item, width) * ROW_PITCH
                laid.append(("words", item, y, height))
            else:
                height = self._shot_size(item, width)[1]
                laid.append(("shot", item, y, height))
            y += height + GAP
        if self.state == self.RECORDING:
            ticks = self._slot_ticks_width(width)
            rows = sum(max(1, _rows(item, ticks)) for item in open_section)
            height = (SLOT_PAD_TOP + max(SLOT_BADGE, rows * ROW_PITCH) + 6 + SLOT_HINT_H
                      + SLOT_PAD_BOTTOM)
            laid.append(("slot", open_section, y, height))
        return laid

    def _shot_size(self, shot: ShotItem, width: float) -> tuple[float, float]:
        ratio = shot.image.height() / max(1, shot.image.width())
        h = min(SHOT_MAX_H, width * ratio)
        return h / ratio, h

    def _paint_body(self, painter: QPainter, body: QRectF) -> None:
        laid = self._layout(body.width())
        content_h = laid[-1][2] + laid[-1][3] if laid else 0.0
        # Keep the newest at the bottom in view, gliding there, unless the
        # user scrolled back.
        newest = max(0.0, content_h - body.height())
        self._scrolled_back = min(self._scrolled_back, newest)
        target = newest - self._scrolled_back
        self._scroll += (target - self._scroll) * min(1.0, FRAME_MS / 1000.0 / 0.18)
        painter.save()
        clip = QPainterPath()
        clip.addRect(body.adjusted(-SHADOW, 0, SHADOW, 0))
        painter.setClipPath(clip)
        now = self._now()
        morph = ease_out(self._since() / MORPH_S) if self.state in (self.WRITING, self.DONE) else 0.0
        self._shot_rects = {}
        for kind, item, top, height in laid:
            y = body.top() + top - self._scroll
            if y + height < body.top() - 20 or y > body.bottom() + 20:
                continue
            if kind == "words":
                self._paint_paragraph(painter, item, body.left(), y, body.width(), now, morph)
            elif kind == "note":
                self._paint_note(painter, item, QRectF(body.left(), y, body.width(), height), now,
                                 morph)
            elif kind == "shot":
                self._paint_shot(painter, item, body.left(), y, body.width(), now)
            else:
                self._paint_slot(painter, item, QRectF(body.left(), y, body.width(), height), now)
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

    def _paint_ticks(self, painter: QPainter, paragraph: Paragraph, left: float, top: float,
                     width: float, now: float, morph: float, color: QColor) -> None:
        per_row = _per_row(width)
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

    def _paint_paragraph(self, painter: QPainter, paragraph: Paragraph, left: float,
                         top: float, width: float, now: float, morph: float) -> None:
        self._paint_ticks(painter, paragraph, left, top, width, now, morph,
                          token_color("text-soft"))

    def _note_length(self, note: Paragraph) -> float:
        end = note.ended_s if note.ended_s is not None else self._recorded()
        return max(0.0, end - note.started_s)

    def _paint_note(self, painter: QPainter, note: Paragraph, rect: QRectF, now: float,
                    morph: float) -> None:
        """A note for the agent: a purple row that says it stays out of the document."""
        painter.save()
        painter.setPen(QPen(token_color("purple", 102), 1.0))
        painter.setBrush(token_color("purple", 36))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        ink = token_color("purple-text")
        head = QRectF(rect.left() + NOTE_PAD_X, rect.top() + NOTE_PAD_TOP,
                      rect.width() - 2 * NOTE_PAD_X, NOTE_HEAD_H)
        paint_icon(painter, "note", QPointF(head.left() + 6, head.center().y()), 12, ink)
        font = QFont(self.font())
        # 10 px, so all of it fits the column: "not in the document" is the point.
        font.setPixelSize(10)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(ink)
        text = f"Note for the agent · {clock(self._note_length(note))} · not in the document"
        painter.drawText(head.adjusted(18, 0, 0, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight,
                                                          int(head.width() - 18)))
        self._paint_ticks(painter, note, head.left(), head.bottom() + 3, head.width(), now, morph,
                          ink)
        painter.restore()

    def _paint_caret(self, painter: QPainter, x: float, centre: float) -> None:
        blink = 0.5 + 0.5 * math.cos(self._clock.elapsed() / 1000.0 * math.tau / 1.1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(token_color("accent", int(90 + 165 * blink)))
        painter.drawRoundedRect(QRectF(x, centre - 7, 2, 14), 1, 1)

    def _section_started(self) -> float:
        """When the section being spoken began: the newest screenshot still kept."""
        return max((item.kept_at_s for item in self.column.items if isinstance(item, ShotItem)),
                   default=0.0)

    def _paint_slot(self, painter: QPainter, items: list[Item], rect: QRectF, now: float) -> None:
        """The section being spoken: an open, dashed slot the next screenshot closes."""
        painter.save()
        pen = QPen(token_color("accent-tint-border"), 1.5, Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([3.0, 2.5])
        painter.setPen(pen)
        painter.setBrush(token_color("accent-tint"))
        painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 10, 10)

        top = rect.top() + SLOT_PAD_TOP
        badge = QRectF(rect.left() + SLOT_PAD_X, top, SLOT_BADGE, SLOT_BADGE)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(token_color("accent"))
        painter.drawRoundedRect(badge, 6, 6)
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(token_color("on-accent"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(self.column.shots + 1))

        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(token_color("accent-soft"))
        elapsed = max(0.0, self._recorded() - self._section_started())
        time_rect = QRectF(rect.right() - SLOT_PAD_X - SLOT_TIME_W, top, SLOT_TIME_W, SLOT_BADGE)
        painter.drawText(time_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         clock(elapsed))

        left = badge.right() + 6
        width = self._slot_ticks_width(rect.width())
        # A single row of ticks sits level with the badge.
        y = top + (SLOT_BADGE - ROW_PITCH) / 2
        caret = (left + 2, y + ROW_PITCH / 2)
        for item in items:
            rows = max(1, _rows(item, width))
            ink = token_color("purple-text" if item.note else "text")
            self._paint_ticks(painter, item, left, y, width, now, 0.0, ink)
            count = len(item.ticks)
            row, col = divmod(count, _per_row(width))
            if count and col == 0:
                # A full row: the caret waits at its end, not on a row of its own.
                row, col = row - 1, _per_row(width)
            caret = (left + col * TICK_STEP + 2, y + row * ROW_PITCH + ROW_PITCH / 2)
            y += rows * ROW_PITCH
        self._paint_caret(painter, *caret)

        hint = QFont(self.font())
        hint.setPixelSize(12)
        painter.setFont(hint)
        holding = self.column.holding
        painter.setPen(token_color("purple-text" if holding else "accent-soft"))
        hint_top = rect.bottom() - SLOT_PAD_BOTTOM - SLOT_HINT_H
        painter.drawText(QRectF(rect.left() + SLOT_PAD_X, hint_top, rect.width() - 2 * SLOT_PAD_X,
                                SLOT_HINT_H),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         NOTE_HINT if holding else SLOT_HINT)
        painter.restore()

    def _paint_shot(self, painter: QPainter, shot: ShotItem, left: float, top: float,
                    width: float, now: float) -> None:
        w, h = self._shot_size(shot, width)
        t = (now - shot.born) / SHOT_IN_S
        scale = 0.82 + 0.18 * ease_out_back(t)
        rect = QRectF(left + (width - w) / 2, top, w, h)
        self._shot_rects[shot.number] = rect
        centre = rect.center()
        hovered = self._hover_shot == shot.number
        painter.save()
        painter.setOpacity(painter.opacity() * ease_out(t * 1.6))
        painter.translate(centre.x() + 24 * (1 - ease_out(t)), centre.y())
        painter.scale(scale, scale)
        painter.translate(-centre.x(), -centre.y())
        painter.setPen(Qt.PenStyle.NoPen)
        for i in (6, 4, 2):
            painter.setBrush(QColor(0, 0, 0, 16))
            painter.drawRoundedRect(rect.adjusted(-i / 2, -i / 2 + 3, i / 2, i / 2 + 3), 6 + i, 6 + i)
        if hovered:
            painter.setPen(QPen(token_color("accent"), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 8, 8)
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(rect, 6, 6)
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, shot.image)
        painter.restore()
        self._paint_badge(painter, shot, rect)
        if hovered and self.state == self.RECORDING:
            self._paint_shot_tools(painter, shot.number)
        painter.restore()

    def _paint_badge(self, painter: QPainter, shot: ShotItem, rect: QRectF) -> None:
        """The screenshot's place on the board and when it was kept: "2 · 0:35"."""
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        text = f"{self.column.place(shot.number)} · {clock(shot.kept_at_s)}"
        width = painter.fontMetrics().horizontalAdvance(text) + 16
        badge = QRectF(rect.left() + 8, rect.top() + 8, width, BADGE_H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ON_IMAGE)
        painter.drawRoundedRect(badge, BADGE_H / 2, BADGE_H / 2)
        painter.setPen(ON_IMAGE_TEXT)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_shot_tools(self, painter: QPainter, number: int) -> None:
        for key, (rect, enabled) in self._shot_tools(number).items():
            name = key.split(":")[0]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ON_IMAGE_HOVER if self._hover == key else ON_IMAGE)
            painter.drawRoundedRect(rect, 8, 8)
            ink = QColor(token_color("danger-text-soft") if name == "remove" else ON_IMAGE_TEXT)
            if not enabled:
                ink.setAlphaF(0.3)
            paint_icon(painter, "x" if name == "remove" else name, rect.center(), 14, ink)

    def _paint_footer(self, painter: QPainter) -> None:
        # Once it records no more, the buttons fade, all but Cancel, which
        # stays while it is written.
        fade = 1.0 - ease_out(self._since() / FADE_S) if self.state != self.RECORDING else 1.0
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        for key, (rect, enabled) in self._footer().items():
            if self.state == self.RECORDING:
                alpha = 1.0 if enabled else 0.4
            else:
                alpha = 1.0 if enabled else fade
            if alpha <= 0.0:
                continue
            painter.save()
            painter.setOpacity(painter.opacity() * alpha)
            self._paint_button(painter, key, rect)
            painter.restore()

    def _paint_button(self, painter: QPainter, key: str, rect: QRectF) -> None:
        hover = self._hover == key
        pressed = self._pressed == key and hover
        if key in ("snap", "snap-menu"):
            # One rounded button in two parts: Snap, then the menu's chevron.
            footer = self._footer()
            whole = footer["snap"][0].united(footer["snap-menu"][0])
            if key == "snap":
                fill = "accent-pressed" if pressed else "accent-hover" if hover else "accent"
            else:
                fill = "accent-hover" if pressed or hover or self._menu is not None \
                    else "accent-pressed"
            painter.save()
            painter.setClipRect(rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color(fill))
            painter.drawRoundedRect(whole, 10, 10)
            painter.restore()
            ink = token_color("on-accent")
            if key == "snap-menu":
                painter.setPen(QPen(token_color("on-accent", 77), 1.0))
                painter.drawLine(QPointF(rect.left() + 0.5, rect.top()),
                                 QPointF(rect.left() + 0.5, rect.bottom()))
                paint_icon(painter, "up", rect.center(), 12, ink)
            elif self._countdown:
                self._paint_label(painter, rect, "camera", f"In {self._countdown}…", ink)
            else:
                self._paint_label(painter, rect, "camera", "Snap", ink)
            return
        fill = token_color("border-hover" if pressed else "surface-active" if hover else "surface-hover")
        painter.setPen(QPen(token_color("border-strong"), 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        if key == "note":
            if self.column.holding:
                painter.setPen(QPen(token_color("purple"), 1.0))
                painter.setBrush(token_color("purple", 70))
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
            self._paint_label(painter, rect, "note", "Hold for note", token_color("purple-text"))
        elif key == "undo":
            paint_icon(painter, "undo", rect.center(), 16, token_color("text"))
        elif key == "cancel":
            ink = token_color("danger-text-soft")
            if rect.width() > FOOT_ICON_W * 2.5:
                self._paint_label(painter, rect, "x", "Cancel", ink)
            else:
                paint_icon(painter, "x", rect.center(), 16, ink)
        elif key == "done":
            self._paint_label(painter, rect, "", "Done", token_color("success"))

    def _paint_label(self, painter: QPainter, rect: QRectF, icon: str, text: str,
                     color: QColor) -> None:
        """``text`` centred in ``rect``, after ``icon`` when there is one."""
        metrics = painter.fontMetrics()
        icon_w = 16.0 + 6.0 if icon else 0.0
        width = icon_w + metrics.horizontalAdvance(text)
        x = rect.center().x() - width / 2
        if icon:
            paint_icon(painter, icon, QPointF(x + 8, rect.center().y()), 16, color)
        painter.setPen(color)
        painter.drawText(QRectF(x + icon_w, rect.top(), width - icon_w + 2, rect.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


def make_panel() -> WalkthroughPanel | None:
    """A column, or None where the compositor places windows (Wayland)."""
    if compositor_managed():
        return None
    return WalkthroughPanel()
