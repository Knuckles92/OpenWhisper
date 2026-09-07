"""Palette tokens, theme switching, and the Settings theme control."""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QLabel

from config import config
from services.settings import SettingsKey, UiTheme, resolve_ui_theme
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.utils.font_scale import (
    apply_ui_theme,
    current_ui_theme_preference,
    resolve_theme_preference,
)
from ui_qt.utils.palette import (
    DARK,
    DARK_PALETTE,
    LIGHT,
    LIGHT_PALETTE,
    Palette,
    current_palette,
    palette_for,
    set_current_palette,
)
from ui_qt.utils.theme_manager import ThemeManager


class TestPalette:
    def test_both_themes_define_the_same_tokens(self):
        assert set(DARK_PALETTE.tokens) == set(LIGHT_PALETTE.tokens)

    def test_resolve_substitutes_every_token_and_leaves_the_rest(self):
        source = "QLabel { color: @text; background: rgba(@accent-rgb, 0.5); }"
        resolved = DARK_PALETTE.resolve(source)
        assert "@" not in resolved
        assert DARK_PALETTE.css("text") in resolved
        assert f"rgba({DARK_PALETTE.css('accent-rgb')}, 0.5)" in resolved
        assert LIGHT_PALETTE.resolve(source) != resolved

    def test_unknown_token_is_an_error_not_silent_text(self):
        with pytest.raises(KeyError, match="@no-such-token"):
            DARK_PALETTE.resolve("color: @no-such-token;")

    def test_color_handles_hex_rgba_and_rgb_triples(self):
        palette = Palette(
            "test",
            {"hex": "#0a84ff", "rgba": "rgba(28, 28, 30, 0.5)", "rgb": "10, 132, 255"},
        )
        assert palette.color("hex") == QColor("#0a84ff")
        rgba = palette.color("rgba")
        assert (rgba.red(), rgba.green(), rgba.blue()) == (28, 28, 30)
        assert rgba.alpha() == 128
        assert palette.color("rgb", 40) == QColor(10, 132, 255, 40)

    def test_theme_stylesheet_has_no_unresolved_tokens_in_either_theme(self):
        for theme in (DARK, LIGHT):
            manager = ThemeManager(theme)
            sheet = manager.stylesheet
            assert sheet
            assert "@" not in sheet, theme
            assert manager.current_theme == theme
        set_current_palette(DARK_PALETTE)

    def test_light_icon_variants_exist(self):
        assets = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "ui_qt", "assets", "tabler"
        )
        for name in ("chevron-down-slate", "chevron-down-gray", "chevron-up-gray"):
            assert os.path.exists(os.path.join(assets, f"{name}.svg")), name
            assert os.path.exists(os.path.join(assets, f"{name}-light.svg")), name


class TestApplyUiTheme:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def teardown_method(self):
        apply_ui_theme(UiTheme.DARK, app=self.app, theme_manager=ThemeManager())
        set_current_palette(DARK_PALETTE)

    def test_switching_restyles_the_app_and_widget_stylesheets(self):
        manager = ThemeManager()
        apply_ui_theme(UiTheme.DARK, app=self.app, theme_manager=manager)
        label = QLabel()
        label.setStyleSheet("QLabel { color: @text; }")
        assert label.styleSheet() == f"QLabel {{ color: {DARK_PALETTE.css('text')}; }}"

        changed = apply_ui_theme(UiTheme.LIGHT, app=self.app, theme_manager=manager)
        assert changed
        assert current_palette() is LIGHT_PALETTE
        assert label.styleSheet() == f"QLabel {{ color: {LIGHT_PALETTE.css('text')}; }}"
        assert LIGHT_PALETTE.css("bg") in self.app.styleSheet()
        assert self.app.palette().color(self.app.palette().ColorRole.Window) == (
            LIGHT_PALETTE.color("bg")
        )

        assert not apply_ui_theme(UiTheme.LIGHT, app=self.app, theme_manager=manager)
        label.deleteLater()

    def test_theme_manager_announces_changes_once(self):
        manager = ThemeManager()
        seen = []
        manager.theme_changed.connect(seen.append)
        assert manager.set_theme(LIGHT)
        assert not manager.set_theme(LIGHT)
        assert manager.set_theme(DARK)
        assert seen == [LIGHT, DARK]

    def test_system_preference_reads_the_platform_scheme(self):
        assert resolve_theme_preference(UiTheme.LIGHT, self.app) == LIGHT
        assert resolve_theme_preference(UiTheme.DARK, self.app) == DARK
        assert resolve_theme_preference("garbage", self.app) == DARK
        assert resolve_theme_preference(UiTheme.SYSTEM, self.app) in (DARK, LIGHT)
        apply_ui_theme(UiTheme.SYSTEM, app=self.app, theme_manager=ThemeManager())
        assert current_ui_theme_preference() == UiTheme.SYSTEM


class TestThemeSetting:
    def test_fresh_install_is_dark(self):
        assert config.UI_THEME == UiTheme.DARK
        assert resolve_ui_theme({}) == UiTheme.DARK

    def test_invalid_saved_value_falls_back_to_default(self):
        assert resolve_ui_theme({SettingsKey.UI_THEME: "sepia"}) == config.UI_THEME
        assert resolve_ui_theme({SettingsKey.UI_THEME: UiTheme.LIGHT}) == UiTheme.LIGHT
        assert resolve_ui_theme({SettingsKey.UI_THEME: UiTheme.SYSTEM}) == UiTheme.SYSTEM

    def test_palette_for_unknown_name_is_dark(self):
        assert palette_for("sepia") is DARK_PALETTE


class TestSettingsThemeControl:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_combo_lists_every_theme_and_persists_the_choice(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            combo = dialog.ui_theme_combo
            assert [combo.itemData(i) for i in range(combo.count())] == list(UiTheme.ALL)
            assert [combo.itemText(i) for i in range(combo.count())] == [
                UiTheme.LABELS[theme] for theme in UiTheme.ALL
            ]
            applied = []
            dialog.on_ui_theme_changed = applied.append
            with patch(
                "ui_qt.dialogs.settings_dialog.settings_manager.get",
                return_value=UiTheme.DARK,
            ), patch(
                "ui_qt.dialogs.settings_dialog.settings_manager.save_setting"
            ) as save:
                combo.setCurrentIndex(combo.findData(UiTheme.LIGHT))
                save.assert_called_once_with(SettingsKey.UI_THEME, UiTheme.LIGHT)
            assert applied == [UiTheme.LIGHT]
        finally:
            dialog.close()
