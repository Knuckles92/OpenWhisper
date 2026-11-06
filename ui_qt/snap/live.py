"""Draw one mark on the live desktop, then give mouse and keyboard input back."""

from __future__ import annotations

import math
from time import monotonic

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QScreen
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import SettingsKey, resolve_snap_to_edges, settings_manager
from ui_qt.snap import affinity, capture, pointed
from ui_qt.snap.annotations import SWATCHES, Annotation, Step, Tool, new_draft
from ui_qt.snap.geometry import EDGE_MARGIN, snap_point
from ui_qt.snap.overlay import (
    _CHORD,
    _COLOR_KEYS,
    _HOTKEY_WINDOW_PROPERTY,
    _TOOL_KEYS,
    LabelEditor,
    label_origin,
)
from ui_qt.snap.toolbar import SnapToolbar
from ui_qt.utils.palette import current_palette

MARK_LIFETIME_S = 1.5
_FLOATING = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
             | Qt.WindowType.WindowStaysOnTopHint)
_NO_FOCUS = Qt.WindowType.WindowDoesNotAcceptFocus
_PASS_THROUGH = _NO_FOCUS | Qt.WindowType.WindowTransparentForInput
_LEFT = Qt.MouseButton.LeftButton
_CTRL = Qt.KeyboardModifier.ControlModifier


class LiveAnnotations(QObject):
    """One toolbar per walkthrough, with temporary drawing input on every screen."""

    arming = pyqtSignal()
    drawn = pyqtSignal(QScreen, object)

    def __init__(self, parent: QObject, panel: QWidget | None = None) -> None:
        super().__init__(parent)
        self._panel = panel
        self._canvases: dict[QScreen, _Canvas] = {}
        self._enabled = False
        self.tool: Tool | None = None
        self.color = 0
        self.snap_enabled: bool
        self._previous_focus = None
        self._screen = capture.screen_under_cursor()
        self._toolbar = SnapToolbar(None, current_palette(), live=True,
                                    flags=_FLOATING | _NO_FOCUS)
        self._toolbar.setWindowTitle("OpenWhisper Walkthrough Drawing")
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._toolbar.tool_chosen.connect(self.arm)
        self._toolbar.color_chosen.connect(self.set_color)
        self._toolbar.snap_clicked.connect(self.toggle_snap)
        self._toolbar.drop_clicked.connect(self.disarm)
        self._toolbar.clear_clicked.connect(self.clear)
        QGuiApplication.instance().screenRemoved.connect(self._remove_screen)
        self._sync_toolbar()
        self.set_enabled(True)

    @property
    def armed(self) -> bool:
        return self.tool is not None

    def windows(self) -> tuple[QWidget, ...]:
        return (self._toolbar, *self._canvases.values())

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self.snap_enabled = resolve_snap_to_edges()
            self._toolbar.set_snap_enabled(self.snap_enabled)
            for canvas in self._canvases.values():
                canvas.present()
            self._show_toolbar()
        else:
            self.disarm()
            self._toolbar.hide()
            for canvas in self._canvases.values():
                canvas.hide()

    def _show_toolbar(self) -> None:
        if self._screen is None:
            self._screen = capture.screen_under_cursor()
        if self._screen is None:
            return
        area = self._screen.geometry()
        self._toolbar.setScreen(self._screen)
        self._toolbar.move(area.x() + max(0, (area.width() - self._toolbar.width()) // 2),
                           area.y() + EDGE_MARGIN)
        self._toolbar.show()
        if not affinity.is_kept_out(self._toolbar):
            affinity.keep_out_of_screenshots(self._toolbar, True)
        self._toolbar.raise_()

    def arm(self, tool: Tool | str = Tool.PEN) -> None:
        if not self._enabled or not self._toolbar.isEnabled():
            return
        screens = QGuiApplication.screens()
        if not screens:
            return
        if not self.armed:
            self.arming.emit()
            self._previous_focus = affinity.foreground_window()
            for screen in screens:
                if screen not in self._canvases:
                    self._canvases[screen] = _Canvas(self, screen)
            # Read the edges before any of the drawing surfaces take focus.
            exclude = self.windows() + ((self._panel,) if self._panel is not None else ())
            for screen, canvas in self._canvases.items():
                canvas.targets = (canvas.rect(), *pointed.window_targets(screen, exclude))
        elif self.tool != tool:
            for canvas in self._canvases.values():
                canvas.cancel_draft()
        self.tool = Tool(tool)
        for canvas in self._canvases.values():
            canvas.present()
        canvas = self._canvases.get(capture.screen_under_cursor())
        if canvas is not None:
            canvas.activateWindow()
            canvas.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            affinity.bring_to_front(canvas)
        self._raise_controls()
        self._sync_toolbar()

    def disarm(self) -> None:
        if not self.armed:
            return
        affinity.return_foreground(self._previous_focus, list(self._canvases.values()))
        self._previous_focus = None
        self.tool = None
        for canvas in self._canvases.values():
            canvas.cancel_draft()
            if self._enabled:
                canvas.present()
        self._sync_toolbar()
        self._raise_controls()

    def _raise_controls(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.raise_()
        self._toolbar.raise_()

    def set_color(self, color: int) -> None:
        self.color = color
        for canvas in self._canvases.values():
            if canvas.label_editor is not None:
                canvas.label_editor.set_color(QColor(SWATCHES[color].color))
        self._sync_toolbar()

    def toggle_snap(self) -> None:
        self.snap_enabled = not self.snap_enabled
        self._toolbar.set_snap_enabled(self.snap_enabled)
        settings_manager.save_setting(SettingsKey.SNAP_TO_EDGES, self.snap_enabled)

    def set_busy(self, busy: bool) -> None:
        self._toolbar.setEnabled(not busy)

    def _sync_toolbar(self) -> None:
        self._toolbar.set_state(self.tool, self.color, False, False)

    def check_focus(self) -> None:
        if self.armed and QApplication.activeWindow() not in self._canvases.values():
            self.disarm()

    def complete(self, canvas: _Canvas, mark: Annotation, pinned: bool) -> None:
        canvas.add(mark, pinned)
        marks = canvas.items()
        self.disarm()
        self.drawn.emit(canvas.screen, marks)

    def pinned_at(self, position: tuple[int, int]) -> tuple[Annotation, ...]:
        screen = capture.screen_at_windows_point(position)
        canvas = self._canvases.get(screen)
        return canvas.items(pinned_only=True) if canvas is not None else ()

    def clear(self) -> None:
        for canvas in self._canvases.values():
            canvas.clear()

    def _remove_screen(self, screen: QScreen) -> None:
        self.disarm()
        canvas = self._canvases.pop(screen, None)
        if canvas is not None:
            canvas.hide()
            canvas.deleteLater()
        if screen is self._screen:
            self._screen = None
            if self._enabled:
                self._show_toolbar()

    def close(self) -> None:
        self.set_enabled(False)
        for window in self.windows():
            window.deleteLater()
        self._canvases.clear()
        self.deleteLater()


class _Canvas(QWidget):
    def __init__(self, live: LiveAnnotations, screen: QScreen) -> None:
        super().__init__(None, _FLOATING | _PASS_THROUGH)
        self.live = live
        self.screen = screen
        self.targets: tuple[QRect, ...] = ()
        self._marks: list[tuple[Annotation, float | None]] = []
        self._next_step = 1
        self._draft: Annotation | None = None
        self.label_editor: LabelEditor | None = None
        self._pin_label = False
        self._expire = QTimer(self, singleShot=True)
        self._expire.setTimerType(Qt.TimerType.PreciseTimer)
        self._expire.timeout.connect(self._expire_marks)
        self.setWindowTitle("OpenWhisper Walkthrough Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setProperty(_HOTKEY_WINDOW_PROPERTY, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        screen.geometryChanged.connect(self.setGeometry)
        self.setGeometry(screen.geometry())

    def present(self) -> None:
        armed = self.live.armed
        flags = _FLOATING if armed else _FLOATING | _PASS_THROUGH
        if (self.windowFlags() & _PASS_THROUGH) != (flags & _PASS_THROUGH):
            self.setWindowFlags(flags)
        if not armed and not self._marks:
            self.hide()
            return
        self.setScreen(self.screen)
        self.setGeometry(self.screen.geometry())
        self.setCursor(Qt.CursorShape.IBeamCursor if self.live.tool is Tool.TEXT
                       else Qt.CursorShape.CrossCursor)
        self.show()
        if not affinity.is_kept_out(self):
            affinity.keep_out_of_screenshots(self, True)
        self.update()

    def items(self, *, pinned_only: bool = False) -> tuple[Annotation, ...]:
        return tuple(mark for mark, expires in self._marks if expires is None or not pinned_only)

    def add(self, mark: Annotation, pinned: bool) -> None:
        if isinstance(mark, Step):
            self._next_step += 1
        self._marks.append((mark, None if pinned else monotonic() + MARK_LIFETIME_S))
        self._expire_marks()

    def _expire_marks(self) -> None:
        now = monotonic()
        self._marks = [(mark, expires) for mark, expires in self._marks
                       if expires is None or expires > now]
        next_expiry = min((expires for _, expires in self._marks if expires is not None),
                          default=None)
        if next_expiry is not None:
            self._expire.start(math.ceil((next_expiry - now) * 1000))
        if not self._marks and not self.live.armed:
            self.hide()
        self.update()

    def clear(self) -> None:
        self._marks.clear()
        self._next_step = 1
        self._expire.stop()
        self.cancel_draft()
        self._expire_marks()

    def cancel_draft(self) -> None:
        self._draft = None
        if self.label_editor is not None:
            self.label_editor.hide()
            self.label_editor.deleteLater()
            self.label_editor = None
        self.update()

    def _point(self, point: QPointF) -> QPointF:
        if self.live.snap_enabled and self.live.tool in (Tool.ARROW, Tool.BOX):
            point = snap_point(point, self.targets)
        return QPointF(min(max(point.x(), 0), self.width()),
                       min(max(point.y(), 0), self.height()))

    def mousePressEvent(self, event) -> None:
        if event.button() != _LEFT or not self.live.armed:
            return
        if self.label_editor is not None:
            self._commit_label()
            return
        tool = self.live.tool
        color = QColor(SWATCHES[self.live.color].color)
        point = self._point(event.position())
        if tool is Tool.TEXT:
            self._open_label(point, color, bool(event.modifiers() & _CTRL))
        elif tool is Tool.STEP:
            self._draft = Step(point, self._next_step, color)
        else:
            self._draft = new_draft(tool, point, color)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._draft is not None:
            before = self._draft.bounds()
            self._draft.extend(self._point(event.position()))
            self.update(before.united(self._draft.bounds()).toAlignedRect().adjusted(-2, -2, 2, 2))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != _LEFT or self._draft is None:
            return
        draft, self._draft = self._draft, None
        draft.extend(self._point(event.position()))
        if draft.meaningful():
            self.live.complete(self, draft, bool(event.modifiers() & _CTRL))
        else:
            self.update()

    def keyPressEvent(self, event) -> None:
        if event.modifiers() & _CHORD:
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.live.disarm()
        elif key == Qt.Key.Key_C:
            self.live.clear()
        elif key == Qt.Key.Key_S:
            self.live.toggle_snap()
        elif key in _TOOL_KEYS and self._draft is None:
            self.live.arm(_TOOL_KEYS[key])
        elif key in _COLOR_KEYS:
            self.live.set_color(_COLOR_KEYS[key])

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            # A click on another monitor can move focus between our canvases.
            QTimer.singleShot(0, self.live.check_focus)

    def _open_label(self, point: QPointF, color: QColor, pin: bool) -> None:
        self._pin_label = pin
        self.label_editor = editor = LabelEditor(self, color)
        editor.move(label_origin(self.rect(), point, editor.size()))
        editor.refit()
        editor.committed.connect(self._commit_label)
        editor.cancelled.connect(self.live.disarm)
        editor.show()
        editor.setFocus()

    def _commit_label(self) -> None:
        label = self.label_editor.annotation()
        pinned = self._pin_label or bool(QGuiApplication.keyboardModifiers() & _CTRL)
        self.cancel_draft()
        if label.meaningful():
            self.live.complete(self, label, pinned)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if self.live.armed:
            # Windows lets mouse events through fully transparent layered pixels.
            painter.fillRect(event.rect(), QColor(0, 0, 0, 1))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        for mark in self.items():
            mark.paint(painter)
        if self._draft is not None:
            self._draft.paint(painter)
        painter.end()
