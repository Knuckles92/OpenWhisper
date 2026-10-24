"""The snap overlay: selecting, keys, labels, the toolbar, keep and drop."""

import time

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from ui_qt.snap import overlay as overlay_module
from ui_qt.snap import support
from ui_qt.snap.annotations import SWATCHES, Arrow, Box, Label, Step, Tool
from ui_qt.snap.chrome import HintPill
from ui_qt.snap.overlay import Phase, SnapOverlay, window_flags
from ui_qt.utils.palette import DARK_PALETTE

SIZE = (640, 400)


def _frozen(dpr: float = 1.0) -> QImage:
    image = QImage(
        round(SIZE[0] * dpr), round(SIZE[1] * dpr), QImage.Format.Format_RGB32
    )
    image.fill(QColor("#f4efe9"))
    image.setDevicePixelRatio(dpr)
    return image


class Spy:
    def __init__(self, signal):
        self.calls = []
        signal.connect(lambda *args: self.calls.append(args))

    def __len__(self):
        return len(self.calls)


@pytest.fixture
def make_overlay():
    created = []

    def make(dpr: float = 1.0, hint: str = "") -> SnapOverlay:
        overlay = SnapOverlay(_frozen(dpr), None, hint=hint)
        overlay.show()
        created.append(overlay)
        return overlay

    yield make
    for overlay in created:
        overlay.hide()
        overlay.deleteLater()


def _mouse(widget, kind, x, y, buttons=Qt.MouseButton.LeftButton):
    point = QPointF(x, y)
    button = (
        Qt.MouseButton.NoButton
        if kind == QEvent.Type.MouseMove
        else Qt.MouseButton.LeftButton
    )
    event = QMouseEvent(
        kind,
        point,
        widget.mapToGlobal(point),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _drag(widget, start, end):
    _mouse(widget, QEvent.Type.MouseButtonPress, *start)
    for step in range(1, 5):
        t = step / 4
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        _mouse(widget, QEvent.Type.MouseMove, x, y)
    _mouse(
        widget, QEvent.Type.MouseButtonRelease, *end, buttons=Qt.MouseButton.NoButton
    )


def _pump_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    return predicate()


def _center(rect):
    return rect.center().x(), rect.center().y()


def _click(widget, x, y, kind=QEvent.Type.MouseButtonPress):
    _mouse(widget, kind, x, y)
    _mouse(
        widget, QEvent.Type.MouseButtonRelease, x, y, buttons=Qt.MouseButton.NoButton
    )


class TestKeepAndDrop:
    def test_enter_with_no_selection_keeps_the_whole_screen(self, make_overlay):
        overlay = make_overlay(dpr=1.5)
        kept = Spy(overlay.kept)

        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert len(kept) == 1
        image = kept.calls[0][0]
        assert image.size() == QSize(960, 600)
        assert image.devicePixelRatio() == 1.5

    def test_enter_keeps_the_dragged_selection_in_device_pixels(self, make_overlay):
        overlay = make_overlay(dpr=2.0)
        kept = Spy(overlay.kept)

        _drag(overlay, (100, 50), (300, 150))
        assert overlay.phase is Phase.ANNOTATE
        assert overlay.selection == QRect(100, 50, 200, 100)
        QTest.keyClick(overlay, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert [image.size() for (image,) in kept.calls] == [QSize(400, 200)]

    def test_escape_drops_exactly_once(self, make_overlay):
        overlay = make_overlay()
        dropped, kept = Spy(overlay.dropped), Spy(overlay.kept)

        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        QTest.keyClick(overlay, Qt.Key.Key_Return)

        assert (len(dropped), len(kept)) == (1, 0)

    def test_keep_and_drop_methods_report_once(self, make_overlay):
        first, second = make_overlay(), make_overlay()
        kept, not_dropped = Spy(first.kept), Spy(first.dropped)
        dropped, not_kept = Spy(second.dropped), Spy(second.kept)

        first.keep()
        first.keep()
        first.drop()
        second.drop()
        second.keep()

        assert (len(kept), len(not_dropped), len(dropped), len(not_kept)) == (
            1,
            0,
            1,
            0,
        )

    def test_closing_the_window_is_a_cancel(self, make_overlay):
        overlay = make_overlay()
        dropped = Spy(overlay.dropped)

        overlay.close()

        assert len(dropped) == 1

    def test_the_kept_image_carries_the_marks(self, make_overlay):
        overlay = make_overlay(dpr=2.0)
        kept = Spy(overlay.kept)
        _drag(overlay, (100, 50), (300, 150))
        overlay.set_tool(Tool.BOX)
        _drag(overlay, (120, 70), (200, 120))

        overlay.keep()

        image = kept.calls[0][0]
        # The box's left edge: local x = 20, mid-height local y = 45, at 2x.
        edge = image.pixelColor(40, 90)
        assert edge.red() > 200 and edge.green() < 140
        assert image.pixelColor(300, 180) == QColor("#f4efe9")


class TestSelecting:
    def test_a_click_is_not_a_selection(self, make_overlay):
        overlay = make_overlay()

        _click(overlay, 200, 200)

        assert overlay.phase is Phase.SELECT
        assert overlay.selection is None
        assert overlay.toolbar.isHidden()

    def test_double_click_with_no_selection_takes_the_whole_screen(self, make_overlay):
        overlay = make_overlay()

        _click(overlay, 200, 200)
        _click(overlay, 200, 200, kind=QEvent.Type.MouseButtonDblClick)

        assert overlay.phase is Phase.ANNOTATE
        assert overlay.selection == QRect(0, 0, *SIZE)
        assert not overlay.toolbar.isHidden()

    def test_drags_are_clamped_to_the_screen(self, make_overlay):
        overlay = make_overlay()

        _drag(overlay, (500, 300), (900, 700))

        assert overlay.selection == QRect(500, 300, 140, 100)

    def test_dragging_outside_an_unmarked_selection_starts_over(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (300, 150))

        _drag(overlay, (400, 200), (500, 300))

        assert overlay.selection == QRect(400, 200, 100, 100)
        assert overlay.phase is Phase.ANNOTATE

    def test_a_marked_selection_is_kept_when_dragging_outside_it(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (300, 150))
        _drag(overlay, (120, 70), (200, 120))

        _drag(overlay, (400, 200), (500, 300))

        assert overlay.selection == QRect(100, 50, 200, 100)
        assert len(overlay.annotations) == 1


class TestToolbar:
    def _panel(self, overlay):
        return overlay.toolbar.geometry()

    def test_sits_under_the_selection(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 40), (500, 160))

        panel = self._panel(overlay)

        assert panel.top() > overlay.selection.bottom()
        assert QRect(0, 0, *SIZE).contains(panel)

    def test_flips_above_a_selection_touching_the_bottom(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 250), (500, SIZE[1]))

        panel = self._panel(overlay)

        assert panel.bottom() < overlay.selection.top()
        assert QRect(0, 0, *SIZE).contains(panel)

    def test_clicks_right_beside_the_panel_reach_the_screenshot(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (0, 0), SIZE)
        overlay.set_tool(Tool.STEP)
        panel = self._panel(overlay)
        assert overlay.selection.contains(panel)

        # Through the window, so the click lands on whichever widget is there.
        QTest.mouseClick(
            overlay.windowHandle(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(panel.center().x(), panel.top() - 4),
        )
        QApplication.processEvents()

        assert len(overlay.annotations) == 1

    def test_its_buttons_drive_the_overlay(self, make_overlay):
        overlay = make_overlay()
        kept = Spy(overlay.kept)
        _drag(overlay, (100, 40), (500, 160))
        toolbar = overlay.toolbar

        _click(toolbar, *_center(toolbar.button_rect("tool", Tool.STEP.value)))
        _click(toolbar, *_center(toolbar.button_rect("color", "2")))
        assert (overlay.tool, overlay.color_index) == (Tool.STEP, 2)
        _click(toolbar, *_center(toolbar.button_rect("keep")))

        assert len(kept) == 1


class TestShortcuts:
    @pytest.mark.parametrize(
        ("key", "tool"),
        [
            (Qt.Key.Key_B, Tool.BOX),
            (Qt.Key.Key_P, Tool.PEN),
            (Qt.Key.Key_H, Tool.HIGHLIGHTER),
            (Qt.Key.Key_T, Tool.TEXT),
            (Qt.Key.Key_N, Tool.STEP),
            (Qt.Key.Key_A, Tool.ARROW),
        ],
    )
    def test_letters_pick_tools(self, make_overlay, key, tool):
        overlay = make_overlay()
        overlay.set_tool(Tool.PEN if tool is not Tool.PEN else Tool.BOX)

        QTest.keyClick(overlay, key)

        assert overlay.tool is tool

    def test_digits_pick_colours(self, make_overlay):
        overlay = make_overlay()
        picked = []
        for key in (Qt.Key.Key_3, Qt.Key.Key_2, Qt.Key.Key_4, Qt.Key.Key_1):
            QTest.keyClick(overlay, key)
            picked.append(overlay.color_index)

        assert picked == [2, 1, 3, 0]
        assert overlay.current_color() == QColor(SWATCHES[0].color)

    def test_undo_and_redo(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (500, 300))
        _drag(overlay, (150, 100), (300, 200))
        _drag(overlay, (150, 150), (300, 250))
        ctrl = Qt.KeyboardModifier.ControlModifier
        ctrl_shift = ctrl | Qt.KeyboardModifier.ShiftModifier
        counts = []

        for key, modifiers in (
            (Qt.Key.Key_Z, ctrl),
            (Qt.Key.Key_Z, ctrl_shift),
            (Qt.Key.Key_Z, ctrl),
            (Qt.Key.Key_Z, ctrl),
            (Qt.Key.Key_Y, ctrl),
        ):
            QTest.keyClick(overlay, key, modifiers)
            counts.append(len(overlay.annotations))

        assert counts == [1, 2, 1, 0, 1]
        assert all(isinstance(item, Arrow) for item in overlay.annotations.items)

    def test_shifted_letters_are_not_shortcuts(self, make_overlay):
        overlay = make_overlay()

        QTest.keyClick(overlay, Qt.Key.Key_B, Qt.KeyboardModifier.ShiftModifier)

        assert overlay.tool is Tool.ARROW


class TestMarks:
    def test_steps_number_themselves_even_on_quick_clicks(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (500, 300))
        overlay.set_tool(Tool.STEP)

        _click(overlay, 150, 100)
        _click(overlay, 200, 100, kind=QEvent.Type.MouseButtonDblClick)

        steps = [item for item in overlay.annotations.items if isinstance(item, Step)]
        assert [step.number for step in steps] == [1, 2]
        assert steps[0].center == QPointF(50, 50)

    def test_box_marks_are_relative_to_the_selection(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (500, 300))
        overlay.set_tool(Tool.BOX)

        _drag(overlay, (120, 70), (220, 170))

        (box,) = overlay.annotations.items
        assert isinstance(box, Box)
        assert box.rect() == QRectF(20, 20, 100, 100)


class TestLabels:
    def _start_label(self, overlay):
        _drag(overlay, (100, 50), (500, 300))
        overlay.set_tool(Tool.TEXT)
        _click(overlay, 200, 150)
        assert overlay.label_editor is not None
        return overlay.label_editor

    def test_escape_while_typing_cancels_only_the_label(self, make_overlay):
        overlay = make_overlay()
        dropped = Spy(overlay.dropped)
        editor = self._start_label(overlay)

        QTest.keyClicks(editor, "Click here")
        QTest.keyClick(editor, Qt.Key.Key_Escape)

        assert overlay.label_editor is None
        assert len(overlay.annotations) == 0
        assert len(dropped) == 0
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        assert len(dropped) == 1

    def test_enter_while_typing_commits_the_label_without_keeping(self, make_overlay):
        overlay = make_overlay()
        kept = Spy(overlay.kept)
        editor = self._start_label(overlay)

        QTest.keyClicks(editor, "Click here")
        QTest.keyClick(editor, Qt.Key.Key_Return)

        assert len(kept) == 0
        (label,) = overlay.annotations.items
        assert isinstance(label, Label)
        assert label.text == "Click here"
        assert label.color == QColor(SWATCHES[0].color)
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        assert len(kept) == 1

    def test_letters_typed_into_a_label_are_not_shortcuts(self, make_overlay):
        overlay = make_overlay()
        editor = self._start_label(overlay)

        QTest.keyClicks(editor, "abt 2")

        assert overlay.tool is Tool.TEXT
        assert overlay.color_index == 0
        assert editor.text() == "abt 2"

    def test_a_colour_change_recolours_the_label_being_typed(self, make_overlay):
        overlay = make_overlay()
        editor = self._start_label(overlay)
        QTest.keyClicks(editor, "Note")

        overlay.set_color(2)
        QTest.keyClick(editor, Qt.Key.Key_Return)

        (label,) = overlay.annotations.items
        assert label.color == QColor(SWATCHES[2].color)

    def test_keep_while_typing_keeps_the_label_too(self, make_overlay):
        overlay = make_overlay()
        kept = Spy(overlay.kept)
        editor = self._start_label(overlay)
        QTest.keyClicks(editor, "Note")

        overlay.keep()

        assert len(kept) == 1
        assert [item.text for item in overlay.annotations.items] == ["Note"]

    def test_a_click_away_finishes_the_label(self, make_overlay):
        overlay = make_overlay()
        editor = self._start_label(overlay)
        QTest.keyClicks(editor, "Note")

        _click(overlay, 400, 250)

        assert overlay.label_editor is None
        assert [item.text for item in overlay.annotations.items] == ["Note"]

    def test_tab_keeps_the_keyboard_in_the_label(self, make_overlay):
        overlay = make_overlay()
        overlay.activateWindow()
        editor = self._start_label(overlay)
        editor.setFocus()
        assert _pump_until(editor.hasFocus)

        QTest.keyClick(editor, Qt.Key.Key_Tab)

        assert editor.hasFocus()
        assert overlay.label_editor is editor

    def test_a_long_label_stays_on_screen(self, make_overlay):
        overlay = make_overlay()
        _drag(overlay, (100, 50), (SIZE[0], 300))
        overlay.set_tool(Tool.TEXT)
        _click(overlay, SIZE[0] - 60, 150)

        QTest.keyClicks(overlay.label_editor, "A rather long note " * 6)

        assert overlay.label_editor.geometry().right() < SIZE[0]

    def test_undo_while_typing_discards_the_label(self, make_overlay):
        overlay = make_overlay()
        editor = self._start_label(overlay)
        QTest.keyClicks(editor, "Note")

        overlay.undo()

        assert overlay.label_editor is None
        assert len(overlay.annotations) == 0


@pytest.mark.parametrize(
    ("platform", "wayland", "kind", "on_top"),
    [
        ("win32", False, Qt.WindowType.Tool, True),
        ("darwin", False, Qt.WindowType.Tool, True),
        ("linux", False, Qt.WindowType.Window, True),
        ("linux", True, Qt.WindowType.Window, False),
    ],
    ids=["windows", "macos", "x11", "wayland"],
)
def test_window_flags_suit_each_desktop(monkeypatch, platform, wayland, kind, on_top):
    monkeypatch.setattr(support, "_platform", lambda: platform)
    monkeypatch.setattr(overlay_module, "is_wayland", lambda: wayland)

    flags = window_flags()

    assert flags & Qt.WindowType.WindowType_Mask == kind
    assert bool(flags & Qt.WindowType.FramelessWindowHint)
    assert bool(flags & Qt.WindowType.WindowStaysOnTopHint) is on_top


class TestHintPill:
    def test_a_long_hint_is_cut_short_so_the_keys_still_fit(self):
        font = QApplication.font()
        keys_only = HintPill("", font, DARK_PALETTE, 600).size().width()
        short = HintPill("Snap 2", font, DARK_PALETTE, 600).size().width()
        long = (
            HintPill("Walkthrough step " * 40, font, DARK_PALETTE, 600).size().width()
        )

        assert keys_only < short < long <= 600

    def test_without_room_for_the_hint_only_the_keys_show(self):
        font = QApplication.font()
        keys_only = HintPill("", font, DARK_PALETTE, 0).size().width()

        assert (
            HintPill("Walkthrough", font, DARK_PALETTE, keys_only + 10).size().width()
            == keys_only
        )
