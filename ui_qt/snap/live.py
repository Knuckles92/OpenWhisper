"""Draw on the live desktop while a drawing tool is selected."""

from __future__ import annotations

import math
from time import monotonic

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QRegion, QScreen
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import (
    SettingsKey,
    resolve_snap_box_infill,
    resolve_snap_to_edges,
    resolve_walkthrough_keep_marks,
    settings_manager,
)
from ui_qt.snap import affinity, capture, macos, pointed
from ui_qt.snap.annotations import (
    SWATCHES,
    Annotation,
    Step,
    Tool,
    extend_draft,
    new_draft,
)
from ui_qt.snap.chrome import (
    badge_size,
    chrome_font,
    paint_badge,
    paint_frame,
    paint_panel,
)
from ui_qt.snap.geometry import EDGE_MARGIN, snap_point, span
from ui_qt.snap.icons import paint_icon
from ui_qt.snap.overlay import (
    _CHORD,
    _COLOR_KEYS,
    _CTRL_SHIFT,
    _HOTKEY_WINDOW_PROPERTY,
    _KEEP_KEYS,
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
    snap_requested = pyqtSignal(object)
    enabled_changed = pyqtSignal(bool)
    capture_changed = pyqtSignal()
    capture_delay_changed = pyqtSignal(int)
    capture_cancelled = pyqtSignal()
    interaction_changed = pyqtSignal(bool)

    def __init__(
        self, parent: QObject, panel: QWidget | None = None, *,
        mark_shortcuts: dict[str, str] | None = None,
        auto_mode: bool = True,
        snap_shortcut: str = "",
    ) -> None:
        super().__init__(parent)
        self._panel = panel
        self.auto_mode = auto_mode
        self._canvases: dict[QScreen, _Canvas] = {}
        self._enabled = False
        self.busy = False
        self._capture_pending = False
        self._counting_down = False
        self.tool: Tool | None = None
        self.selecting_region = False
        self.selecting_window = False
        self.capture_region: tuple[QScreen, QRect] | None = None
        self._region_source: str | None = None
        self._region_close: _RegionClose | None = None
        self._undo_stack: list[tuple[_Canvas, Annotation, bool]] = []
        self._redo_stack: list[tuple[_Canvas, Annotation, bool]] = []
        self._last_drawn_screen: QScreen | None = None
        self.color = 0
        self.box_infill = resolve_snap_box_infill()
        self.snap_enabled: bool
        self._keep_marks = resolve_walkthrough_keep_marks()
        self._previous_focus = None
        self._panel_level: int | None = None
        self._screen = capture.screen_under_cursor()
        self._toolbar = SnapToolbar(
            None,
            current_palette(),
            live=True,
            auto_mode=auto_mode,
            flags=_FLOATING | _NO_FOCUS,
            mark_shortcuts=mark_shortcuts,
            snap_shortcut=snap_shortcut,
        )
        self._toolbar.setWindowTitle("OpenWhisper Walkthrough Drawing")
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        if self._panel is not None:
            macos.join_all_spaces(self._panel)
        self._toolbar.tool_chosen.connect(self.toggle_tool)
        self._toolbar.color_chosen.connect(self.set_color)
        self._toolbar.undo_clicked.connect(self.undo)
        self._toolbar.redo_clicked.connect(self.redo)
        self._toolbar.snap_clicked.connect(self.toggle_snap)
        self._toolbar.infill_clicked.connect(self.toggle_box_infill)
        self._toolbar.set_box_infill(self.box_infill)
        self._toolbar.keep_marks_clicked.connect(self.toggle_keep_marks)
        self._toolbar.drop_clicked.connect(self.cancel_capture)
        self._toolbar.keep_clicked.connect(lambda: self.request_snap(None))
        if self._toolbar.capture_delay is not None:
            self._toolbar.capture_delay.changed.connect(self.capture_delay_changed)
            # The chip's × cancels only the screenshot; the tool stays in hand.
            self._toolbar.capture_delay.cancelled.connect(self.capture_cancelled)
        self._toolbar.clear_clicked.connect(self.clear)
        self._toolbar.region_clicked.connect(self.select_region)
        self._toolbar.window_clicked.connect(self.select_window)
        self._toolbar.set_keep_marks(self._keep_marks)
        self._toolbar.installEventFilter(self)
        if self._panel is not None:
            self._panel.installEventFilter(self)
        QGuiApplication.instance().screenRemoved.connect(self._remove_screen)
        self._sync_toolbar()
        self.set_enabled(True)

    @property
    def armed(self) -> bool:
        return self.tool is not None or self.selecting_region or self.selecting_window

    def windows(self) -> tuple[QWidget, ...]:
        controls = (self._region_close,) if self._region_close is not None else ()
        delay = (
            self._toolbar.capture_delay.popup_windows()
            if self._toolbar.capture_delay is not None
            else ()
        )
        return (self._toolbar, *self._canvases.values(), *controls, *delay)

    @property
    def interacting(self) -> bool:
        return any(canvas._draft is not None or canvas.label_editor is not None
                   for canvas in self._canvases.values())

    def set_capture_countdown(self, remaining: float, total: float = 0.0) -> None:
        if self._toolbar.capture_delay is not None:
            self._toolbar.capture_delay.set_countdown(remaining, total)
        self._counting_down = remaining > 0
        self._sync_drop()

    def _sync_drop(self) -> None:
        # × puts a tool down or cancels a screenshot that is coming; with
        # neither it has nothing to do, and says so instead of ignoring clicks.
        self._toolbar.set_drop_enabled(
            self.armed or self._capture_pending or self._counting_down)

    def set_capture_pending(self, pending: bool, *, captured: bool = False,
                            screen: QScreen | None = None) -> None:
        """A delayed or live screenshot is coming, or came (``captured``) or was dropped.

        ``screen`` is the monitor the screenshot was of: marks drawn on
        another one were not in it, so they stay. None: every monitor.
        """
        if self._capture_pending == pending:
            return
        self._capture_pending = pending
        self._sync_drop()
        removed_marks: set[int] = set()
        for on_screen, canvas in self._canvases.items():
            if pending:
                canvas._expire.stop()
            else:
                if captured and (screen is None or on_screen is screen):
                    # Temporary marks leave the desktop with their screenshot;
                    # Keep marks and individually pinned marks stay put.
                    kept = []
                    for mark, expires in canvas._marks:
                        if expires is None:
                            kept.append((mark, expires))
                        else:
                            removed_marks.add(id(mark))
                    canvas._marks = kept
                    if not canvas._marks:
                        canvas._next_step = 1
                else:
                    # A cancelled or failed snap keeps manual marks on screen
                    # and gives auto-mode temporary marks their lifetime again.
                    fallback = math.inf if not self.auto_mode else monotonic() + MARK_LIFETIME_S
                    canvas._marks = [(mark, None if expires is None else fallback)
                                     for mark, expires in canvas._marks]
                canvas._expire_marks()
        if removed_marks:
            self._undo_stack = [
                entry for entry in self._undo_stack if id(entry[1]) not in removed_marks
            ]
            self._redo_stack.clear()
            if (self._last_drawn_screen is not None
                    and not self.marks_on(self._last_drawn_screen)):
                self._last_drawn_screen = None
            self._sync_toolbar()

    def cancel_capture(self) -> None:
        self.capture_cancelled.emit()
        self.disarm()

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self.snap_enabled = resolve_snap_to_edges()
            self._toolbar.set_snap_enabled(self.snap_enabled)
            self._sync_toolbar()
            for canvas in self._canvases.values():
                canvas.present()
            self._show_toolbar()
            self._show_region_close()
        else:
            self.disarm()
            self._toolbar.hide()
            for canvas in self._canvases.values():
                canvas.hide()
            if self._region_close is not None:
                self._region_close.hide()
        self.enabled_changed.emit(enabled)

    def _show_toolbar(self) -> None:
        if self._screen is None:
            self._screen = capture.screen_under_cursor()
        if self._screen is None:
            return
        area = self._screen.geometry()
        self._toolbar.fit_to_width(area.width() - 2 * EDGE_MARGIN)
        self._toolbar.setScreen(self._screen)
        self._toolbar.move(area.x() + max(0, (area.width() - self._toolbar.width()) // 2),
                           area.y() + EDGE_MARGIN)
        macos.make_nonactivating(self._toolbar)
        self._toolbar.show()
        macos.join_all_spaces(self._toolbar)
        macos.cover(self._toolbar)
        macos.set_level(self._toolbar, macos.PARKED_LEVEL)
        if not affinity.is_kept_out(self._toolbar):
            affinity.keep_out_of_screenshots(self._toolbar, True)
        self._toolbar.raise_()

    def toggle_tool(self, tool: str) -> None:
        if self.tool == tool:
            self.disarm()
        else:
            self.arm(tool)

    def select_region(self) -> None:
        if self.selecting_region:
            self.disarm()
        else:
            self.arm("region")

    def select_window(self) -> None:
        if self.selecting_window:
            self.disarm()
        else:
            self.arm("window")

    def arm(self, tool: Tool | str = Tool.PEN) -> None:
        if not self._enabled or self.busy:
            return
        screens = QGuiApplication.screens()
        if not screens:
            return
        if not self.armed:
            self.arming.emit()
            self._previous_focus = affinity.foreground_window()
            if self._panel is not None:
                self._panel_level = macos.set_level(self._panel, macos.PARKED_LEVEL)
            for screen in screens:
                if screen not in self._canvases:
                    self._canvases[screen] = _Canvas(self, screen)
            # Read the edges before any of the drawing surfaces take focus.
            exclude = self.windows() + ((self._panel,) if self._panel is not None else ())
            for screen, canvas in self._canvases.items():
                window_rects = pointed.window_targets(screen, exclude)
                canvas.window_rects = window_rects
                canvas.targets = (canvas.rect(), *window_rects)
        elif (self.tool != tool
              or self.selecting_region != (tool == "region")
              or self.selecting_window != (tool == "window")):
            for canvas in self._canvases.values():
                canvas.cancel_draft()
        self.selecting_region = tool == "region"
        self.selecting_window = tool == "window"
        self.tool = None if (self.selecting_region or self.selecting_window) else Tool(tool)
        for canvas in self._canvases.values():
            canvas.present()
        canvas = self._canvases.get(capture.screen_under_cursor())
        if canvas is not None:
            if self.selecting_window:
                canvas.refresh_window_hover()
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
        if self._panel is not None and self._panel_level is not None:
            macos.set_level(self._panel, self._panel_level)
            self._panel_level = None
        self.tool = None
        self.selecting_region = False
        self.selecting_window = False
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
        if self._region_close is not None and self._region_close.isVisible():
            self._region_close.raise_()

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.Move, QEvent.Type.Resize) and self.armed:
            for canvas in self._canvases.values():
                canvas.update_control_mask()
        return super().eventFilter(watched, event)

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

    def toggle_box_infill(self) -> None:
        self.box_infill = not self.box_infill
        settings_manager.save_setting(SettingsKey.SNAP_BOX_INFILL, self.box_infill)
        self._toolbar.set_box_infill(self.box_infill)

    def toggle_keep_marks(self) -> None:
        keep_marks = not self._keep_marks
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_KEEP_MARKS, keep_marks)
        self._keep_marks = keep_marks
        self._toolbar.set_keep_marks(keep_marks)

    def handle_mark_shortcut(self, action: str) -> None:
        if not self._enabled:
            return
        if action == "keep_marks":
            self.toggle_keep_marks()
        elif action == "clear":
            self.clear()

    def set_busy(self, busy: bool) -> None:
        if busy == self.busy:
            return
        self.busy = busy
        self._toolbar.setEnabled(not busy)
        if not busy and self.tool is not None and not self._canvas_has_focus():
            # A screenshot can temporarily hide the canvas that had focus.
            self.arm(self.tool)

    def _sync_toolbar(self) -> None:
        self.box_infill = resolve_snap_box_infill()
        self._toolbar.set_box_infill(self.box_infill)
        has_editor = any(c.label_editor is not None for c in self._canvases.values())
        can_undo = (bool(self._undo_stack) or has_editor) if not self.auto_mode else False
        can_redo = bool(self._redo_stack) and not has_editor if not self.auto_mode else False
        self._toolbar.set_state(self.tool, self.color, can_undo, can_redo)
        self._toolbar.set_region_active(
            self.selecting_region
            or (self.capture_region is not None and self._region_source != "window")
        )
        self._toolbar.set_window_active(
            self.selecting_window
            or (self.capture_region is not None and self._region_source == "window")
        )
        self._sync_drop()

    def set_region(self, screen: QScreen, rect: QRect, *, source: str = "region") -> None:
        bounds = QRect(0, 0, screen.geometry().width(), screen.geometry().height())
        rect = rect.intersected(bounds)
        if rect.width() < 4 or rect.height() < 4:
            return
        self.capture_changed.emit()
        self.capture_region = (screen, QRect(rect))
        self._region_source = source
        self.disarm()
        for canvas in self._canvases.values():
            if self._enabled:
                canvas.present()
        if self._region_close is None:
            self._region_close = _RegionClose(self)
            self._region_close.installEventFilter(self)
        self._show_region_close()
        self._sync_toolbar()

    def clear_region(self) -> None:
        # Discard pending captures and queued hook releases before hiding ×.
        self.capture_changed.emit()
        self.capture_region = None
        self._region_source = None
        if self._region_close is not None:
            self._region_close.hide()
        for canvas in self._canvases.values():
            if self._enabled:
                canvas.present()
        self._sync_toolbar()

    def _show_region_close(self) -> None:
        if not self._enabled or self.capture_region is None or self._region_close is None:
            return
        screen, rect = self.capture_region
        close = self._region_close
        area = screen.geometry()
        close.setScreen(screen)
        # Keep the control on screen, including a selection against an edge.
        x = area.x() + rect.right() - close.width() + 1
        close.move(max(area.left(), min(area.right() - close.width() + 1, x)),
                   max(area.y(), area.y() + rect.y() - close.height() - 3))
        macos.make_nonactivating(close)
        close.show()
        macos.join_all_spaces(close)
        macos.cover(close)
        macos.set_level(close, macos.PARKED_LEVEL)
        affinity.keep_out_of_screenshots(close, True)
        close.raise_()

    def _canvas_has_focus(self) -> bool:
        return QApplication.activeWindow() in self._canvases.values()

    def check_focus(self) -> None:
        if self.armed and not self.busy and not self._canvas_has_focus():
            self.disarm()

    def complete(self, canvas: _Canvas, mark: Annotation, pinned: bool) -> None:
        keep = pinned or self._keep_marks
        canvas.add(mark, keep, persistent=not self.auto_mode)
        self._last_drawn_screen = canvas.screen
        if self.auto_mode:
            self.drawn.emit(canvas.screen, canvas.items())
        else:
            self._undo_stack.append((canvas, mark, keep))
            self._redo_stack.clear()
            self._sync_toolbar()

    def undo(self) -> None:
        if self.auto_mode or self.busy:
            return
        for canvas in self._canvases.values():
            if canvas.label_editor is not None:
                canvas.cancel_draft()
                self._sync_toolbar()
                return
        while self._undo_stack:
            canvas, mark, keep = self._undo_stack.pop()
            if canvas.remove_mark(mark):
                self._redo_stack.append((canvas, mark, keep))
                if (self._last_drawn_screen is canvas.screen
                        and not self.marks_on(canvas.screen)):
                    self._last_drawn_screen = None
                break
        self._sync_toolbar()

    def redo(self) -> None:
        if self.auto_mode or self.busy:
            return
        if any(c.label_editor is not None for c in self._canvases.values()):
            return
        if not self._redo_stack:
            return
        canvas, mark, keep = self._redo_stack.pop()
        if canvas.screen not in self._canvases:
            self._sync_toolbar()
            return
        canvas.add(mark, keep, persistent=not self.auto_mode)
        if self._enabled:
            canvas.present()
        self._undo_stack.append((canvas, mark, keep))
        self._last_drawn_screen = canvas.screen
        self._sync_toolbar()

    def commit_active_editors(self) -> None:
        for canvas in list(self._canvases.values()):
            if canvas.label_editor is not None:
                canvas._commit_label()

    def request_snap(self, screen: QScreen | None = None) -> None:
        if not self._enabled or self.busy:
            return
        self.commit_active_editors()
        self.snap_requested.emit(screen)

    def marks_on(self, screen: QScreen | None) -> tuple[Annotation, ...]:
        canvas = self._canvases.get(screen)
        return canvas.items() if canvas is not None else ()

    def _has_unpinned_marks(self, screen: QScreen | None) -> bool:
        canvas = self._canvases.get(screen)
        return any(expires is not None for _, expires in canvas._marks) if canvas is not None else False

    def target_screen(
        self, *, preferred: QScreen | None = None, from_hotkey: bool = False,
        whole_screen: bool = False,
    ) -> QScreen | None:
        if not whole_screen and self.capture_region is not None:
            return self.capture_region[0]
        screens = QGuiApplication.screens()
        if preferred is not None and preferred in screens:
            if self.marks_on(preferred):
                return preferred
            if (self._last_drawn_screen in screens
                    and self._has_unpinned_marks(self._last_drawn_screen)):
                return self._last_drawn_screen
            return preferred
        if from_hotkey:
            pointed_screen = capture.screen_under_cursor()
            if pointed_screen is not None and self.marks_on(pointed_screen):
                return pointed_screen
            if (self._last_drawn_screen in screens
                    and self._has_unpinned_marks(self._last_drawn_screen)):
                return self._last_drawn_screen
            return pointed_screen
        if self._last_drawn_screen in screens and self.marks_on(self._last_drawn_screen):
            return self._last_drawn_screen
        for screen in screens:
            if self.marks_on(screen):
                return screen
        return capture.screen_under_cursor() or (self._screen if self._screen in screens else None)

    def pinned_at(self, position: tuple[int, int]) -> tuple[Annotation, ...]:
        screen = capture.screen_at_click(position)
        return self.pinned_on(screen)

    def pinned_on(self, screen: QScreen | None) -> tuple[Annotation, ...]:
        canvas = self._canvases.get(screen)
        return canvas.items(pinned_only=True) if canvas is not None else ()

    def clear(self) -> None:
        self.capture_cancelled.emit()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._last_drawn_screen = None
        for canvas in self._canvases.values():
            canvas.clear()
        self._sync_toolbar()

    def _remove_screen(self, screen: QScreen) -> None:
        self.disarm()
        if self.capture_region is not None and self.capture_region[0] is screen:
            self.clear_region()
        canvas = self._canvases.pop(screen, None)
        if canvas is not None:
            self._undo_stack = [entry for entry in self._undo_stack if entry[0] is not canvas]
            self._redo_stack = [entry for entry in self._redo_stack if entry[0] is not canvas]
            canvas.hide()
            canvas.deleteLater()
        if self._last_drawn_screen is screen:
            self._last_drawn_screen = None
        if screen is self._screen:
            self._screen = None
            if self._enabled:
                self._show_toolbar()
        self._sync_toolbar()

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
        self.window_rects: tuple[QRect, ...] = ()
        self._marks: list[tuple[Annotation, float | None]] = []
        self._next_step = 1
        self._draft: Annotation | None = None
        self._region_start: QPointF | None = None
        self._region_draft: QRect | None = None
        self._window_hover: QRect | None = None
        self._window_pressed = False
        self.label_editor: LabelEditor | None = None
        self._pin_label = False
        self._expire = QTimer(self, singleShot=True)
        self._expire.setTimerType(Qt.TimerType.PreciseTimer)
        self._expire.timeout.connect(self._expire_marks)
        self.setWindowTitle("OpenWhisper Walkthrough Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setProperty(_HOTKEY_WINDOW_PROPERTY, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        screen.geometryChanged.connect(self._geometry_changed)
        self.setGeometry(screen.geometry())

    def _geometry_changed(self, rect: QRect) -> None:
        self.setGeometry(rect)
        if self.live.armed:
            self.update_control_mask()
        if self.region() is not None:
            self.live.clear_region()

    def present(self) -> None:
        armed = self.live.armed
        flags = _FLOATING if armed else _FLOATING | _PASS_THROUGH
        if (self.windowFlags() & _PASS_THROUGH) != (flags & _PASS_THROUGH):
            self.setWindowFlags(flags)
        if not armed and not self._marks and self.region() is None:
            self.hide()
            return
        self.setScreen(self.screen)
        self.setGeometry(self.screen.geometry())
        self.update_control_mask()
        if self.live.selecting_window:
            cursor = Qt.CursorShape.PointingHandCursor
        elif self.live.tool is Tool.TEXT:
            cursor = Qt.CursorShape.IBeamCursor
        else:
            cursor = Qt.CursorShape.CrossCursor
        self.setCursor(cursor)
        # Cocoa's show animation can reset the cursor after focus is restored.
        # Apply before showing, including after flags recreate the native window.
        macos.disable_animation(self)
        self.show()
        macos.join_all_spaces(self)
        macos.cover(self)
        if not affinity.is_kept_out(self):
            affinity.keep_out_of_screenshots(self, True)
        self.update()

    def update_control_mask(self) -> None:
        # Clicking a topmost canvas can raise it above its sibling controls.
        # Leave holes for their native hit targets regardless of stacking order.
        if not self.live.armed:
            self.clearMask()
            return
        area = QRegion(self.rect())
        for control in (self.live._toolbar, self.live._panel, self.live._region_close):
            if control is not None and control.isVisible():
                hole = QRect(self.mapFromGlobal(control.mapToGlobal(QPoint())), control.size())
                area = area.subtracted(QRegion(hole))
        self.setMask(area)

    def items(self, *, pinned_only: bool = False) -> tuple[Annotation, ...]:
        return tuple(mark for mark, expires in self._marks if expires is None or not pinned_only)

    def add(self, mark: Annotation, pinned: bool, *, persistent: bool = False) -> None:
        if isinstance(mark, Step):
            self._next_step += 1
        expires = None if pinned else (math.inf if persistent else monotonic() + MARK_LIFETIME_S)
        self._marks.append((mark, expires))
        self._expire_marks()

    def remove_mark(self, target: Annotation) -> bool:
        for index in range(len(self._marks) - 1, -1, -1):
            if self._marks[index][0] is target:
                self._marks.pop(index)
                if isinstance(target, Step):
                    self._next_step = max(1, self._next_step - 1)
                self._expire_marks()
                return True
        return False

    def _expire_marks(self) -> None:
        if self.live._capture_pending:
            self._expire.stop()
            self.update()
            return
        now = monotonic()
        self._marks = [(mark, expires) for mark, expires in self._marks
                       if expires is None or expires > now]
        next_expiry = min(
            (expires for _, expires in self._marks
             if expires is not None and math.isfinite(expires)),
            default=None,
        )
        if next_expiry is not None:
            self._expire.start(math.ceil((next_expiry - now) * 1000))
        else:
            self._expire.stop()
        if not self._marks and not self.live.armed and self.region() is None:
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
        self._region_start = self._region_draft = None
        self._window_hover = None
        self._window_pressed = False
        if self.label_editor is not None:
            self.label_editor.hide()
            self.label_editor.deleteLater()
            self.label_editor = None
        self.live.interaction_changed.emit(False)
        self.update()

    def _window_at(self, pos: QPoint) -> QRect | None:
        for rect in self.window_rects:
            clipped = rect.intersected(self.rect())
            if clipped.width() >= 4 and clipped.height() >= 4 and clipped.contains(pos):
                return clipped
        return None

    def refresh_window_hover(self) -> None:
        if not self.live.selecting_window:
            return
        pos = self.mapFromGlobal(QCursor.pos())
        self._window_hover = self._window_at(pos) if self.rect().contains(pos) else None
        self.update()

    def _point(self, point: QPointF) -> QPointF:
        if self.live.snap_enabled and self.live.tool in (Tool.ARROW, Tool.BOX, Tool.ELLIPSE):
            point = snap_point(point, self.targets)
        return QPointF(min(max(point.x(), 0), self.width()),
                       min(max(point.y(), 0), self.height()))

    def mousePressEvent(self, event) -> None:
        if event.button() != _LEFT or not self.live.armed or self.live.busy:
            return
        if self.label_editor is not None:
            self._commit_label()
            return
        if self.live.selecting_window:
            self._window_pressed = True
            self._window_hover = self._window_at(event.position().toPoint())
            self.update()
            return
        tool = self.live.tool
        color = QColor(SWATCHES[self.live.color].color)
        point = self._point(event.position())
        if self.live.selecting_region:
            self._region_start = point
            self._region_draft = QRect(point.toPoint(), point.toPoint())
        elif tool is Tool.TEXT:
            self._open_label(point, color, bool(event.modifiers() & _CTRL))
        elif tool is Tool.STEP:
            self._draft = Step(point, self._next_step, color)
        else:
            self._draft = new_draft(tool, point, color, box_infill=self.live.box_infill)
        if self.live.interacting:
            self.live.interaction_changed.emit(True)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.live.selecting_window:
            hovered = self._window_at(event.position().toPoint())
            if hovered != self._window_hover:
                self._window_hover = hovered
                self.update()
            return
        if self._region_start is not None:
            self._region_draft = span(self._region_start.toPoint(), self._point(event.position()).toPoint())
            self.update()
        if self._draft is not None:
            before = self._draft.bounds()
            extend_draft(self._draft, self._point(event.position()),
                         constrained=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                         bounds=QRectF(self.rect()))
            self.update(before.united(self._draft.bounds()).toAlignedRect().adjusted(-2, -2, 2, 2))

    def leaveEvent(self, event) -> None:
        if self._window_hover is not None:
            self._window_hover = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == _LEFT and self._window_pressed:
            self._window_pressed = False
            rect = self._window_at(event.position().toPoint())
            if rect is not None:
                self.live.set_region(self.screen, rect, source="window")
            self.update()
            return
        if event.button() == _LEFT and self._region_start is not None:
            rect = span(self._region_start.toPoint(), self._point(event.position()).toPoint())
            self._region_start = self._region_draft = None
            self.live.set_region(self.screen, rect, source="region")
            self.update()
            return
        if event.button() != _LEFT or self._draft is None:
            return
        draft, self._draft = self._draft, None
        extend_draft(draft, self._point(event.position()),
                     constrained=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                     bounds=QRectF(self.rect()))
        self.live.interaction_changed.emit(False)
        if draft.meaningful():
            self.live.complete(self, draft, bool(event.modifiers() & _CTRL))
        else:
            self.update()

    def keyPressEvent(self, event) -> None:
        modifiers = event.modifiers() & _CHORD
        key = event.key()
        if not self.live.auto_mode:
            if modifiers == _CTRL:
                if key == Qt.Key.Key_Z:
                    self.live.undo()
                    return
                if key == Qt.Key.Key_Y:
                    self.live.redo()
                    return
            elif modifiers == _CTRL_SHIFT and key == Qt.Key.Key_Z:
                self.live.redo()
                return
        if modifiers:
            return
        if key == Qt.Key.Key_Escape:
            self.live.cancel_capture()
        elif not self.live.auto_mode and key in _KEEP_KEYS and self._draft is None:
            self.live.request_snap(self.screen)
        elif key == Qt.Key.Key_C:
            self.live.clear()
        elif key == Qt.Key.Key_K and not event.isAutoRepeat():
            self.live.toggle_keep_marks()
        elif key == Qt.Key.Key_S:
            self.live.toggle_snap()
        elif key in _TOOL_KEYS and self._draft is None:
            self.live.arm(_TOOL_KEYS[key])
        elif key in _COLOR_KEYS:
            self.live.set_color(_COLOR_KEYS[key])

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow()
                and not self.live.busy):
            # A click on another monitor can move focus between our canvases.
            QTimer.singleShot(0, self.live.check_focus)

    def _open_label(self, point: QPointF, color: QColor, pin: bool) -> None:
        self._pin_label = pin
        self.label_editor = editor = LabelEditor(self, color)
        editor.move(label_origin(self.rect(), point, editor.size()))
        editor.refit()
        editor.committed.connect(self._commit_label)
        # Esc in a label is Esc on the canvas: it also cancels a pending screenshot.
        editor.cancelled.connect(self.live.cancel_capture)
        editor.show()
        editor.setFocus()
        self.live._sync_toolbar()

    def _commit_label(self) -> None:
        label = self.label_editor.annotation()
        pinned = self._pin_label or bool(QGuiApplication.keyboardModifiers() & _CTRL)
        self.cancel_draft()
        if label.meaningful():
            self.live.complete(self, label, pinned)
        else:
            self.live._sync_toolbar()

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
        region = self._region_draft if self._region_draft is not None else self.region()
        if region is not None:
            paint_frame(painter, region)
        if self.live.selecting_window and self._window_hover is not None:
            fill = current_palette().color("accent")
            fill.setAlpha(28)
            painter.fillRect(self._window_hover, fill)
            paint_frame(painter, self._window_hover)
        if self.live.selecting_region or self.live.selecting_window:
            text = (
                "Click a window to capture · Esc to cancel"
                if self.live.selecting_window
                else "Drag a capture region · Esc to cancel"
            )
            font = chrome_font(self.font(), 12)
            size = badge_size(text, font)
            hint = QRect((self.width() - size.width()) // 2,
                         self.live._toolbar.height() + 2 * EDGE_MARGIN,
                         size.width(), size.height())
            paint_badge(painter, hint, text, font, current_palette())
        painter.end()

    def region(self) -> QRect | None:
        region = self.live.capture_region
        return region[1] if region is not None and region[0] is self.screen else None


class _RegionClose(QWidget):
    """Only this small control takes input; the region frame is click-through."""

    def __init__(self, live: LiveAnnotations) -> None:
        super().__init__(None, _FLOATING | _NO_FOCUS)
        self.live = live
        self.setFixedSize(24, 24)
        self.setWindowTitle("Close automatic capture region")
        self.setAccessibleName("Close region and capture the whole screen")
        self.setToolTip("Close region · capture the whole screen")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pressed = False

    def mousePressEvent(self, event) -> None:
        self._pressed = event.button() == _LEFT

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, False
        if pressed and event.button() == _LEFT and self.rect().contains(event.position().toPoint()):
            self.live.clear_region()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        paint_panel(painter, QRectF(self.rect()), 6, palette, shadow=0)
        paint_icon(painter, "drop", QRectF(2, 2, 20, 20), palette.color("overlay-text"))
        painter.end()
