"""Live annotation input, screenshot boundaries and mark lifetime."""

from PyQt6.QtCore import QObject, QPoint, Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtTest import QTest

from ui_qt.snap import live
from ui_qt.snap.annotations import Label, Stroke, Tool


def test_live_release_pins_with_ctrl_and_returns_desktop_input(monkeypatch):
    now = 100.0
    monkeypatch.setattr(live, "monotonic", lambda: now)
    # Verify Qt behavior without changing the desktop foreground in this test.
    monkeypatch.setattr(live.affinity, "bring_to_front", lambda _: None)
    monkeypatch.setattr(live.affinity, "return_foreground", lambda *_: None)
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

        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[screen]
        assert not canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(90, 110))
        assert len(shots) == 1
        assert shots[0][0] is screen
        assert isinstance(shots[0][1][0], Stroke)
        assert canvas.windowFlags() & Qt.WindowType.WindowTransparentForInput
        assert not drawing.armed

        drawing.arm(Tool.PEN)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.ControlModifier, QPoint(90, 110))
        assert len(shots) == 2
        assert canvas._expire.isActive()
        now += live.MARK_LIFETIME_S
        canvas._expire.timeout.emit()
        pinned = canvas.items(pinned_only=True)
        assert len(pinned) == 1
        assert canvas.items() == pinned

        drawing.arm(Tool.PEN)
        QTest.keyClick(canvas, Qt.Key.Key_C)
        assert not canvas.items()
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert not drawing.armed
        assert not canvas.isVisible()
        assert len(shots) == 2
    finally:
        drawing.close()


def test_live_text_saves_on_commit(monkeypatch):
    monkeypatch.setattr(live.affinity, "bring_to_front", lambda _: None)
    monkeypatch.setattr(live.affinity, "return_foreground", lambda *_: None)
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
        assert not drawing.armed
    finally:
        drawing.close()
        QGuiApplication.processEvents()
