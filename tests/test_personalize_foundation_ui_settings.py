"""Settings shell for the personalization pages: Personalize rail, page modules, helpers."""
import importlib
import os
import sys
import tempfile
import types
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_basic import BasicSettingsPage
from ui_qt.dialogs.settings_destinations import (
    BASIC_DICTATION,
    CLEANUP,
    CLEANUP_PROFILES,
    CLEANUP_RULES,
    COMMANDS,
    DICTIONARY,
    GENERAL,
    HOTKEYS,
    MEETING_VOICE,
    OVERVIEW,
    SNIPPETS,
    STYLES,
)
from ui_qt.widgets.setting_tile import InfoTile, TileBase

NEW_PAGES = (DICTIONARY, SNIPPETS, STYLES, COMMANDS)
INTRO_SEEN = "flow_features_intro_seen"


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_metadata():
    # Registering a page module writes its routes into these shared maps.
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


@pytest.fixture
def fake_page(monkeypatch):
    """Put a recording page module in the Dictionary slot of the registry."""
    calls = []
    module = types.ModuleType("flow_foundation_fake_page")
    module.TITLE = "Fake words"
    module.SUBTITLE = "Words for the tests."
    module.SEARCH_FIELDS = [("fake_page_tile", "Fake words tile", "zebra quokka")]
    module.CONTROL_ATTRS = ("fake_page_tile",)

    def build(dialog, layout):
        calls.append("build")
        dialog.fake_page_tile = InfoTile("Fake words tile", "zebra quokka")
        dialog._tile_group(layout, "", [dialog.fake_page_tile], columns=1)

    def basic_rows(page, group):
        calls.append(("basic_rows", type(page), type(group)))
        page.add_refresh_hook(lambda settings: calls.append("basic_refresh"))

    module.build = build
    module.load = lambda dialog, settings: calls.append("load")
    module.rail_value = lambda settings: "3 words"
    module.refresh = lambda dialog: calls.append("refresh")
    module.cancel_capture = lambda dialog: calls.append("cancel")
    module.basic_rows = basic_rows
    monkeypatch.setitem(sys.modules, module.__name__, module)
    registry = tuple(
        (key, module.__name__ if key == DICTIONARY else path, icon)
        for key, path, icon in settings_dialog_module._PAGE_MODULES
    )
    monkeypatch.setattr(settings_dialog_module, "_PAGE_MODULES", registry)
    return module, calls


def _modules():
    return [
        (key, importlib.import_module(path))
        for key, path, _icon in settings_dialog_module._PAGE_MODULES
    ]


def _intro_tiles(widget):
    return [tile for tile in widget.findChildren(InfoTile) if tile.property("tileId") == "flowIntro"]


class TestPersonalizeRail:
    def test_personalize_group_sits_between_dictation_and_meeting_mode(self, make_dialog):
        dialog, _store = make_dialog()
        keys = dialog.rail.keys()
        personalize = (DICTIONARY, SNIPPETS, STYLES, CLEANUP_RULES, CLEANUP_PROFILES, COMMANDS)
        assert keys[keys.index(CLEANUP) + 1:keys.index(MEETING_VOICE)] == personalize
        assert dialog._rail_groups[CLEANUP] == "Dictation"
        assert {dialog._rail_groups[key] for key in personalize} == {"Personalize"}
        assert [dialog.rail.name(key) for key in personalize] == [
            "Dictionary", "Snippets", "Styles", "Learned rules", "Profiles", "Commands",
        ]
        assert dialog._headings[STYLES][0] == "Apps & styles"

    def test_page_modules_are_registered_without_building_their_pages(self, make_dialog):
        dialog, _store = make_dialog()
        assert [module for _key, module in _modules()] == list(dialog.page_modules())
        assert not set(NEW_PAGES) & dialog._built_pages
        for key, module in _modules():
            assert dialog.rail.value(key) == module.rail_value({})
            for name in module.CONTROL_ATTRS:
                assert settings_metadata.CONTROL_DESTINATIONS[name] == key

    @pytest.mark.parametrize("key", NEW_PAGES)
    def test_each_page_builds_the_tiles_its_search_copy_names(self, make_dialog, key):
        dialog, _store = make_dialog()
        module = dict(_modules())[key]
        dialog.select_destination(key)
        assert key in dialog._built_pages
        assert dialog.page_title.text() == module.TITLE
        page = dialog._pages[key]
        for attr, title, _keywords in module.SEARCH_FIELDS:
            widget = dialog.__dict__[attr]
            assert page.isAncestorOf(widget)
            if isinstance(widget, TileBase):
                assert widget.title_label.text() == title
        assert set(module.CONTROL_ATTRS) <= set(dialog.__dict__)

    def test_pages_are_searchable_before_they_are_built(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.show()
        try:
            for key, module in _modules():
                attr, title, _description = module.SEARCH_FIELDS[0]
                entries = [
                    entry for entry in dialog._search_index()
                    if entry.destination == key and entry.target_name == attr
                ]
                assert [entry.title for entry in entries] == [title]
                assert entries[0].detail == f"Personalize › {dialog.rail.name(key)}"
                assert key not in dialog._built_pages
                dialog._on_search_activated(entries[0])
                QApplication.processEvents()
                assert dialog.rail.current_key() == key
                assert getattr(dialog, attr).property("searchHit") is True
        finally:
            dialog.close()

    def test_a_control_attribute_builds_its_page(self, make_dialog):
        dialog, _store = make_dialog()
        tile = dialog.commands_tile
        assert COMMANDS in dialog._built_pages
        assert tile.parentWidget() is dialog._pages[COMMANDS]


class TestPageModuleRegistry:
    def test_registry_calls_each_hook_at_its_moment(self, make_dialog, fake_page):
        module, calls = fake_page
        dialog, _store = make_dialog()
        assert dialog.rail.value(DICTIONARY) == "3 words"
        assert settings_metadata.CONTROL_DESTINATIONS["fake_page_tile"] == DICTIONARY
        assert calls == []

        dialog.fake_page_tile
        assert calls == ["build", "load"]

        calls.clear()
        dialog.refresh()
        assert calls == ["load", "refresh"]

        calls.clear()
        dialog.select_destination(DICTIONARY)
        assert calls == []
        dialog.select_destination(GENERAL)
        assert calls == ["cancel"]

        calls.clear()
        dialog.refresh_page(DICTIONARY)
        assert calls == ["refresh"]

        calls.clear()
        dialog.set_settings_view(SettingsView.BASIC)
        assert calls[0] == "cancel"
        calls.clear()
        dialog.close()
        assert calls == ["cancel"]

    def test_unbuilt_pages_are_neither_loaded_refreshed_nor_cancelled(self, make_dialog, fake_page):
        _module, calls = fake_page
        dialog, _store = make_dialog()
        dialog.refresh()
        dialog.refresh_page(DICTIONARY)
        dialog.select_destination(GENERAL)
        dialog.close()
        assert calls == []

    def test_a_failing_rail_value_leaves_the_rail_working(self, make_dialog, fake_page, monkeypatch):
        module, _calls = fake_page
        dialog, store = make_dialog()

        def broken(_settings):
            raise ValueError("broken page")

        monkeypatch.setattr(module, "rail_value", broken)
        dialog._refresh_rail_values()
        assert dialog.rail.value(DICTIONARY) == ""
        dialog.select_destination(GENERAL)
        dialog.copy_clipboard_check.toggle()
        assert store.get(SettingsKey.COPY_CLIPBOARD) == dialog.copy_clipboard_check.isChecked()

    def test_a_failing_page_still_opens_switches_and_closes(self, make_dialog, fake_page, monkeypatch):
        module, calls = fake_page

        def broken(*_args):
            calls.append("broken")
            raise ValueError("broken page")

        for hook in ("load", "refresh", "cancel_capture"):
            monkeypatch.setattr(module, hook, broken)
        dialog, _store = make_dialog()
        dialog.select_destination(DICTIONARY)
        assert dialog.rail.current_key() == DICTIONARY
        dialog.refresh()
        dialog.select_destination(GENERAL)
        assert dialog.rail.current_key() == GENERAL
        dialog.close()
        assert calls == ["build"] + ["broken"] * 5


class TestDialogHelpers:
    def test_standard_hotkey_survives_a_later_hotkeys_page_edit(self, make_dialog):
        dialog, store = make_dialog()
        assert dialog.set_standard_hotkey("command_mode", "ctrl+alt+k") == ""
        assert store.load_hotkey_settings()["command_mode"] == "ctrl+alt+k"
        assert dialog.message_label.text() == "Command Mode hotkey updated."

        dialog.refresh()
        dialog.select_destination(HOTKEYS)
        thread = object()
        dialog.capture_thread, dialog.capturing = thread, "cancel"
        dialog._on_hotkey_captured(thread, "ctrl+shift+x")
        saved = store.load_hotkey_settings()
        assert saved["cancel"] == "ctrl+shift+x"
        assert saved["command_mode"] == "ctrl+alt+k"

    def test_standard_hotkey_conflicts_match_any_spelling_and_profiles(self, make_dialog):
        profile = {"id": "ticket", "name": "Ticket", "instructions": "Format a ticket.", "hotkey": "ctrl+alt+t"}
        dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [profile]})
        assert dialog.set_standard_hotkey("command_mode", "ctrl+alt+k") == ""
        error = dialog.set_standard_hotkey("scratchpad_toggle", "Alt+Ctrl+K")
        assert error == "That shortcut is already used by Command Mode."
        assert dialog.message_label.text() == error
        assert dialog.set_standard_hotkey("scratchpad_toggle", "alt+ctrl+t") == (
            "That shortcut is already used by Ticket."
        )
        assert store.load_hotkey_settings().get("scratchpad_toggle", "") == ""

        assert dialog.set_standard_hotkey("command_mode", "ctrl+alt+k") == ""
        cancel = dialog.current_hotkeys["cancel"]
        assert dialog.set_standard_hotkey("cancel", cancel) == ""

    def test_standard_hotkey_goes_through_the_app_registration(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.on_hotkeys_changed = MagicMock()
        assert dialog.set_standard_hotkey("scratchpad_toggle", "ctrl+alt+n") == ""
        registered = dialog.on_hotkeys_changed.call_args.args[0]
        assert registered["scratchpad_toggle"] == "ctrl+alt+n"
        assert registered["record_toggle"] == dialog.current_hotkeys["record_toggle"]

    def test_standard_hotkey_reports_why_it_was_not_saved(self, make_dialog):
        dialog, store = make_dialog()
        record = dialog.current_hotkeys["record_toggle"]
        error = dialog.set_standard_hotkey("command_mode", record.upper())
        assert "Recording" in error
        assert store.load_hotkey_settings().get("command_mode", "") == ""

        dialog.on_hotkeys_changed = MagicMock(side_effect=OSError("Disk full"))
        error = dialog.set_standard_hotkey("command_mode", "ctrl+alt+k")
        assert error == "Couldn't save hotkeys: Disk full"
        assert dialog.message_label.text() == error
        assert dialog.current_hotkeys.get("command_mode", "") == ""

    def test_clearing_a_standard_hotkey_never_conflicts(self, make_dialog):
        dialog, store = make_dialog()
        assert dialog.set_standard_hotkey("cycle_language", "") == ""
        assert store.load_hotkey_settings()["cycle_language"] == ""

    def test_notify_changed_reaches_the_injected_callback(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.notify_changed("dictionary")
        dialog.on_settings_changed = MagicMock()
        with patch.object(dialog, "_refresh_rail_values") as rail:
            dialog.notify_changed("transforms")
        dialog.on_settings_changed.assert_called_once_with("transforms")
        rail.assert_called_once_with()

    def test_capture_suspension_uses_the_profile_capture_path(self, make_dialog):
        dialog, _store = make_dialog()
        dialog.on_profile_hotkey_capture = MagicMock()
        dialog.set_hotkey_capture_suspended(True)
        dialog.set_hotkey_capture_suspended(False)
        assert [call.args for call in dialog.on_profile_hotkey_capture.call_args_list] == [
            (True,), (False,),
        ]


class TestBasicPersonalize:
    def test_dictation_tab_has_a_personalize_group_fed_by_page_modules(self, make_dialog, fake_page):
        _module, calls = fake_page
        dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
        page = dialog._basic_pages[BASIC_DICTATION]
        titles = [label.text() for label in page.findChildren(QLabel, "basicSettingsGroupTitle")]
        assert titles == ["Record", "Transcribe", "Personalize", "Output"]
        assert ("basic_rows", BasicSettingsPage, QVBoxLayout) in calls
        assert not set(NEW_PAGES) & dialog._built_pages

        calls.clear()
        dialog._refresh_rail_values()
        assert "basic_refresh" in calls

        link = next(
            button for button in page.findChildren(QPushButton, "basicSettingsAdvancedLink")
            if button.text().startswith("Dictionary, snippets")
        )
        link.click()
        assert dialog.settings_view == SettingsView.ADVANCED
        assert dialog.rail.current_key() == DICTIONARY

    def test_new_features_note_dismisses_everywhere_and_stays_dismissed(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
        dialog.show()
        try:
            basic = _intro_tiles(dialog._pages[BASIC_DICTATION])
            overview = _intro_tiles(dialog._pages[OVERVIEW])
            assert len(basic) == len(overview) == 1
            text = basic[0].description_label.text()
            for feature in ("dictionary", "snippets", "styles", "Command Mode",
                            "hands-free", "Stats", "Scratchpad"):
                assert feature in text
            assert basic[0].isVisible()
            dismiss = next(
                button for button in overview[0].findChildren(QPushButton)
                if button.text() == "Got it"
            )
            dismiss.click()
            assert store.get(INTRO_SEEN) is True
            assert basic[0].isHidden() and overview[0].isHidden()
        finally:
            dialog.close()
        reopened = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        try:
            assert not _intro_tiles(reopened._pages[OVERVIEW])
            assert not _intro_tiles(reopened._pages[BASIC_DICTATION])
        finally:
            reopened.close()


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 560)])
def test_new_pages_fit_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale = current_ui_font_scale_percent()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog()
        dialog.show()
        dialog.resize(width, 600)
        for key in (OVERVIEW, *NEW_PAGES):
            dialog.select_destination(key)
            for _ in range(8):
                app.processEvents()
            assert dialog.width() == width
            page = dialog._pages[key]
            controls = (page.findChildren(QAbstractButton) + page.findChildren(QComboBox)
                        + page.findChildren(QLineEdit))
            for control in controls:
                if control.isVisible():
                    assert control.mapTo(page, control.rect().topLeft()).x() >= 0, key
                    assert control.mapTo(page, control.rect().bottomRight()).x() < page.width(), (
                        key, control.objectName() or control.text())
            for tile in page.findChildren(TileBase):
                if tile.isHidden():
                    continue  # a tile shown only in some states
                for label in (tile.title_label, tile.description_label):
                    assert label.height() >= label.heightForWidth(label.width()), key
                    assert tile.rect().contains(label.mapTo(tile, label.rect().bottomRight())), key
                assert page.rect().contains(tile.geometry()), key
            scroll = dialog._page_scrolls[key]
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            app.processEvents()
            assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
