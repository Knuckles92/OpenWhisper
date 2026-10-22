"""Snap's annotation model: undo and redo, step numbers, drafts and shapes."""

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor

from ui_qt.snap.annotations import (
    HIGHLIGHTER_WIDTH,
    PEN_WIDTH,
    SWATCHES,
    AnnotationStack,
    Arrow,
    Box,
    Label,
    Step,
    Stroke,
    Tool,
    arrow_shapes,
    ink_for,
    new_draft,
)

CORAL = QColor(SWATCHES[0].color)


def _arrow(x1, y1, x2, y2):
    return Arrow(QPointF(x1, y1), QPointF(x2, y2), CORAL)


class TestStack:
    def test_undo_and_redo_walk_back_and_forth(self):
        stack = AnnotationStack()
        first, second = _arrow(0, 0, 50, 0), _arrow(0, 10, 50, 10)
        stack.add(first)
        stack.add(second)

        assert stack.undo() is second
        assert stack.items == (first,)
        assert stack.can_undo and stack.can_redo
        assert stack.redo() is second
        assert stack.items == (first, second)
        assert not stack.can_redo

    def test_a_new_mark_after_undo_forgets_the_redo(self):
        stack = AnnotationStack()
        stack.add(_arrow(0, 0, 50, 0))
        stack.undo()
        stack.add(_arrow(0, 10, 50, 10))

        assert not stack.can_redo
        assert stack.redo() is None
        assert len(stack) == 1

    def test_undo_and_redo_with_nothing_to_do_return_none(self):
        stack = AnnotationStack()

        assert stack.undo() is None
        assert stack.redo() is None
        assert not stack.can_undo and not stack.can_redo

    def test_steps_count_up_and_undo_gives_the_number_back(self):
        stack = AnnotationStack()
        assert stack.next_step_number() == 1
        stack.add(Step(QPointF(10, 10), stack.next_step_number(), CORAL))
        stack.add(_arrow(0, 0, 50, 0))
        stack.add(Step(QPointF(30, 10), stack.next_step_number(), CORAL))

        assert [item.number for item in stack.items if isinstance(item, Step)] == [1, 2]
        assert stack.next_step_number() == 3
        stack.undo()
        assert stack.next_step_number() == 2
        stack.redo()
        assert stack.next_step_number() == 3


class TestDrafts:
    @pytest.mark.parametrize(
        ("tool", "kind"),
        [
            (Tool.ARROW, Arrow),
            (Tool.BOX, Box),
            (Tool.PEN, Stroke),
            (Tool.HIGHLIGHTER, Stroke),
        ],
    )
    def test_drag_tools_start_a_draft_at_the_press(self, tool, kind):
        draft = new_draft(tool, QPointF(4, 5), CORAL)

        assert isinstance(draft, kind)
        assert draft.color == CORAL

    @pytest.mark.parametrize("tool", [Tool.TEXT, Tool.STEP])
    def test_click_tools_have_no_draft(self, tool):
        assert new_draft(tool, QPointF(4, 5), CORAL) is None

    def test_short_drags_are_clicks_but_a_pen_dot_counts(self):
        arrow = new_draft(Tool.ARROW, QPointF(0, 0), CORAL)
        arrow.extend(QPointF(3, 2))
        box = new_draft(Tool.BOX, QPointF(0, 0), CORAL)
        box.extend(QPointF(40, 2))
        dot = new_draft(Tool.PEN, QPointF(0, 0), CORAL)

        assert not arrow.meaningful()
        assert not box.meaningful()
        assert dot.meaningful()
        arrow.extend(QPointF(30, 0))
        box.extend(QPointF(40, 30))
        assert arrow.meaningful() and box.meaningful()

    def test_the_highlighter_is_a_wide_stroke(self):
        pen = new_draft(Tool.PEN, QPointF(0, 0), CORAL)
        marker = new_draft(Tool.HIGHLIGHTER, QPointF(0, 0), CORAL)

        assert (pen.width, marker.width) == (PEN_WIDTH, HIGHLIGHTER_WIDTH)
        assert marker.highlighter and not pen.highlighter

    def test_pen_skips_sub_pixel_jitter(self):
        stroke = new_draft(Tool.PEN, QPointF(0, 0), CORAL)
        stroke.extend(QPointF(0.5, 0.5))
        stroke.extend(QPointF(10, 0))

        assert stroke.points == [QPointF(0, 0), QPointF(10, 0)]

    def test_drafts_copy_the_colour(self):
        color = QColor(CORAL)
        draft = new_draft(Tool.BOX, QPointF(0, 0), color)
        color.setRed(0)

        assert draft.color == CORAL

    def test_an_empty_label_is_not_a_mark(self):
        assert not Label(QPointF(0, 0), "   ", CORAL).meaningful()
        assert Label(QPointF(0, 0), "Click here", CORAL).meaningful()


class TestArrowShape:
    def test_the_head_points_at_the_end(self):
        _, head = arrow_shapes(QPointF(0, 100), QPointF(200, 100), 4.0)

        assert head[0] == QPointF(200, 100)

    @pytest.mark.parametrize(
        ("start", "end"),
        [((0, 100), (200, 100)), ((200, 100), (0, 100))],
        ids=["rightward", "leftward"],
    )
    def test_horizontal_arrows_bow_upward_either_way(self, start, end):
        shaft, _ = arrow_shapes(QPointF(*start), QPointF(*end), 4.0)

        assert shaft.boundingRect().top() < 100 - 5
        assert shaft.boundingRect().bottom() <= 100 + 0.01

    @pytest.mark.parametrize(
        ("start", "end"),
        [((100, 0), (100, 200)), ((100, 200), (100, 0))],
        ids=["down", "up"],
    )
    def test_vertical_arrows_bow_left(self, start, end):
        shaft, _ = arrow_shapes(QPointF(*start), QPointF(*end), 4.0)

        assert shaft.boundingRect().left() < 100 - 5

    def test_the_shaft_ends_inside_the_head(self):
        shaft, head = arrow_shapes(QPointF(0, 100), QPointF(200, 100), 4.0)

        assert head.containsPoint(shaft.currentPosition(), Qt.FillRule.OddEvenFill)
        assert shaft.currentPosition().x() < 200

    def test_a_zero_length_arrow_has_no_head(self):
        _, head = arrow_shapes(QPointF(5, 5), QPointF(5, 5), 4.0)

        assert head.isEmpty()


def test_badge_ink_is_white_on_dark_fills_and_dark_on_amber():
    inks = [ink_for(QColor(swatch.color)).lightness() for swatch in SWATCHES]
    names = [swatch.name for swatch in SWATCHES]

    assert dict(zip(names, (ink > 128 for ink in inks), strict=True)) == {
        "Coral": True,
        "Amber": False,
        "Blue": True,
        "Green": True,
    }
