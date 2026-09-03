import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QToolButton, QVBoxLayout, QWidget

from services.settings import UiFontScale
from ui_qt.main_window import CustomTitleBar
from ui_qt.utils.font_scale import apply_ui_font_scale


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def title_bar(app):
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    bar = CustomTitleBar(host)
    layout.addWidget(bar)
    for name in ("File", "View", "Help"):
        bar.menu_bar.addMenu(name).addAction("Item")
    host.resize(680, 100)
    host.show()
    app.processEvents()
    yield bar
    apply_ui_font_scale(UiFontScale.DEFAULT, app=app)
    host.close()
    app.processEvents()


def _menu_items_visible(bar: CustomTitleBar) -> bool:
    menu_bar = bar.menu_bar
    rects = [menu_bar.actionGeometry(a) for a in menu_bar.actions()]
    overflow = menu_bar.findChild(QToolButton, "qt_menubar_ext_button")
    overflow_hidden = overflow is None or not overflow.isVisible()
    return overflow_hidden and all(menu_bar.rect().contains(r) for r in rects)


@pytest.mark.parametrize("percent", UiFontScale.ALL)
def test_menu_items_fit_at_every_font_scale(app, title_bar, percent):
    apply_ui_font_scale(percent, app=app)
    app.processEvents()
    app.processEvents()

    assert _menu_items_visible(title_bar)
    assert title_bar.height() >= title_bar.BASE_HEIGHT
    assert title_bar.height() >= title_bar.menu_bar.sizeHint().height()
    assert title_bar.close_btn.height() == title_bar.height()


def test_default_scale_keeps_base_height(app, title_bar):
    apply_ui_font_scale(UiFontScale.DEFAULT, app=app)
    app.processEvents()

    assert title_bar.height() == CustomTitleBar.BASE_HEIGHT
    assert _menu_items_visible(title_bar)
