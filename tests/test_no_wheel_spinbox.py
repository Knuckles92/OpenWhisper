"""Chevron stepper on NoWheelSpinBox."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractSpinBox, QApplication

from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets.no_wheel import NoWheelSpinBox


class TestNoWheelSpinBox:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _box(self, minimum=1, maximum=10, value=5) -> NoWheelSpinBox:
        box = NoWheelSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.resize(120, 36)
        box.show()
        self.app.processEvents()
        return box

    def test_hides_native_arrows_and_loads_chevrons(self):
        box = self._box()
        try:
            assert (
                box.buttonSymbols()
                == QAbstractSpinBox.ButtonSymbols.NoButtons
            )
            assert box.up_button.objectName() == "spinStepUp"
            assert box.down_button.objectName() == "spinStepDown"
            assert not box.up_button.icon().isNull()
            assert not box.down_button.icon().isNull()
            assert box.up_button.toolTip() == "Increase"
            assert box.down_button.toolTip() == "Decrease"
        finally:
            box.close()
            box.deleteLater()

    def test_chevron_buttons_step_the_value(self):
        box = self._box()
        try:
            QTest.mouseClick(box.up_button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            assert box.value() == 6
            QTest.mouseClick(box.down_button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            assert box.value() == 5
        finally:
            box.close()
            box.deleteLater()

    def test_buttons_disable_at_the_range_ends(self):
        box = self._box()
        try:
            box.setValue(box.maximum())
            self.app.processEvents()
            assert not box.up_button.isEnabled()
            assert box.down_button.isEnabled()
            box.setValue(box.minimum())
            self.app.processEvents()
            assert box.up_button.isEnabled()
            assert not box.down_button.isEnabled()
        finally:
            box.close()
            box.deleteLater()

    def test_special_value_text_fits_beside_the_stepper(self):
        box = NoWheelSpinBox()
        try:
            box.setRange(0, 65535)
            box.setSpecialValueText("Automatic")
            box.setValue(0)
            box.adjustSize()
            box.show()
            self.app.processEvents()
            edit = box.lineEdit()
            assert edit is not None
            margins = edit.textMargins()
            available = (
                edit.width() - margins.left() - margins.right()
            )
            needed = box.fontMetrics().horizontalAdvance("Automatic")
            assert available >= needed
            assert box.width() >= box.sizeHint().width()
        finally:
            box.close()
            box.deleteLater()

    def test_stylesheet_uses_chevron_images_not_border_marks(self):
        sheet = ThemeManager().stylesheet
        assert "chevron-up-gray.svg" in sheet
        assert "chevron-down-gray.svg" in sheet
        up_rule = sheet.split("QSpinBox::up-arrow {", 1)[1].split("}", 1)[0]
        down_rule = sheet.split("QSpinBox::down-arrow {", 1)[1].split("}", 1)[0]
        assert "border-left" not in up_rule
        assert "border-top" not in up_rule
        assert "border-left" not in down_rule
        assert "border-bottom" not in down_rule
