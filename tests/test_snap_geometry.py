"""Snap's logical-to-device maths, toolbar placement and the kept image."""

import pytest
from PyQt6.QtCore import QPoint, QPointF, QRect, QSize
from PyQt6.QtGui import QColor, QImage

from ui_qt.snap import export
from ui_qt.snap.annotations import SWATCHES, Box, Stroke
from ui_qt.snap.geometry import (
    badge_rect,
    device_rect,
    grab_at,
    place_toolbar,
    resized,
    span,
)

CORAL = QColor(SWATCHES[0].color)
WHITE = QColor("#ffffff")


def _frozen(dpr: float, size=(400, 300)) -> QImage:
    image = QImage(
        round(size[0] * dpr), round(size[1] * dpr), QImage.Format.Format_RGB32
    )
    image.fill(WHITE)
    image.setDevicePixelRatio(dpr)
    return image


def _is_coral(color: QColor) -> bool:
    return color.red() > 200 and color.green() < 140 and color.blue() < 120


class TestDeviceRect:
    @pytest.mark.parametrize(
        ("dpr", "expected"),
        [
            (1.0, QRect(10, 20, 100, 50)),
            (1.5, QRect(15, 30, 150, 75)),
            (2.0, QRect(20, 40, 200, 100)),
        ],
    )
    def test_the_selection_scales_to_device_pixels(self, dpr, expected):
        image_rect = QRect(0, 0, round(800 * dpr), round(600 * dpr))

        assert device_rect(QRect(10, 20, 100, 50), dpr, image_rect) == expected

    def test_fractional_ratios_round_outward(self):
        # 1.5..6.0 by 1.5..6.0 device pixels: keep every pixel the frame touched.
        assert device_rect(QRect(1, 1, 3, 3), 1.5, QRect(0, 0, 100, 100)) == QRect(
            1, 1, 5, 5
        )

    def test_the_crop_never_leaves_the_image(self):
        assert device_rect(
            QRect(390, 290, 50, 50), 2.0, QRect(0, 0, 800, 600)
        ) == QRect(780, 580, 20, 20)


def test_a_drag_spans_the_same_rectangle_in_any_direction():
    assert span(QPoint(110, 60), QPoint(10, 10)) == QRect(10, 10, 100, 50)
    assert span(QPoint(10, 60), QPoint(110, 10)) == QRect(10, 10, 100, 50)


class TestToolbarPlacement:
    BOUNDS = QRect(0, 0, 1000, 800)
    SIZE = QSize(400, 46)

    def test_centred_just_below_the_selection(self):
        corner = place_toolbar(
            self.SIZE, QRect(300, 100, 400, 200), self.BOUNDS, gap=10
        )

        assert corner == QPoint(300, 310)

    def test_flips_above_when_the_selection_touches_the_bottom(self):
        selection = QRect(300, 500, 400, 300)
        corner = place_toolbar(self.SIZE, selection, self.BOUNDS, gap=10)

        assert corner.y() + self.SIZE.height() <= selection.top() - 10
        assert corner.y() >= self.BOUNDS.top()

    def test_sits_inside_when_the_selection_fills_the_screen(self):
        corner = place_toolbar(
            self.SIZE, QRect(self.BOUNDS), self.BOUNDS, gap=10, margin=8
        )
        toolbar = QRect(corner, self.SIZE)

        assert self.BOUNDS.contains(toolbar)
        assert toolbar.bottom() < self.BOUNDS.bottom()

    def test_stays_fully_on_screen_beside_an_edge(self):
        corner = place_toolbar(self.SIZE, QRect(0, 100, 60, 60), self.BOUNDS, margin=8)

        assert corner.x() == 8
        assert QRect(self.BOUNDS).contains(QRect(corner, self.SIZE))

    def test_the_size_readout_moves_inside_at_the_top_edge(self):
        size = QSize(80, 22)
        above = badge_rect(size, QRect(100, 200, 300, 100), self.BOUNDS)
        inside = badge_rect(size, QRect(100, 2, 300, 100), self.BOUNDS)

        assert above.bottom() < 200
        assert inside.top() > 2


class TestGrabbing:
    SELECTION = QRect(100, 100, 200, 100)

    @pytest.mark.parametrize(
        ("point", "grab"),
        [
            ((100, 100), (-1, -1)),
            ((92, 92), (-1, -1)),
            ((300, 200), (1, 1)),
            ((310, 95), (1, -1)),
            # Along an edge near its end still takes the corner.
            ((100, 112), (-1, -1)),
            ((200, 98), (0, -1)),
            ((303, 150), (1, 0)),
            ((200, 200), (0, 1)),
        ],
    )
    def test_corners_and_edges(self, point, grab):
        assert grab_at(self.SELECTION, QPoint(*point)) == grab

    @pytest.mark.parametrize("point", [(200, 150), (106, 150), (80, 80), (200, 220)])
    def test_inside_and_far_away_grab_nothing(self, point):
        assert grab_at(self.SELECTION, QPoint(*point)) is None

    def test_a_thin_selection_grabs_the_nearer_edge(self):
        thin = QRect(100, 100, 200, 6)

        assert grab_at(thin, QPoint(200, 101)) == (0, -1)
        assert grab_at(thin, QPoint(200, 105)) == (0, 1)

    def test_resizing_moves_only_the_grabbed_edges(self):
        assert resized(self.SELECTION, (1, 0), QPoint(400, 0)) == QRect(
            100, 100, 300, 100
        )
        assert resized(self.SELECTION, (-1, -1), QPoint(50, 60)) == QRect(
            50, 60, 250, 140
        )

    def test_dragging_past_the_far_side_flips(self):
        assert resized(self.SELECTION, (-1, 0), QPoint(350, 0)) == QRect(
            300, 100, 50, 100
        )


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
class TestExport:
    SELECTION = QRect(20, 10, 100, 60)

    def test_the_crop_is_the_selection_in_device_pixels(self, dpr):
        frozen = _frozen(dpr)
        frozen.setPixelColor(round(20 * dpr), round(10 * dpr), QColor("#123456"))

        image = export.render(frozen, self.SELECTION)

        assert image.size() == QSize(round(100 * dpr), round(60 * dpr))
        assert image.devicePixelRatio() == dpr
        assert image.pixelColor(0, 0) == QColor("#123456")

    def test_a_box_stroke_lands_on_its_device_pixels(self, dpr):
        box = Box(QPointF(30, 20), QPointF(80, 50), CORAL)

        image = export.render(_frozen(dpr), self.SELECTION, [box])

        # The left edge runs down screen x = 30, crop x = 10: device x = 10 * dpr.
        assert _is_coral(image.pixelColor(round(10 * dpr), round(25 * dpr)))
        assert _is_coral(image.pixelColor(round(60 * dpr), round(25 * dpr)))
        # Outside the box the screenshot is untouched.
        assert image.pixelColor(round(85 * dpr), round(50 * dpr)) == WHITE

    def test_a_pen_line_is_drawn_at_device_scale(self, dpr):
        line = Stroke([QPointF(30, 40), QPointF(70, 40), QPointF(110, 40)], CORAL)

        image = export.render(_frozen(dpr), self.SELECTION, [line])

        assert _is_coral(image.pixelColor(round(50 * dpr), round(30 * dpr)))
        # The stroke is PEN_WIDTH (4) logical pixels: 2 * dpr either side.
        assert image.pixelColor(round(50 * dpr), round(30 * dpr - 4 * dpr)) == WHITE

    def test_marks_outside_the_selection_are_clipped(self, dpr):
        outside = Box(QPointF(150, 100), QPointF(250, 200), CORAL)

        image = export.render(_frozen(dpr), self.SELECTION, [outside])

        colors = {
            image.pixel(x, y)
            for x in range(0, image.width(), 7)
            for y in range(0, image.height(), 7)
        }
        assert colors == {WHITE.rgb()}


def test_the_whole_screen_is_the_image_at_its_logical_size():
    assert export.logical_rect(_frozen(2.0, (640, 400))) == QRect(0, 0, 640, 400)
