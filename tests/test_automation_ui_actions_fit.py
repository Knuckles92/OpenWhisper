"""Settings → Automate → Actions fits narrow windows and large fonts, classic and Omarchy."""

import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit

from services.actions.model import AGENT_ACTION_ID
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import ACTIONS
from ui_qt.widgets.setting_tile import TileBase

pytestmark = pytest.mark.qt

ACTIONS_LIST = [
    {"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
     "params": {"agent": "claude_code", "folder": "C:\\code\\a-rather-long-project-folder-name",
                "include_selection": True},
     "hotkey": "ctrl+alt+shift+space", "spoken_names": ["Claude Code"]},
    {"id": "a1", "name": "Ask Claude", "kind": "open_link",
     "params": {"template": "claude://claude.ai/new?q={text}"}, "hotkey": "ctrl+alt+c",
     "spoken_names": ["Claude"]},
    {"id": "h1", "name": "Home Assistant", "kind": "webhook",
     "params": {"url": "http://homeassistant.local:8123/api/webhook/voice-commands",
                "output": "notice"},
     "spoken_names": ["House"], "description": "Lights, heating and the kettle"},
    {"id": "p1", "name": "Journal", "kind": "program",
     "params": {"program": "C:\\Tools\\journal.exe", "args": ["--append"]}, "enabled": False},
]


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
def make_dialog(monkeypatch):
    monkeypatch.setattr("services.actions.agents.available",
                        lambda: {"claude_code": True, "codex": True, "opencode_cli": False})
    stacks, dialogs = [], []
    temp = tempfile.TemporaryDirectory()

    def build(values):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({**values})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        dialog.on_profile_hotkey_capture = MagicMock()
        dialogs.append(dialog)
        dialog.select_destination(ACTIONS)
        return dialog

    yield build
    for dialog in dialogs:
        dialog._loading = True
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _rect_in(widget, ancestor):
    top_left = widget.mapTo(ancestor, widget.rect().topLeft())
    bottom_right = widget.mapTo(ancestor, widget.rect().bottomRight())
    return top_left.x(), top_left.y(), bottom_right.x(), bottom_right.y()


def _controls(widget):
    found = (widget.findChildren(QAbstractButton) + widget.findChildren(QLineEdit)
             + widget.findChildren(QComboBox))
    return [control for control in found if control.isVisible()]


def _open(dialog, state):
    panel = dialog.actions_panel
    if state == "new-folder":
        fields = dialog.actions_agent_card.fields
        fields.chooser.choices["codex"].click()
        fields.new_folder_row.switch.click()
    elif state == "gallery":
        panel.add_action()
    elif state != "list":
        panel.add_action()
        panel.start_from_preset(panel.gallery.tile(state).preset)


@pytest.mark.parametrize("state", ["list", "new-folder", "gallery", "quick-answer", "webhook", "program"])
@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 720), ("omarchy", 460)])
def test_the_actions_page_fits_narrow_windows_and_large_fonts(make_dialog, monkeypatch,
                                                              ui_mode, width, state):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    manager = ThemeManager()
    try:
        apply_ui_font_scale(130, app=app, theme_manager=manager)
        dialog = make_dialog({SettingsKey.ACTIONS: ACTIONS_LIST})
        _open(dialog, state)
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 600)
        for _ in range(12):
            app.processEvents()
        page = dialog._pages[ACTIONS]
        assert dialog.actions_agent_card.isVisible()
        tiles = [tile for tile in page.findChildren(TileBase) if tile.isVisible()]
        assert tiles
        # Below the classic minimum width only the Advanced rail is too wide;
        # the cards themselves must still hold their controls.
        for tile in tiles:
            for control in _controls(tile):
                left, _top, right, _bottom = _rect_in(control, tile)
                assert left >= 0 and right < tile.width(), (
                    tile.title_label.text(), control.objectName() or control.text()
                    if isinstance(control, QAbstractButton) else control.accessibleName())
        if width > 460:
            for control in _controls(page):
                assert _rect_in(control, page)[2] < page.width(), (
                    control.objectName(), control.accessibleName())
        # Nothing is squeezed below the height its text needs: Settings pages
        # lay out at their minimum height.
        assert page.height() >= page.minimumSizeHint().height()
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width()), label.text()
        for control in _controls(page):
            if isinstance(control, QAbstractButton) and control.text():
                assert control.height() >= control.minimumSizeHint().height(), control.text()
    finally:
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
