"""The full-screen freeze-and-annotate overlay.

It shows the frozen screen under a smoked layer. The user drags out a
region, which shows through clear with its size beside it, then marks it up
with the toolbar that appears under it. Enter keeps, Esc drops. The overlay
emits ``kept`` (the annotated crop, device pixels) or ``dropped`` exactly
once; whoever opened it closes and deletes it.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from functools import partial

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QPainter,
    QPalette,
    QRegion,
    QScreen,
)
from PyQt6.QtWidgets import QLineEdit, QWidget

from ui_qt.snap import affinity, chrome, export, support
from ui_qt.snap.annotations import (
    LABEL_PAD_X,
    LABEL_PAD_Y,
    SWATCHES,
    Annotation,
    AnnotationStack,
    Label,
    Step,
    Tool,
    ink_for,
    new_draft,
)
from ui_qt.snap.geometry import badge_rect, device_rect, place_toolbar, span
from ui_qt.snap.toolbar import SHADOW, SnapToolbar
from ui_qt.utils.desktop import is_wayland
from ui_qt.utils.palette import current_palette

#: A drag smaller than this either way is a click, not a selection.
MIN_SELECTION = 6
_HINT_EDGE = 14
#: The same marker the Scratchpad sets: app hotkeys behave as they do in our
#: other windows while the overlay has focus (see services/runtime/hotkeys.py).
_HOTKEY_WINDOW_PROPERTY = "openwhisperHotkeyWindow"

_CHORD = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.ShiftModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)
_CTRL = Qt.KeyboardModifier.ControlModifier
_CTRL_SHIFT = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
_TOOL_KEYS = {
    Qt.Key.Key_A: Tool.ARROW,
    Qt.Key.Key_B: Tool.BOX,
    Qt.Key.Key_P: Tool.PEN,
    Qt.Key.Key_H: Tool.HIGHLIGHTER,
    Qt.Key.Key_T: Tool.TEXT,
    Qt.Key.Key_N: Tool.STEP,
}
_COLOR_KEYS = {Qt.Key.Key_1: 0, Qt.Key.Key_2: 1, Qt.Key.Key_3: 2, Qt.Key.Key_4: 3}
_KEEP_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
_TAB_KEYS = (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)


class Phase(Enum):
    SELECT = "select"
    ANNOTATE = "annotate"


def window_flags() -> Qt.WindowType:
    """A frameless top-level window that covers one screen.

    Wayland offers neither stay-on-top nor positioning, so there it is a
    plain toplevel that asks for fullscreen; Hyprland fullscreens it on the
    focused monitor. On Windows it is an always-on-top tool window, which
    keeps it off the taskbar. X11 window managers only fullscreen normal
    windows reliably, so there it stays a normal window kept on top.
    """
    frameless = Qt.WindowType.FramelessWindowHint
    if is_wayland():
        return Qt.WindowType.Window | frameless
    windows = support._platform() == "win32"
    kind = Qt.WindowType.Tool if windows else Qt.WindowType.Window
    return kind | frameless | Qt.WindowType.WindowStaysOnTopHint


def _dimmed(frozen: QImage) -> QImage:
    """The frozen screen under the smoked layer, made once so drags only copy pixels."""
    dimmed = frozen.copy()
    painter = QPainter(dimmed)
    painter.fillRect(QRectF(QPointF(0, 0), dimmed.deviceIndependentSize()), chrome.DIM)
    painter.end()
    return dimmed


class LabelEditor(QLineEdit):
    """The inline editor for a text label, styled as the tag it becomes.

    Enter commits the label and Esc cancels only the label: both are
    swallowed here so they never reach the overlay as keep or drop.
    """

    committed = pyqtSignal()
    cancelled = pyqtSignal()

    _SHEET = (
        "QLineEdit#snapLabelEditor {{ background: {fill}; color: {ink}; border: none;"
        " border-radius: 6px; padding: 3px 6px; font-size: 16px; font-weight: 600;"
        " selection-background-color: rgba({wash}, 0.35); selection-color: {ink}; }}"
    )

    def __init__(self, parent: QWidget, color: QColor) -> None:
        super().__init__(parent)
        self.setObjectName("snapLabelEditor")
        self.setPlaceholderText("Label")
        self.setFrame(False)
        self.setMaxLength(200)
        self._color = QColor(color)
        self.textChanged.connect(self._fit)
        self.set_color(color)

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        ink = ink_for(color)
        wash = "255, 255, 255" if ink.lightness() > 128 else "0, 0, 0"
        self.setStyleSheet(
            self._SHEET.format(fill=color.name(), ink=ink.name(), wash=wash)
        )
        palette = self.palette()
        hint = QColor(ink)
        hint.setAlphaF(0.6)
        palette.setColor(QPalette.ColorRole.PlaceholderText, hint)
        self.setPalette(palette)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in _KEEP_KEYS:
            self.committed.emit()
        elif key == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif key not in _TAB_KEYS:
            super().keyPressEvent(event)
            return
        # Swallowed, Tab included: an ignored key goes on to the overlay,
        # where Esc drops the snap and Tab moves the keyboard off the label.
        event.accept()

    def focusNextPrevChild(self, forward: bool) -> bool:
        # Tab stays here (see keyPressEvent): the overlay treats every letter
        # as a tool shortcut.
        return False

    def _fit(self) -> None:
        self.ensurePolished()
        metrics = self.fontMetrics()
        text = self.text() or self.placeholderText()
        self.resize(
            round(metrics.horizontalAdvance(text) + 2 * LABEL_PAD_X + 8),
            round(metrics.height() + 2 * LABEL_PAD_Y + 2),
        )


class SnapOverlay(QWidget):
    """Freeze, select, annotate: one screen, one snap."""

    kept = pyqtSignal(QImage)
    dropped = pyqtSignal()

    def __init__(
        self, frozen: QImage, screen: QScreen | None = None, *, hint: str = ""
    ) -> None:
        super().__init__(None, window_flags())
        self._frozen = frozen
        self._dimmed = _dimmed(frozen)
        self._canvas = export.logical_rect(frozen)
        self._screen = screen
        self._palette = current_palette()
        base_font = QGuiApplication.font()
        self._hint = chrome.HintPill(hint, base_font, self._palette)
        self._badge_font = chrome.chrome_font(base_font, 11.5, QFont.Weight.DemiBold)
        self._phase = Phase.SELECT
        self._selection: QRect | None = None
        self._origin: QPoint | None = None
        self._stack = AnnotationStack()
        self._draft: Annotation | None = None
        self._tool = Tool.ARROW
        self._color = 0
        self._editor: LabelEditor | None = None
        self._editor_pos = QPointF()
        self._done = False
        self._configure()
        self._toolbar = self._build_toolbar()

    def _configure(self) -> None:
        self.setObjectName("snapOverlay")
        self.setWindowTitle("OpenWhisper Snap")
        # Every pixel is painted from the frozen image: no background to clear.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setProperty(_HOTKEY_WINDOW_PROPERTY, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.resize(self._canvas.size())

    def _build_toolbar(self) -> SnapToolbar:
        toolbar = SnapToolbar(self, self._palette)
        toolbar.hide()
        toolbar.tool_chosen.connect(self.set_tool)
        toolbar.color_chosen.connect(self.set_color)
        toolbar.undo_clicked.connect(self.undo)
        toolbar.redo_clicked.connect(self.redo)
        toolbar.drop_clicked.connect(self.drop)
        toolbar.keep_clicked.connect(self.keep)
        return toolbar

    # ---- what callers and tests read ----

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def selection(self) -> QRect | None:
        return QRect(self._selection) if self._selection is not None else None

    @property
    def annotations(self) -> AnnotationStack:
        return self._stack

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def color_index(self) -> int:
        return self._color

    @property
    def label_editor(self) -> LabelEditor | None:
        return self._editor

    @property
    def toolbar(self) -> SnapToolbar:
        return self._toolbar

    def current_color(self) -> QColor:
        return QColor(SWATCHES[self._color].color)

    # ---- showing, keeping, dropping ----

    def present(self) -> None:
        """Cover the captured screen, focused so that Enter and Esc reach us."""
        if self._screen is not None:
            self.setScreen(self._screen)
            self.setGeometry(self._screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        affinity.bring_to_front(self)

    def keep(self) -> None:
        """Keep the selection (the whole screen when there is none) with its marks."""
        if self._done:
            return
        if self._editor is not None:
            self._commit_label()
        if self._draft is not None:
            self._finish_draft()
        selection = self._selection if self._selection is not None else self._canvas
        image = export.render(self._frozen, selection, self._stack.items)
        self._done = True
        self.kept.emit(image)

    def drop(self) -> None:
        if self._done:
            return
        self._done = True
        self.dropped.emit()

    def _select_all(self) -> None:
        """Select the whole screen and start annotating it."""
        self._origin = None
        self._set_selection(QRect(self._canvas))
        self._enter_annotate()

    def closeEvent(self, event) -> None:
        # Closed from outside (a compositor keybind, Alt+F4): that is a cancel.
        self.drop()
        event.accept()

    # ---- tools, colours, history ----

    def set_tool(self, tool: Tool | str) -> None:
        if self._editor is not None:
            self._commit_label()
        self._tool = Tool(tool)
        self._sync_toolbar()

    def set_color(self, index: int) -> None:
        if not 0 <= index < len(SWATCHES):
            return
        self._color = index
        if self._editor is not None:
            self._editor.set_color(self.current_color())
        self._sync_toolbar()

    def undo(self) -> None:
        if self._editor is not None:
            # The label being typed is the newest mark: undo throws it away.
            self._close_label()
            return
        self._after_history(self._stack.undo())

    def redo(self) -> None:
        if self._editor is None:
            self._after_history(self._stack.redo())

    def _after_history(self, item: Annotation | None) -> None:
        if item is not None:
            self._sync_toolbar()
            self._update_local(item.bounds())

    def _sync_toolbar(self) -> None:
        self._toolbar.set_state(
            self._tool, self._color, self._stack.can_undo, self._stack.can_redo
        )

    # ---- keys ----

    def keyPressEvent(self, event: QKeyEvent) -> None:
        action = self._shortcut(event)
        if action is None:
            super().keyPressEvent(event)
            return
        event.accept()
        action()

    def _shortcut(self, event: QKeyEvent) -> Callable[[], None] | None:
        key = event.key()
        modifiers = event.modifiers() & _CHORD
        if modifiers == _CTRL:
            return {Qt.Key.Key_Z: self.undo, Qt.Key.Key_Y: self.redo}.get(key)
        if modifiers == _CTRL_SHIFT:
            return self.redo if key == Qt.Key.Key_Z else None
        if modifiers:
            return None
        if key in _KEEP_KEYS:
            return self.keep
        if key == Qt.Key.Key_Escape:
            return self.drop
        if key in _TOOL_KEYS:
            return partial(self.set_tool, _TOOL_KEYS[key])
        if key in _COLOR_KEYS:
            return partial(self.set_color, _COLOR_KEYS[key])
        return None

    # ---- mouse ----

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._done:
            return
        if self._editor is not None:
            # A click away from the label being typed finishes it.
            self._commit_label()
            return
        point = event.position()
        if self._may_select(point.toPoint()):
            self._start_selection(point.toPoint())
        else:
            self._start_annotation(point)

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        if self._origin is not None:
            self._drag_selection(point.toPoint())
        elif self._draft is not None:
            self._extend_draft(point)
        else:
            self._update_cursor(point.toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._origin is not None:
            self._finish_selection()
        elif self._draft is not None:
            self._finish_draft()

    def mouseDoubleClickEvent(self, event) -> None:
        first_click = self._phase is Phase.SELECT and self._selection is None
        if (
            first_click
            and event.button() == Qt.MouseButton.LeftButton
            and not self._done
        ):
            self._select_all()
            return
        # Two quick clicks while annotating are two clicks (two step badges).
        self.mousePressEvent(event)

    def _update_cursor(self, point: QPoint) -> None:
        typing = (
            self._phase is Phase.ANNOTATE
            and self._tool is Tool.TEXT
            and self._selection is not None
            and self._selection.contains(point)
        )
        shape = Qt.CursorShape.IBeamCursor if typing else Qt.CursorShape.CrossCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    # ---- selecting ----

    def _may_select(self, point: QPoint) -> bool:
        """Drags select at first; later only outside a selection with no marks."""
        if self._phase is Phase.SELECT or self._selection is None:
            return True
        return not len(self._stack) and not self._selection.contains(point)

    def _start_selection(self, point: QPoint) -> None:
        reselecting = self._phase is Phase.ANNOTATE
        self._phase = Phase.SELECT
        self._toolbar.hide()
        self._stack = AnnotationStack()
        self._origin = self._clamp(point)
        self._set_selection(None)
        if reselecting:
            # The hint pill may have moved out of the toolbar's way: repaint it all.
            self.update()

    def _drag_selection(self, point: QPoint) -> None:
        rect = span(self._origin, self._clamp(point))
        big_enough = rect.width() >= MIN_SELECTION and rect.height() >= MIN_SELECTION
        self._set_selection(rect if big_enough else None)

    def _finish_selection(self) -> None:
        self._origin = None
        if self._selection is not None:
            self._enter_annotate()

    def _enter_annotate(self) -> None:
        self._phase = Phase.ANNOTATE
        corner = place_toolbar(
            self._toolbar.panel_size(), self._selection, self._canvas
        )
        self._toolbar.move(corner - QPoint(SHADOW, SHADOW))
        self._sync_toolbar()
        self._toolbar.show()
        self._toolbar.raise_()
        self.update()

    def _clamp(self, point: QPoint) -> QPoint:
        canvas = self._canvas
        return QPoint(
            min(max(point.x(), canvas.left()), canvas.right() + 1),
            min(max(point.y(), canvas.top()), canvas.bottom() + 1),
        )

    def _set_selection(self, rect: QRect | None) -> None:
        dirty = self._decor_region()
        self._selection = rect
        self.update(dirty.united(self._decor_region()))

    def _decor_region(self) -> QRegion:
        """What the selection's look touches: frame, size readout and the hint pill."""
        region = QRegion(self._hint_rect())
        if self._selection is not None:
            region = region.united(QRegion(self._selection.adjusted(-4, -4, 4, 4)))
            region = region.united(QRegion(self._badge_rect().adjusted(-2, -2, 2, 2)))
        return region

    # ---- annotating ----

    def _start_annotation(self, point: QPointF) -> None:
        if self._selection is None or not self._selection.contains(point.toPoint()):
            return
        local = point - QPointF(self._selection.topLeft())
        if self._tool is Tool.TEXT:
            self._open_label(local)
        elif self._tool is Tool.STEP:
            step = Step(
                self._inside(local),
                self._stack.next_step_number(),
                self.current_color(),
            )
            self._add(step)
        else:
            self._draft = new_draft(self._tool, local, self.current_color())
            self._update_local(self._draft.bounds())

    def _extend_draft(self, point: QPointF) -> None:
        before = self._draft.bounds()
        self._draft.extend(point - QPointF(self._selection.topLeft()))
        self._update_local(before.united(self._draft.bounds()))

    def _finish_draft(self) -> None:
        draft, self._draft = self._draft, None
        if draft.meaningful():
            self._add(draft)
        else:
            self._update_local(draft.bounds())

    def _add(self, item: Annotation) -> None:
        self._stack.add(item)
        self._sync_toolbar()
        self._update_local(item.bounds())

    def _inside(self, local: QPointF) -> QPointF:
        return QPointF(
            min(max(local.x(), 0.0), float(self._selection.width())),
            min(max(local.y(), 0.0), float(self._selection.height())),
        )

    def _update_local(self, rect: QRectF) -> None:
        if self._selection is not None:
            shifted = rect.translated(QPointF(self._selection.topLeft()))
            self.update(shifted.toAlignedRect().adjusted(-2, -2, 2, 2))

    # ---- text labels ----

    def _open_label(self, local: QPointF) -> None:
        editor = LabelEditor(self, self.current_color())
        editor.committed.connect(self._commit_label)
        editor.cancelled.connect(self._close_label)
        self._editor_pos = self._label_origin(local, editor.size())
        editor.move((QPointF(self._selection.topLeft()) + self._editor_pos).toPoint())
        self._editor = editor
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)

    def _label_origin(self, local: QPointF, size: QSize) -> QPointF:
        """The label's top-left: centred on the click, inside the selection."""
        room_x = max(0.0, self._selection.width() - size.width())
        room_y = max(0.0, self._selection.height() - size.height())
        return QPointF(
            min(max(local.x(), 0.0), room_x),
            min(max(local.y() - size.height() / 2, 0.0), room_y),
        )

    def _commit_label(self) -> None:
        editor = self._editor
        if editor is None:
            return
        label = Label(
            QPointF(self._editor_pos),
            editor.text().strip(),
            editor.color(),
            QFont(editor.font()),
        )
        self._close_label()
        if label.meaningful():
            self._add(label)

    def _close_label(self) -> None:
        editor, self._editor = self._editor, None
        if editor is None:
            return
        editor.hide()
        editor.deleteLater()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---- painting ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if not self._canvas.contains(self.rect()):
            painter.fillRect(self.rect(), QColor(0, 0, 0))
        painter.drawImage(QPointF(0, 0), self._dimmed)
        if self._selection is not None:
            self._paint_selection(painter)
        hint = self._hint_rect()
        if not hint.isNull():
            self._hint.paint(painter, hint)
        painter.end()

    def _paint_selection(self, painter: QPainter) -> None:
        selection = self._selection
        painter.save()
        painter.setClipRect(selection)
        painter.drawImage(QPointF(0, 0), self._frozen)
        if self._phase is Phase.ANNOTATE:
            self._paint_annotations(painter)
        painter.restore()
        chrome.paint_frame(painter, selection)
        if self._phase is Phase.SELECT:
            chrome.paint_badge(
                painter,
                self._badge_rect(),
                self._badge_text(),
                self._badge_font,
                self._palette,
            )

    def _paint_annotations(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.translate(QPointF(self._selection.topLeft()))
        for item in self._stack.items:
            item.paint(painter)
        if self._draft is not None:
            self._draft.paint(painter)

    def _badge_text(self) -> str:
        crop = device_rect(
            self._selection, self._frozen.devicePixelRatio(), self._frozen.rect()
        )
        return f"{crop.width()} × {crop.height()}"

    def _badge_rect(self) -> QRect:
        size = chrome.badge_size(self._badge_text(), self._badge_font)
        return badge_rect(size, self._selection, self._canvas)

    def _hint_rect(self) -> QRect:
        """Top centre, else bottom centre: never over the selection or toolbar."""
        size = self._hint.size()
        x = self._canvas.center().x() - size.width() // 2
        top = self._canvas.top() + _HINT_EDGE
        bottom = self._canvas.bottom() + 1 - _HINT_EDGE - size.height()
        for y in (top, bottom):
            rect = QRect(QPoint(x, y), size)
            if not self._blocks_hint(rect):
                return rect
        return QRect()

    def _blocks_hint(self, rect: QRect) -> bool:
        blockers = [self._selection] if self._selection is not None else []
        if self._phase is Phase.ANNOTATE and not self._toolbar.isHidden():
            blockers.append(self._toolbar.geometry())
        return any(rect.intersects(other) for other in blockers)
