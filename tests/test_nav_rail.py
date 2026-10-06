"""Layout tests for the Settings navigation rail."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui_qt.widgets.nav_rail import NavRail


class TestNavRail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _scrolling_rail(self):
        host = QWidget()
        rail = NavRail()
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


if __name__ == "__main__":
    unittest.main()
