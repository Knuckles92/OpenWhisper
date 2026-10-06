"""Enter in Settings does the focused field's own job and never switches the view.

Settings saves as you go and has no default action, yet Qt makes the first
auto-default button of a shown QDialog (the header's Basic view button) its
default, which Enter in any field that ignores Return used to click. These
tests need a shown, active window: an unshown dialog has no default button and
hides the bug.
"""
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services import app_styles, dictionary
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs import settings_styles
from ui_qt.dialogs.settings_destinations import (
    CLEANUP_RULES,
    COMMANDS,
    DICTIONARY,
    RECORDING,
    SNIPPETS,
    STYLES,
)

KEYPAD_ENTER = (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
RETURN = (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
CLEANUP_ON = {
    SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free",
}
WORD = {"id": "a", "term": "Ksenia", "starred": False, "heard": [], "learned": False, "new": False}


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


@pytest.fixture(params=["classic", "omarchy"])
def make_dialog(request, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_UI", request.param)
    stacks, dialogs = [], []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED,
                                 SettingsKey.SELECTED_MODEL: "parakeet", **(values or {})})
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
        dialog.resize(1100, 820)
        dialog.show()
        dialog.activateWindow()
        assert QTest.qWaitForWindowActive(dialog)
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.hide()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _go(dialog, key):
    dialog.select_destination(key)
    for _ in range(3):
        QApplication.processEvents()


def _press(widget, key=RETURN):
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    QTest.keyClick(widget, key[0], key[1])
    QApplication.processEvents()


def _type_and_press(widget, text, key=RETURN):
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    QTest.keyClicks(widget, text)
    _press(widget, key)


def _stayed(dialog, store, page_title):
    assert dialog._settings_view == SettingsView.ADVANCED
    assert store.get(SettingsKey.SETTINGS_VIEW) == SettingsView.ADVANCED
    assert dialog.page_title.text() == page_title


def _words(store):
    return [term.term for term in dictionary.load_dictionary(store.load_all_settings())]


@pytest.mark.parametrize("key", [RETURN, KEYPAD_ENTER], ids=["return", "keypad-enter"])
def test_enter_adds_words_one_after_another(make_dialog, key):
    dialog, store = make_dialog()
    _go(dialog, DICTIONARY)

    _type_and_press(dialog.dictionary_term_edit, "Ksenia", key)
    _type_and_press(dialog.dictionary_term_edit, "Oluwaseun", key)

    assert sorted(_words(store)) == ["Ksenia", "Oluwaseun"]
    _stayed(dialog, store, "Dictionary")
    assert dialog.dictionary_term_edit.isVisible()
    assert QApplication.focusWidget() is dialog.dictionary_term_edit


def test_enter_in_sounds_like_adds_the_word(make_dialog):
    dialog, store = make_dialog()
    _go(dialog, DICTIONARY)
    dialog.dictionary_term_edit.setText("Siobhan")

    _type_and_press(dialog.dictionary_heard_edit, "shivon")

    terms = dictionary.load_dictionary(store.load_all_settings())
    assert [(term.term, term.heard) for term in terms] == [("Siobhan", ("shivon",))]
    _stayed(dialog, store, "Dictionary")


def _pick_and_add(picker, text):
    # Typing opens the picker's filtered list; the first Enter takes the
    # match from it and the second is the field's own.
    _type_and_press(picker, text)
    assert not picker.view().isVisible()
    _press(picker)


def test_enter_adds_an_app_to_never_read_from(make_dialog):
    dialog, store = make_dialog()
    _go(dialog, STYLES)

    _pick_and_add(dialog.apps_styles_page.excluded_picker, "Signal")

    assert store.get(SettingsKey.APP_CONTEXT_EXCLUDED_APPS) == ["Signal"]
    _stayed(dialog, store, "Apps & styles")


def test_enter_moves_an_app_to_a_style(make_dialog):
    dialog, store = make_dialog(CLEANUP_ON)
    _go(dialog, STYLES)

    _pick_and_add(dialog.apps_styles_page.override_picker, "Notion")

    assert app_styles.resolve_overrides(store.load_all_settings()) == (("Notion", "email"),)
    _stayed(dialog, store, "Apps & styles")


def test_enter_adds_a_learned_rule(make_dialog, monkeypatch):
    dialog, store = make_dialog(CLEANUP_ON)
    _go(dialog, CLEANUP_RULES)
    title = dialog.page_title.text()
    # Adding a rule polishes it with the cleanup provider, which needs a network.
    polish = MagicMock()
    monkeypatch.setattr(dialog, "_polish_cleanup_rule", polish)

    _type_and_press(dialog.cleanup_rule_input, "Use British spelling")

    polish.assert_called_once_with("Use British spelling")
    _stayed(dialog, store, title)


def _search_box(dialog):
    return dialog._dictionary_page.library.search


def _snippet_trigger(dialog):
    dialog.snippets_panel.new_button.click()
    return dialog.snippets_panel.trigger_edit


def _transform_name(dialog):
    dialog.transforms_panel.new_button.click()
    return dialog.transforms_panel.name_edit


@pytest.mark.parametrize("destination,title,field", [
    (DICTIONARY, "Dictionary", _search_box),
    (SNIPPETS, "Snippets", _snippet_trigger),
    (COMMANDS, "Commands", _transform_name),
    (RECORDING, None, lambda dialog: dialog.max_recordings_spinbox),
])
def test_enter_in_a_field_without_an_action_changes_nothing(make_dialog, destination, title, field):
    dialog, store = make_dialog({SettingsKey.DICTATION_DICTIONARY: [WORD]})
    _go(dialog, destination)
    title = title or dialog.page_title.text()
    widget = field(dialog)
    widget.show()

    _type_and_press(widget, "4")

    _stayed(dialog, store, title)
    assert QApplication.focusWidget() is widget


def test_enter_on_a_focused_button_still_presses_it(make_dialog):
    dialog, store = make_dialog()
    _go(dialog, DICTIONARY)
    dialog.dictionary_term_edit.setText("Kubernetes")

    _press(dialog.dictionary_add_button)

    assert _words(store) == ["Kubernetes"]
    _stayed(dialog, store, "Dictionary")


def test_enter_on_the_advanced_view_button_switches_view(make_dialog):
    dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})

    _press(dialog.view_buttons[SettingsView.ADVANCED])

    assert dialog._settings_view == SettingsView.ADVANCED
    assert store.get(SettingsKey.SETTINGS_VIEW) == SettingsView.ADVANCED


def test_escape_still_closes_settings(make_dialog):
    dialog, _store = make_dialog()
    _go(dialog, DICTIONARY)

    _press(dialog.dictionary_term_edit, (Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))

    assert not dialog.isVisible()
