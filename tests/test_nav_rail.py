"""Layout tests for the Settings navigation rail."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui_qt.widgets.nav_rail import NavRail


class TestNavRail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _scrolling_rail(self, rail_class=NavRail):
        host = QWidget()
        rail = rail_class()
        rail.add_group("Group")
        for index in range(24):
            rail.add_destination(f"page{index}", f"Page {index}")
        rail.select("page0")
        QVBoxLayout(host).addWidget(rail)
        host.resize(NavRail.RAIL_WIDTH, 300)
        return host, rail

    def _assert_rows_fit(self, rail):
        self.assertTrue(rail.verticalScrollBar().isVisible())
        viewport = rail.viewport().width()
        for key, row in rail._rows.items():
            with self.subTest(key=key):
                self.assertLessEqual(row.geometry().right(), viewport - 1)

    def test_rows_fit_beside_the_scroll_bar_when_first_shown(self):
        host, rail = self._scrolling_rail()
        # Settings lays the rail out while hidden, at Qt's default 640 px width.
        rail.doItemsLayout()
        host.show()
        try:
            self.app.processEvents()
            self._assert_rows_fit(rail)
        finally:
            host.close()

    def test_rows_follow_a_narrower_rail(self):
        host, rail = self._scrolling_rail()
        host.show()
        try:
            self.app.processEvents()
            host.resize(NavRail.RAIL_WIDTH - 20, 300)
            self.app.processEvents()
            self._assert_rows_fit(rail)
        finally:
            host.close()

    def test_scrollbar_moves_keep_labels_over_their_click_targets(self):
        from ui_qt.utils.theme_manager import ThemeManager

        previous_style = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        host, rail = self._scrolling_rail()
        for key in rail.keys():
            rail.set_value(key, "Current setting")
        host.show()
        try:
            self.app.processEvents()
            bar = rail.verticalScrollBar()
            for value in (bar.maximum(), bar.maximum() // 2, 0):
                bar.setValue(value)
                self.app.processEvents()
                for key, row in rail._rows.items():
                    point = row.name_label.mapTo(rail.viewport(), row.name_label.rect().center())
                    if rail.viewport().rect().contains(point):
                        with self.subTest(value=value, key=key):
                            self.assertIs(rail.itemAt(point), rail._items[key])
        finally:
            host.close()
            self.app.setStyleSheet(previous_style)

    def test_scroll_geometry_refresh_keeps_rows_aligned(self):
        class RefreshingRail(NavRail):
            def scrollContentsBy(self, dx, dy):
                # Reproduce a layout refresh after the scrollbar's value has
                # changed but before Qt moves the viewport's child widgets.
                self.updateGeometries()
                super().scrollContentsBy(dx, dy)

        host, rail = self._scrolling_rail(RefreshingRail)
        host.show()
        try:
            self.app.processEvents()
            misplaced_moves = []
            items_by_row = {row: rail._items[key] for key, row in rail._rows.items()}
            class PositionObserver(QObject):
                def eventFilter(self, row, event):
                    if event.type() == QEvent.Type.Move:
                        expected = rail.visualItemRect(items_by_row[row]).top()
                        if event.pos().y() != expected:
                            misplaced_moves.append((event.pos().y(), expected))
                    return False

            observer = PositionObserver(rail)
            for row in rail._rows.values():
                row.installEventFilter(observer)
            bar = rail.verticalScrollBar()
            for value in (bar.maximum(), bar.maximum() // 2, 0, bar.maximum()):
                bar.setValue(value)
                self.app.processEvents()
                for key, row in rail._rows.items():
                    with self.subTest(value=value, key=key):
                        expected = rail.visualItemRect(rail._items[key])
                        self.assertEqual(row.geometry().top(), expected.top())
                        self.assertEqual(row.geometry().bottom(), expected.bottom())
                for key, row in rail._rows.items():
                    label = row.name_label
                    point = label.mapTo(rail.viewport(), label.rect().center())
                    if not rail.viewport().rect().contains(point):
                        continue
                    with self.subTest(value=value, clicked=key):
                        self.assertIs(rail.itemAt(point), rail._items[key])
                        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton,
                                         pos=label.mapTo(host, label.rect().center()))
                        self.app.processEvents()
                        self.assertEqual(rail.current_key(), key)
            self.assertEqual(misplaced_moves, [])
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()
