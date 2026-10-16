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
        rail.add_chapter("Chapter")
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


class TestChapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.rail = NavRail()
        self.rail.add_destination("home", "Home")
        for chapter, pages in (("First", ("a", "b")), ("Second", ("c", "d", "e"))):
            self.rail.add_chapter(chapter)
            for key in pages:
                self.rail.add_destination(key, key.upper())
        QVBoxLayout(self.host).addWidget(self.rail)
        self.host.resize(NavRail.RAIL_WIDTH, 500)
        self.host.show()
        self.rail.select("home")
        self.app.processEvents()
        self.changes = []
        self.rail.destination_changed.connect(self.changes.append)

    def tearDown(self):
        self.host.close()

    def _visible(self):
        return [key for key in self.rail.keys() if not self.rail._items[key].isHidden()]

    def _click_chapter(self, title):
        row = self.rail._chapters[title]["row"]
        QTest.mouseClick(self.host.windowHandle(), Qt.MouseButton.LeftButton,
                         pos=row.name_label.mapTo(self.host, row.name_label.rect().center()))
        self.app.processEvents()

    def test_only_the_current_pages_chapter_shows_its_pages(self):
        self.assertEqual(self._visible(), ["home"])
        self.rail.select("d")
        self.assertEqual(self._visible(), ["home", "c", "d", "e"])
        self.assertEqual(self.rail.open_chapter(), "Second")
        self.rail.select("a")
        self.assertEqual(self._visible(), ["home", "a", "b"])
        self.rail.select("home")
        self.assertEqual(self._visible(), ["home"])

    def test_a_chapter_click_reopens_its_last_page(self):
        self._click_chapter("Second")
        self.assertEqual(self.rail.current_key(), "c")
        self.rail.select("e")
        self._click_chapter("First")
        self.assertEqual(self.rail.current_key(), "a")
        self._click_chapter("Second")
        self.assertEqual(self.rail.current_key(), "e")
        self.assertEqual(self.changes, ["c", "e", "a", "e"])

    def test_a_chapter_heading_is_never_current(self):
        self._click_chapter("First")
        self.assertEqual(self.rail.chapter(self.rail.current_key()), "First")
        self.assertIsNotNone(self.rail.currentItem().data(Qt.ItemDataRole.UserRole))

    def test_arrow_keys_walk_every_page_and_open_chapters(self):
        self.rail.setFocus()
        walked = []
        for _ in range(6):
            QTest.keyClick(self.rail, Qt.Key.Key_Down)
            walked.append((self.rail.current_key(), self.rail.open_chapter()))
        self.assertEqual(walked, [
            ("a", "First"), ("b", "First"), ("c", "Second"),
            ("d", "Second"), ("e", "Second"), ("e", "Second"),
        ])
        QTest.keyClick(self.rail, Qt.Key.Key_Up)
        QTest.keyClick(self.rail, Qt.Key.Key_Up)
        QTest.keyClick(self.rail, Qt.Key.Key_Up)
        self.assertEqual((self.rail.current_key(), self.rail.open_chapter()), ("b", "First"))
        QTest.keyClick(self.rail, Qt.Key.Key_Home)
        self.assertEqual((self.rail.current_key(), self.rail.open_chapter()), ("home", ""))

    def test_a_first_value_grows_a_closed_chapters_row(self):
        row, item = self.rail._rows["c"], self.rail._items["c"]
        single = item.sizeHint().height()
        self.rail.set_value("c", "Current setting")
        self.assertGreater(item.sizeHint().height(), single)
        self.rail.select("c")
        self.app.processEvents()
        self.assertEqual(row.geometry(), self.rail.visualItemRect(item))


if __name__ == "__main__":
    unittest.main()
