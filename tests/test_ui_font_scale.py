"""UI font-scale resolver application and stylesheet rewriting."""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from services.settings import SettingsKey, UiFontScale
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.utils.font_scale import (
    apply_ui_font_scale,
    current_ui_font_scale,
    current_ui_font_scale_percent,
    scale_qss_fonts,
)
from ui_qt.utils.theme_manager import ThemeManager


class TestScaleQssFonts:
    def test_multiplies_integer_px_and_leaves_other_lengths(self):
        source = "QLabel { font-size: 14px; min-height: 14px; padding: 8px; }"
        assert scale_qss_fonts(source, 1.3) == (
            "QLabel { font-size: 18px; min-height: 14px; padding: 8px; }"
        )

    def test_identity_scale_returns_the_same_string(self):
        source = "font-size: 14px;"
        assert scale_qss_fonts(source, 1.0) is source

    def test_scales_pt_and_decimal_px(self):
        assert scale_qss_fonts("font-size: 10pt;", 1.5) == "font-size: 15pt;"
        assert scale_qss_fonts("font-size: 10.5px;", 2.0) == "font-size: 21px;"


class TestApplyUiFontScale:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def teardown_method(self):
        apply_ui_font_scale(UiFontScale.DEFAULT, app=self.app)

    def test_rewrites_widget_stylesheet_from_the_original(self):
        label = QLabel()
        label.setStyleSheet("QLabel { font-size: 10px; }")
        apply_ui_font_scale(UiFontScale.EXTRA_LARGE, app=self.app)
        assert "13px" in label.styleSheet()
        apply_ui_font_scale(UiFontScale.LARGE, app=self.app)
        assert "12px" in label.styleSheet()
        apply_ui_font_scale(UiFontScale.DEFAULT, app=self.app)
        assert "10px" in label.styleSheet()
        label.deleteLater()

    def test_theme_stylesheet_font_sizes_scale(self):
        manager = ThemeManager()
        designed = manager.stylesheet
        assert "font-size: 14px" in designed
        scaled = manager.scaled_stylesheet(1.3)
        assert scaled.count("font-size: 18px") == designed.count("font-size: 14px")
        apply_ui_font_scale(
            UiFontScale.EXTRA_LARGE, app=self.app, theme_manager=manager
        )
        assert "font-size: 18px" in self.app.styleSheet()
        assert current_ui_font_scale_percent() == UiFontScale.EXTRA_LARGE
        assert current_ui_font_scale() == 1.3


class TestSettingsFontScaleControl:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_combo_persists_the_chosen_percent(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            with patch(
                "ui_qt.dialogs.settings_dialog.settings_manager.get",
                return_value=UiFontScale.DEFAULT,
            ), patch(
                "ui_qt.dialogs.settings_dialog.settings_manager.save_setting"
            ) as save:
                index = dialog.ui_font_scale_combo.findData(UiFontScale.LARGE)
                dialog.ui_font_scale_combo.setCurrentIndex(index)
                save.assert_called_once_with(
                    SettingsKey.UI_FONT_SCALE, UiFontScale.LARGE
                )
        finally:
            dialog.close()
