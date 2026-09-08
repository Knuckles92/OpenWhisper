"""Contextual engine help navigation and popup lifetime."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from ui_qt.widgets.field_help import FieldHelp
from ui_qt.widgets.quick_record_tab import QuickRecordTab
from ui_qt.ui_controller import UIController


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


def test_model_help_links_emit_destinations_and_close():
    tab = QuickRecordTab()
    tab.resize(700, 500)
    tab.show()
    QApplication.processEvents()
    help_button = tab.local_engine.model_combo.parentWidget().findChild(FieldHelp)
    destinations = []
    tab.help_requested.connect(destinations.append)
    try:
        for link, destination in zip(help_button.links, ("ondemand", "downloads")):
            help_button.show_help()
            assert help_button.card.isVisible()
            QTest.mouseClick(link, Qt.MouseButton.LeftButton)
            assert destinations[-1] == destination
            assert not help_button.card.isVisible()
    finally:
        tab.close()


def test_help_card_is_a_window_anchored_to_the_caption():
    tab = QuickRecordTab()
    tab.resize(700, 500)
    tab.show()
    QApplication.processEvents()
    help_button = tab.local_engine.model_combo.parentWidget().findChild(FieldHelp)
    try:
        help_button.show_help()
        QApplication.processEvents()
        card = help_button.card
        assert card.isWindow()
        assert card.isVisible()
        anchor = help_button.mapToGlobal(QPoint(0, help_button.height() + 4))
        assert abs(card.pos().x() - anchor.x()) < 24
        assert abs(card.pos().y() - anchor.y()) < 24
        assert 80 <= card.height() <= 280
        assert card.width() >= 160
    finally:
        tab.close()


def test_popup_stays_reachable_and_escape_dismisses():
    button = FieldHelp("Model", "Choose a model.", [("Open Downloads", "downloads")])
    button.show()
    QApplication.processEvents()
    try:
        button.show_help()
        QCursor.setPos(button.card.mapToGlobal(QPoint(20, 20)))
        button._dismiss_if_outside()
        assert button.card.isVisible()
        QTest.keyClick(button.links[0], Qt.Key.Key_Escape)
        assert not button.card.isVisible()
        button.show_help()
        button.hide()
        assert not button.card.isVisible()
    finally:
        button.close()


@pytest.mark.parametrize("destination", ["ondemand", "downloads", "api_keys"])
def test_help_routes_to_the_named_page(destination):
    dialog = MagicMock()
    controller = SimpleNamespace(
        _prepare_settings_dialog=MagicMock(return_value=dialog),
        _raise_dialog=MagicMock(),
        open_model_manager_dialog=MagicMock(),
    )
    UIController.open_engine_help_destination(controller, destination)
    if destination == "api_keys":
        dialog.focus_api_keys.assert_called_once_with("OPENAI_API_KEY")
        controller._raise_dialog.assert_called_once_with(dialog)
        controller.open_model_manager_dialog.assert_not_called()
    else:
        controller.open_model_manager_dialog.assert_called_once_with(destination)
        controller._prepare_settings_dialog.assert_not_called()
