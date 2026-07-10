"""Unit tests for overlay screen-edge positioning helpers."""

import unittest

from PyQt6.QtCore import QPoint, QRect

from ui_qt.utils.overlay_position import (
    clamp_rect_to_available,
    max_height_for_anchor,
    preferred_overlay_position,
)


class TestClampRectToAvailable(unittest.TestCase):
    def setUp(self):
        self.screen = QRect(0, 0, 1920, 1080)

    def test_keeps_in_bounds_position(self):
        self.assertEqual(
            clamp_rect_to_available(100, 200, 300, 80, self.screen),
            (100, 200),
        )

    def test_clamps_right_and_bottom_overflow(self):
        x, y = clamp_rect_to_available(1900, 1050, 300, 200, self.screen)
        self.assertEqual(x, 1920 - 300 - 8)
        self.assertEqual(y, 1080 - 200 - 8)

    def test_clamps_left_and_top_overflow(self):
        self.assertEqual(
            clamp_rect_to_available(-50, -20, 300, 80, self.screen),
            (8, 8),
        )

    def test_handles_secondary_monitor_origin(self):
        secondary = QRect(1920, 0, 1600, 900)
        x, y = clamp_rect_to_available(3400, 850, 300, 200, secondary)
        self.assertEqual(x, 1920 + 1600 - 300 - 8)
        self.assertEqual(y, 900 - 200 - 8)


class TestPreferredOverlayPosition(unittest.TestCase):
    def setUp(self):
        self.screen = QRect(0, 0, 1920, 1080)

    def test_prefers_below_right_of_anchor(self):
        self.assertEqual(
            preferred_overlay_position(QPoint(100, 100), 300, 80, self.screen),
            (110, 110),
        )

    def test_flips_left_near_right_edge(self):
        x, y = preferred_overlay_position(QPoint(1900, 100), 300, 80, self.screen)
        self.assertEqual(x, 1900 - 300 - 10)
        self.assertEqual(y, 110)

    def test_flips_above_near_bottom_edge(self):
        x, y = preferred_overlay_position(QPoint(100, 1040), 300, 200, self.screen)
        self.assertEqual(x, 110)
        self.assertEqual(y, 1040 - 200 - 10)

    def test_flips_and_clamps_in_corner(self):
        x, y = preferred_overlay_position(QPoint(1900, 1040), 300, 200, self.screen)
        # Flips left/above of the anchor, then stays within margins.
        self.assertEqual(x, 1900 - 300 - 10)
        self.assertEqual(y, 1040 - 200 - 10)
        self.assertGreaterEqual(x, 8)
        self.assertGreaterEqual(y, 8)
        self.assertLessEqual(x + 300, 1920 - 8)
        self.assertLessEqual(y + 200, 1080 - 8)

    def test_growing_height_near_bottom_moves_up(self):
        anchor = QPoint(200, 1000)
        short = preferred_overlay_position(anchor, 300, 80, self.screen)
        tall = preferred_overlay_position(anchor, 300, 400, self.screen)
        # Even the short overlay flips above: 1000+10+80 exceeds the bottom margin.
        self.assertEqual(short, (210, 1000 - 80 - 10))
        self.assertLess(tall[1], short[1])
        self.assertGreaterEqual(tall[1], 8)
        self.assertLessEqual(tall[1] + 400, 1080 - 8)


class TestMaxHeightForAnchor(unittest.TestCase):
    def setUp(self):
        self.screen = QRect(0, 0, 1920, 1080)

    def test_respects_configured_max_when_roomy(self):
        self.assertEqual(
            max_height_for_anchor(QPoint(100, 100), self.screen, 400),
            400,
        )

    def test_limits_by_space_on_short_display(self):
        short_screen = QRect(0, 0, 1920, 300)
        height = max_height_for_anchor(QPoint(100, 200), short_screen, 400)
        # above=182, below=82 → room is 182
        self.assertEqual(height, 200 - 10 - 8)
        self.assertLess(height, 400)

    def test_never_returns_below_one(self):
        tiny = QRect(0, 0, 100, 20)
        self.assertGreaterEqual(max_height_for_anchor(QPoint(10, 10), tiny, 400), 1)


if __name__ == "__main__":
    unittest.main()
