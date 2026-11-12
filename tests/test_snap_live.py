"""Live annotation input, screenshot boundaries and mark lifetime."""

from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QGuiApplication, QHelpEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QToolTip

from ui_qt.snap import live
from ui_qt.snap.annotations import Label, Redaction, Stroke, Tool


@pytest.fixture
def no_focus_moves(monkeypatch):
    monkeypatch.setattr(live.affinity, "bring_to_front", lambda _: None)
    monkeypatch.setattr(live.affinity, "return_foreground", lambda *_: None)


@pytest.mark.parametrize("tool", [Tool.BOX, Tool.ELLIPSE])
@pytest.mark.parametrize("infill", [False, True])
def test_rectangle_infill_persists_for_live_drawing(no_focus_moves, infill, tool):
    from services.settings import SettingsKey, settings_manager

    settings_manager.save_setting(SettingsKey.SNAP_BOX_INFILL, not infill)
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        drawing.arm(tool)
        toolbar = drawing._toolbar
        QTest.mouseClick(toolbar, Qt.MouseButton.LeftButton,
                         pos=toolbar.button_rect("infill").center())
        assert live.resolve_snap_box_infill() is infill
        drawing.arm(Tool.PEN)
        drawing.arm(tool)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(180, 120))
        assert canvas.items()[-1].infill is infill
    finally:
        drawing.close()

    reopened = live.LiveAnnotations(owner)
    try:
        assert reopened.box_infill is infill
        assert reopened._toolbar._box_infill is infill
    finally:
        reopened.close()


@pytest.mark.parametrize("tool", [Tool.BOX, Tool.ELLIPSE, Tool.ARROW])
def test_live_shift_drag_constrains_callouts(no_focus_moves, tool):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        assert not drawing.box_infill
        drawing.arm(tool)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ShiftModifier, QPoint(180, 130))
        mark = canvas.items()[-1]
        assert mark.end.x() - mark.start.x() == pytest.approx(mark.end.y() - mark.start.y())
    finally:
        drawing.close()


def test_live_redactions_leave_with_their_screenshot_like_other_marks(monkeypatch, no_focus_moves):
    now = 100.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    owner = QObject()
    drawing = live.LiveAnnotations(owner)

    def redact(modifiers=Qt.KeyboardModifier.NoModifier):
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 60))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, modifiers, QPoint(180, 120))

    try:
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.keyClick(canvas, Qt.Key.Key_R)
        assert drawing.tool is Tool.REDACT
        drawn = Mock()
        drawing.drawn.connect(drawn)
        redact()
        # The screenshot gets the cover; later automatic screenshots do not.
        (_, marks), _ = drawn.call_args
        assert isinstance(marks[0], Redaction)
        assert not drawing.pinned_on(canvas.screen)
        now += live.MARK_LIFETIME_S + 1
        canvas._expire.timeout.emit()
        assert canvas.items() == ()

        # With a capture delay it leaves as the screenshot is taken.
        redact()
        drawing.set_capture_pending(True)
        now += live.MARK_LIFETIME_S + 10
        canvas._expire.timeout.emit()
        assert len(canvas.items()) == 1
        drawing.set_capture_pending(False, captured=True)
        assert canvas.items() == ()

        # Pinning one, or Keep marks, keeps it until Clear marks.
        redact(Qt.KeyboardModifier.ControlModifier)
        redaction, = drawing.pinned_on(canvas.screen)
        now += live.MARK_LIFETIME_S + 10
        canvas._expire.timeout.emit()
        assert canvas.items() == (redaction,)
        drawing.clear()
        assert not drawing.pinned_on(canvas.screen)
    finally:
        drawing.close()


def test_live_toolbar_close_is_dimmed_when_it_has_nothing_to_do(no_focus_moves):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        toolbar = drawing._toolbar
        drop = toolbar.button_rect("drop").center()
        dropped = Mock()
        toolbar.drop_clicked.connect(dropped)
        # No tool and no screenshot coming: it is shown as unavailable.
        assert toolbar._button_at(drop) is None
        QTest.mouseClick(toolbar, Qt.MouseButton.LeftButton, pos=drop)
        dropped.assert_not_called()

        drawing.arm(Tool.PEN)
        assert toolbar._button_at(drop) is not None
        QTest.mouseClick(toolbar, Qt.MouseButton.LeftButton, pos=drop)
        dropped.assert_called_once()
        assert not drawing.armed and toolbar._button_at(drop) is None

        # A delayed screenshot of a click can be cancelled with it too.
        cancelled = Mock()
        drawing.capture_cancelled.connect(cancelled)
        drawing.set_capture_countdown(2.0, 3.0)
        QTest.mouseClick(toolbar, Qt.MouseButton.LeftButton, pos=drop)
        cancelled.assert_called_once()
        drawing.set_capture_countdown(0)
        assert toolbar._button_at(drop) is None
    finally:
        drawing.close()


def test_resuming_live_drawing_reads_the_infill_choice_from_screenshots():
    from services.settings import SettingsKey, settings_manager

    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        assert not drawing.box_infill
        drawing.set_enabled(False)
        settings_manager.save_setting(SettingsKey.SNAP_BOX_INFILL, True)
        drawing.set_enabled(True)
        assert drawing.box_infill and drawing._toolbar._box_infill
    finally:
        drawing.close()


def test_auto_region_stays_click_through_until_closed_and_survives_pause(no_focus_moves):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    changes, shots = [], []
    drawing.capture_changed.connect(lambda: changes.append(True))
    drawing.drawn.connect(lambda *args: shots.append(args))
    try:
        toolbar = drawing._toolbar
        left = Qt.MouseButton.LeftButton
        QTest.mouseClick(toolbar, left, pos=toolbar.button_rect("region").center())
        assert drawing.armed and drawing.selecting_region
        screen = live.capture.screen_under_cursor()
        canvas = drawing._canvases[screen]
        QTest.mousePress(canvas, left, pos=QPoint(180, 160))
        QTest.mouseMove(canvas, QPoint(50, 80))
        QTest.mouseRelease(canvas, left, pos=QPoint(50, 80))
        assert drawing.capture_region == (screen, QRect(50, 80, 130, 80))
        assert not drawing.armed
        assert canvas.isVisible()
        assert canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput
        assert drawing._region_close.isVisible()
        assert toolbar._region_active
        assert not shots

        drawing.select_region()
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert drawing.capture_region == (screen, QRect(50, 80, 130, 80))
        drawing.clear()  # Clearing marks keeps the capture region.
        assert canvas.isVisible()
        drawing.set_enabled(False)
        assert not canvas.isVisible() and not drawing._region_close.isVisible()
        drawing.set_enabled(True)
        assert canvas.isVisible() and drawing._region_close.isVisible()

        QTest.mouseClick(drawing._region_close, left)
        assert drawing.capture_region is None
        assert not drawing._region_close.isVisible() and not canvas.isVisible()
        assert not toolbar._region_active
        assert len(changes) == 2
        assert not shots
    finally:
        drawing.close()


def test_tiny_region_drag_does_not_replace_region(no_focus_moves):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        drawing.select_region()
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        assert drawing.capture_region is None
        assert drawing.selecting_region
        assert drawing._region_close is None
    finally:
        drawing.close()


@pytest.mark.parametrize("captured", [False, True])
def test_temporary_marks_survive_countdown_then_clear_with_successful_snap(monkeypatch, no_focus_moves, captured):
    now = 100.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(90, 110))
        drawing.set_capture_pending(True)
        now += 10
        canvas._expire.timeout.emit()
        assert len(canvas.items()) == 1
        assert not canvas._expire.isActive()
        # Holding a temporary mark must not turn it into a pinned one.
        assert not canvas.items(pinned_only=True)
        drawing.set_capture_pending(False, captured=captured)
        assert len(canvas.items()) == (0 if captured else 1)
        assert canvas._expire.isActive() is not captured
        now += live.MARK_LIFETIME_S
        canvas._expire.timeout.emit()
        assert not canvas.items()
    finally:
        drawing.close()


@pytest.mark.parametrize("keep_marks", [False, True])
def test_delayed_snap_preserves_kept_and_individually_pinned_marks(monkeypatch, no_focus_moves, keep_marks):
    monkeypatch.setattr(live, "resolve_walkthrough_keep_marks", lambda: keep_marks)
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(90, 110))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(70, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ControlModifier, QPoint(110, 110))
        pinned = canvas.items(pinned_only=True)
        assert len(pinned) == (2 if keep_marks else 1)
        drawing.set_capture_pending(True)
        drawing.set_capture_pending(False, captured=True)
        assert canvas.items() == pinned
        assert not canvas._expire.isActive()
    finally:
        drawing.close()


def test_live_tool_stays_selected_until_switched_or_toggled_off(monkeypatch, no_focus_moves):
    now = 100.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    # Verify Qt behavior without changing the desktop foreground in this test.
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    shots = []
    drawing.drawn.connect(lambda screen, marks: shots.append((screen, marks)))
    try:
        toolbar, = drawing.windows()
        screen = live.capture.screen_under_cursor()
        area = screen.geometry()
        assert abs(toolbar.geometry().center().x() - area.center().x()) <= 1
        assert toolbar.geometry().top() == area.top() + live.EDGE_MARGIN
        assert not drawing.armed

        left = Qt.MouseButton.LeftButton
        pen = toolbar.button_rect("tool", Tool.PEN).center()
        box = toolbar.button_rect("tool", Tool.BOX).center()
        QTest.mouseClick(toolbar, left, pos=pen)
        canvas = drawing._canvases[screen]
        assert not canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput
        QTest.mousePress(canvas, left, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, left, pos=QPoint(90, 110))
        assert len(shots) == 1
        assert shots[0][0] is screen
        assert isinstance(shots[0][1][0], Stroke)
        assert drawing.tool is toolbar._tool is Tool.PEN
        assert not canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput

        QTest.mousePress(canvas, left, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, left,
                           Qt.KeyboardModifier.ControlModifier, QPoint(90, 110))
        assert len(shots) == 2
        assert canvas._expire.isActive()
        now += live.MARK_LIFETIME_S
        canvas._expire.timeout.emit()
        pinned = canvas.items(pinned_only=True)
        assert len(pinned) == 1
        assert canvas.items() == pinned

        QTest.mouseClick(toolbar, left, pos=box)
        assert drawing.tool is toolbar._tool is Tool.BOX
        QTest.mouseClick(toolbar, left, pos=box)
        assert drawing.tool is toolbar._tool is None
        assert canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput
        assert canvas.items() == pinned

        QTest.mouseClick(toolbar, left, pos=pen)
        QTest.keyClick(canvas, Qt.Key.Key_C)
        assert not canvas.items()
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert not drawing.armed
        assert not canvas.isVisible()
        assert len(shots) == 2
    finally:
        drawing.close()


def test_live_toolbar_keeps_new_marks_and_clears_them_without_arming(monkeypatch, no_focus_moves):
    now = 100.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    shots = []
    drawing.drawn.connect(lambda _, marks: shots.append(marks))
    try:
        toolbar, = drawing.windows()
        keep = toolbar.button_rect("keep_marks").center()
        clear = toolbar.button_rect("clear").center()
        left = Qt.MouseButton.LeftButton
        assert not live.resolve_walkthrough_keep_marks()
        QTest.mouseClick(toolbar, left, pos=keep)
        assert live.resolve_walkthrough_keep_marks()
        assert not drawing.armed

        drawing.arm(Tool.PEN)
        screen = live.capture.screen_under_cursor()
        canvas = drawing._canvases[screen]
        QTest.mousePress(canvas, left, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, left, pos=QPoint(90, 110))
        (pinned,) = canvas.items(pinned_only=True)
        assert not canvas._expire.isActive()

        QTest.keyClick(canvas, Qt.Key.Key_K)
        assert not live.resolve_walkthrough_keep_marks()
        QTest.mousePress(canvas, left, pos=QPoint(60, 90))
        QTest.mouseRelease(canvas, left, pos=QPoint(100, 120))
        assert canvas._expire.isActive()
        assert len(canvas.items()) == 2
        now += live.MARK_LIFETIME_S
        canvas._expire.timeout.emit()
        assert canvas.items() == (pinned,)

        QTest.mouseClick(toolbar, left, pos=clear)
        assert drawing.tool is Tool.PEN
        assert canvas.isVisible()
        assert not canvas.items()
        assert not canvas._expire.isActive()
        assert len(shots) == 2
    finally:
        drawing.close()


def test_live_toolbar_tooltips_show_the_active_shortcuts(monkeypatch):
    show_tooltip = Mock()
    monkeypatch.setattr(QToolTip, "showText", show_tooltip)
    owner = QObject()
    drawing = live.LiveAnnotations(
        owner, mark_shortcuts={"keep_marks": "F8", "clear": "F9"}
    )
    try:
        toolbar, = drawing.windows()
        assert toolbar.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        for button, tip in (("keep_marks", "Keep marks (F8)"), ("clear", "Clear marks (F9)")):
            pos = toolbar.button_rect(button).center()
            QApplication.sendEvent(
                toolbar, QHelpEvent(QEvent.Type.ToolTip, pos, toolbar.mapToGlobal(pos))
            )
            assert show_tooltip.call_args.args[1] == tip
    finally:
        drawing.close()


def test_live_tool_survives_focus_loss_while_capturing(no_focus_moves):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QApplication.setActiveWindow(canvas)

        drawing.set_busy(True)
        canvas.hide()
        QApplication.setActiveWindow(None)
        drawing.check_focus()
        assert drawing.tool is Tool.PEN

        canvas.show()
        drawing.set_busy(False)
        QGuiApplication.processEvents()
        assert QApplication.activeWindow() is canvas
        assert drawing.tool is Tool.PEN
    finally:
        drawing.close()


def test_live_text_saves_on_commit(no_focus_moves):
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    shots = []
    drawing.drawn.connect(lambda _, marks: shots.append(marks))
    try:
        drawing.arm(Tool.TEXT)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(90, 110))
        assert not shots
        editor = canvas.label_editor
        QTest.keyClicks(editor, "This field")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        (marks,) = shots
        (label,) = marks
        assert isinstance(label, Label)
        assert label.text == "This field"
        assert drawing.tool is Tool.TEXT
    finally:
        drawing.close()
        QGuiApplication.processEvents()


def test_pinned_marks_follow_the_clicked_screen_and_are_rendered_once(monkeypatch, no_focus_moves):
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication, QWidget

    from ui_qt.snap import export, session, support

    monkeypatch.setattr(support, "_platform", lambda: "darwin")
    monkeypatch.setattr(session, "snap_support", lambda: (True, ""))
    panel = QWidget()
    panel.show()
    drawing = live.LiveAnnotations(panel, panel)
    snap = None
    try:
        screen = live.capture.screen_under_cursor()
        monkeypatch.setattr(live.capture, "screen_at_click", lambda _: screen)
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[screen]
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ControlModifier, QPoint(90, 110))
        assert drawing.tool is Tool.PEN
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert not drawing.armed
        (mark,) = drawing.pinned_at((-120, 25))
        windows = (panel, *drawing.windows())
        visible = []

        def grab(target):
            assert target is screen
            visible.extend(w for w in windows if w.isVisible())
            image = QImage(screen.geometry().size() * 2, QImage.Format.Format_RGB32)
            image.fill(QColor("#194754"))
            return image

        monkeypatch.setattr(live.capture, "_grab_screen", grab)
        snap = session.start_snap(take=session.SCREEN, click_at=(-120, 25), keep_out=windows)
        kept = []
        snap.kept.connect(kept.append)
        QTest.qWait(100)
        QApplication.processEvents()
        (raw,) = kept
        assert not visible
        assert raw.devicePixelRatio() == 2
        assert all(w.isVisible() for w in windows)
        result = export.render(raw, export.logical_rect(raw), (mark,))
        assert result.pixelColor(140, 190) != raw.pixelColor(140, 190)
        assert result.devicePixelRatio() == 2
    finally:
        if snap is not None and snap.active:
            snap.drop()
        drawing.close()
        panel.close()


def test_mac_live_toolbar_explains_command_to_pin(monkeypatch):
    from types import SimpleNamespace

    from ui_qt.snap import toolbar

    monkeypatch.setattr(toolbar, "sys", SimpleNamespace(platform="darwin"))
    owner = QObject()
    drawing = live.LiveAnnotations(owner)
    try:
        tooltips = [button.tip for button in drawing._toolbar._buttons if button.kind == "tool"]
        assert tooltips and all("Hold Cmd to pin" in tip for tip in tooltips)
    finally:
        drawing.close()
