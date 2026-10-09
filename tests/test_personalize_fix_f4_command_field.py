"""The Command Mode shortcut field keeps its box and buttons apart in narrow windows."""
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, COMMANDS


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_metadata():
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


@pytest.fixture
def scaled_dialog(monkeypatch):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    temp = tempfile.TemporaryDirectory()
    opened = []

    def build(ui_mode, scale, view=SettingsView.ADVANCED):
        monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
        apply_ui_font_scale(scale, app=app, theme_manager=ThemeManager("dark"))
        store = SettingsManager(os.path.join(temp.name, f"settings{len(opened)}.json"))
        store.save_all_settings({
            SettingsKey.SETTINGS_VIEW: view,
            "hotkeys": {"command_mode": "ctrl+alt+shift+k"},
        })
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stack.enter_context(
            patch.object(settings_dialog_module.SettingsDialog, "_fit_to_screen", lambda self: None))
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        opened.append((dialog, stack))
        dialog.show()
        return dialog

    yield build
    for dialog, stack in opened:
        dialog.close()
        stack.close()
    temp.cleanup()
    apply_ui_font_scale(previous_scale, app=app)
    set_current_palette(previous_palette)
    app.setFont(previous_font)
    app.setStyleSheet(previous_style)


def _settle(dialog, width):
    dialog.setMinimumSize(0, 0)
    dialog.resize(width, 700)
    for _ in range(10):
        QApplication.processEvents()


def _assert_apart(field):
    buttons = [b for b in (field.change_button, field.clear_button) if b is not None]
    for widget in (field.input, *buttons):
        assert widget.isVisible()
        assert widget.geometry().left() >= 0
        assert widget.geometry().right() < field.width(), widget
    for button in buttons:
        assert not field.input.geometry().intersects(button.geometry())


@pytest.mark.parametrize("scale", [100, 130])
@pytest.mark.parametrize("ui_mode,width", [
    ("omarchy", 640), ("omarchy", 560), ("omarchy", 460), ("classic", 940),
])
def test_commands_page_field_never_overlaps_clear(scaled_dialog, ui_mode, width, scale):
    dialog = scaled_dialog(ui_mode, scale)
    dialog.select_destination(COMMANDS)
    _settle(dialog, width)
    _assert_apart(dialog.commands_shortcut)


@pytest.mark.parametrize("width", [560, 460])
def test_basic_row_field_never_overlaps_change(scaled_dialog, width):
    dialog = scaled_dialog("omarchy", 130, view=SettingsView.BASIC)
    dialog.select_destination(BASIC_DICTATION)
    _settle(dialog, width)
    _assert_apart(dialog.commands_basic_shortcut)


def test_a_wide_field_keeps_its_buttons_beside_the_box(scaled_dialog):
    dialog = scaled_dialog("classic", 100)
    dialog.select_destination(COMMANDS)
    _settle(dialog, 1100)
    field = dialog.commands_shortcut
    assert field.clear_button.geometry().top() < field.input.geometry().bottom()
    assert field.clear_button.geometry().left() > field.input.geometry().right()


def test_stacks_when_a_button_floor_exceeds_its_hint(scaled_dialog):
    """Measured with the button's floor: below it, Qt would draw the box over Clear."""
    from PyQt6.QtWidgets import QWidget

    from ui_qt.utils.font_scale import current_ui_font_scale
    from ui_qt.widgets.command_settings import INPUT_MIN_WIDTH, CommandShortcutField

    dialog = scaled_dialog("omarchy", 100)
    host = QWidget()
    field = CommandShortcutField(dialog, parent=host)
    clear = field.clear_button
    clear.setMinimumWidth(clear.sizeHint().width() + 40)
    box = round(INPUT_MIN_WIDTH * current_ui_font_scale())
    # Wide enough by the size hint, too narrow by the floor.
    width = box + field._row.spacing() + clear.sizeHint().width() + 20
    host.resize(width, 200)
    field.resize(width, 200)
    host.show()
    for _ in range(5):
        QApplication.processEvents()
    assert field._row.direction() == field._row.Direction.TopToBottom
    _assert_apart(field)
    host.close()
