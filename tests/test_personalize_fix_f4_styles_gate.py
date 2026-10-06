"""Apps & styles follows AI cleanup wherever it changes, through the shared gate."""
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton

from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs import settings_styles
from ui_qt.dialogs.settings_destinations import (
    BASIC_DICTATION,
    CLEANUP,
    CLEANUP_RULES,
    COMMANDS,
    DICTIONARY,
    STYLES,
)
from ui_qt.widgets.hotkey_capture import HotkeyCaptureInput

CLEANUP_ON = {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}
OPENROUTER = {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
              SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free"}


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


@pytest.fixture(autouse=True)
def _windows_text(monkeypatch):
    monkeypatch.setattr(settings_styles, "text_reading_supported", lambda: True)


@pytest.fixture
def make_dialog():
    stacks, dialogs = [], []
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
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        dialogs.append(dialog)
        dialog.show()
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _go(dialog, key):
    dialog.select_destination(key)
    for _ in range(3):
        QApplication.processEvents()


def _styles(dialog):
    _go(dialog, STYLES)
    return dialog.apps_styles_page


def _locked(dialog, page) -> bool:
    cards = [card.tile.isEnabled() for card in page.cards.values()]
    enabled = dialog.app_styles_tile.isEnabled()
    assert all(cards) == enabled and any(cards) == enabled
    return not enabled


def test_cleanup_turned_on_on_its_own_page_unlocks_styles(make_dialog):
    dialog, store = make_dialog(OPENROUTER)
    page = _styles(dialog)
    assert _locked(dialog, page)

    _go(dialog, CLEANUP)
    dialog.transcript_cleanup_check.setChecked(True)
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is True
    _go(dialog, STYLES)

    assert not _locked(dialog, page)
    assert dialog.style_overrides_tile.isEnabled()
    assert "OpenRouter" in page.privacy.text()


def test_cleanup_turned_off_on_its_own_page_locks_styles(make_dialog):
    dialog, _store = make_dialog({**CLEANUP_ON, **OPENROUTER})
    page = _styles(dialog)
    assert not _locked(dialog, page)

    _go(dialog, CLEANUP)
    dialog.transcript_cleanup_check.setChecked(False)
    _go(dialog, STYLES)

    assert _locked(dialog, page)
    assert page.privacy.text().startswith("AI cleanup is off")


def test_learned_rules_turn_on_unlocks_styles(make_dialog):
    dialog, _store = make_dialog()
    page = _styles(dialog)
    _go(dialog, CLEANUP_RULES)
    QTest.mouseClick(dialog.cleanup_rules_turn_on_btn, Qt.MouseButton.LeftButton)
    _go(dialog, STYLES)

    assert not _locked(dialog, page)


def test_provider_changed_while_away_shows_on_return(make_dialog):
    dialog, store = make_dialog({**CLEANUP_ON, **OPENROUTER})
    page = _styles(dialog)
    assert "OpenRouter" in page.privacy.text()

    _go(dialog, COMMANDS)
    store.update_settings({SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
                           SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2"})
    _go(dialog, STYLES)

    assert "on this computer" in page.privacy.text()


def test_styles_shows_the_shared_cleanup_gate_and_turns_cleanup_on_in_place(make_dialog):
    dialog, store = make_dialog()
    page = _styles(dialog)
    gate = dialog.styles_cleanup_gate_tile
    button = next(button for tile, button in dialog._cleanup_gates if tile is gate)

    assert not gate.isHidden() and dialog.styles_gate_tile.isHidden()
    assert gate.title_label.text() == "AI cleanup is off"
    assert button.text() == "Turn on (Medium)"

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is True
    assert dialog.rail.current_key() == STYLES
    assert gate.isHidden()
    assert not _locked(dialog, page)


def test_awareness_gate_shows_only_once_cleanup_is_on(make_dialog):
    dialog, store = make_dialog({SettingsKey.APP_CONTEXT_ENABLED: False})
    page = _styles(dialog)
    assert not dialog.styles_cleanup_gate_tile.isHidden()
    assert dialog.styles_gate_tile.isHidden()

    dialog.turn_on_cleanup()

    assert dialog.styles_cleanup_gate_tile.isHidden()
    assert not dialog.styles_gate_tile.isHidden()
    assert dialog.styles_gate_tile.title_label.text() == "Styles need app awareness"
    assert _locked(dialog, page)
    QTest.mouseClick(page.gate_button, Qt.MouseButton.LeftButton)
    assert store.get(SettingsKey.APP_CONTEXT_ENABLED) is True
    assert dialog.styles_gate_tile.isHidden()
    assert not _locked(dialog, page)


def test_the_style_switch_has_one_name(make_dialog):
    dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    basic = dialog._basic_pages[BASIC_DICTATION]
    basic_title = basic.controls[SettingsKey.APP_STYLES_ENABLED].accessibleName()
    titles = {title for attr, title, _copy in settings_styles.SEARCH_FIELDS if attr == "app_styles_tile"}

    assert titles == {basic_title}
    assert dialog.app_styles_tile.title_label.text() == basic_title


def test_command_mode_field_reads_like_every_other_shortcut_field(make_dialog):
    from ui_qt.widgets.command_settings import CommandShortcutField

    dialog, _store = make_dialog()
    field = CommandShortcutField(dialog)
    assert field.input.placeholderText() == HotkeyCaptureInput().placeholderText()


def test_dictionary_learning_gate_says_where_it_goes(make_dialog):
    dialog, _store = make_dialog()
    _go(dialog, DICTIONARY)
    button = dialog.findChild(QPushButton, "dictionaryLearnTurnOnButton")
    # "&&" draws one "&"; a single one would underline the next letter.
    assert button.text() == "Open Apps && styles"
    button.click()
    assert dialog.rail.current_key() == STYLES
