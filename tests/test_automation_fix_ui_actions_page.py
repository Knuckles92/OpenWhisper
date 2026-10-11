"""Settings → Automate → Actions, after the UI review: one section per finding."""

import itertools
import os
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from services.actions import routing
from services.actions.model import (
    AGENT_ACTION_ID,
    delete_action,
    find_action,
    save_action,
)
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_actions
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import ACTIONS

pytestmark = pytest.mark.qt

ASK_CLAUDE = {
    "id": "a1", "name": "Ask Claude", "kind": "open_link",
    "params": {"template": "claude://claude.ai/new?q={text}"}, "hotkey": "ctrl+alt+c",
    "spoken_names": ["Claude"],
}
AGENT = {
    "id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
    "params": {"agent": "claude_code"}, "hotkey": "ctrl+alt+shift+space",
    "spoken_names": ["Claude Code"], "description": "Coding tasks for Claude Code",
}


@dataclass(frozen=True)
class _Status:
    """routing.RoutingStatus with the ``fix`` field the routing fixes add."""

    enabled: bool
    ready: bool
    message: str
    fix: str = ""


@pytest.fixture(autouse=True)
def routing_status(monkeypatch):
    """Routing's own words, with the ``fix`` the page reads; tests may set ``.fixed``."""
    real = routing.status
    state = MagicMock()
    state.fixed = None

    def status(settings):
        if state.fixed is not None:
            return state.fixed
        found = real(settings)
        fix = ""
        if found.enabled and not found.ready:
            fix = "fast_judgments" if "Fast judgments" in found.message else "actions"
        return _Status(found.enabled, found.ready, found.message, fix)

    monkeypatch.setattr(routing, "status", status)
    return state


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


class _Asked(list):
    answer = QMessageBox.StandardButton.Yes


@pytest.fixture
def questions(monkeypatch):
    """Every question asked; answers come from ``questions.answer``."""
    asked = _Asked()

    def question(_parent, title, text, *_args):
        asked.append((title, text))
        return asked.answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return asked


@pytest.fixture
def agents(monkeypatch):
    """What ``agents.available()`` reports; tests change ``agents.found``."""
    state = MagicMock()
    state.found = {"claude_code": True, "codex": True, "opencode_cli": False}
    monkeypatch.setattr("services.actions.agents.available", lambda: dict(state.found))
    return state


@pytest.fixture
def secrets(monkeypatch):
    """The runtime's signing-secret calls over a dict."""
    from services.actions import runtime

    state = MagicMock()
    state.saved = {}
    numbers = itertools.count(1)

    def ensure(action_id):
        return state.saved.setdefault(action_id, f"whsec_{next(numbers)}")

    def delete(action_id):
        state.saved.pop(action_id, None)

    monkeypatch.setattr(runtime, "ensure_action_secret", ensure, raising=False)
    monkeypatch.setattr(runtime, "delete_action_secret", delete, raising=False)
    return state


@pytest.fixture
def make_dialog(questions, agents):
    stacks, dialogs = [], []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, *, select=True, size=None):
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
        dialog.on_profile_hotkey_capture = MagicMock()
        dialogs.append(dialog)
        if size is not None:
            dialog.resize(*size)
        if select:
            dialog.select_destination(ACTIONS)
        dialog.show()
        pump()
        return dialog, store

    yield build
    questions.answer = QMessageBox.StandardButton.Discard
    for dialog in dialogs:
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def pump(times=6):
    for _ in range(times):
        QApplication.processEvents()


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def stored(store, action_id):
    return find_action(store.load_all_settings(), action_id)


def change_stored(store, action_id, **changes):
    """Change a saved action behind the page's back, as another window would."""
    store.mutate_settings(
        lambda settings: save_action(settings, replace(find_action(settings, action_id), **changes)))


# ---- 1. the agent action's full editor and the Agent hotkey card ----


def test_the_agent_editor_follows_the_card_while_it_has_no_edits(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    card, editor = dialog.actions_agent_card, dialog.actions_panel.editor
    card.more_button.click()
    assert editor.action_id == AGENT_ACTION_ID
    card.enabled_switch.click()
    card.fields.chooser.choices["codex"].click()
    assert editor.agent_fields.chooser.current() == "codex"
    assert editor.description_edit.text() == "Coding tasks for Codex"
    assert not editor.has_unsaved_changes()

    editor.description_edit.setText("Fixes in this repo")
    assert editor.save()
    saved = stored(store, AGENT_ACTION_ID)
    assert (saved.enabled, saved.param("agent"), saved.description) == (
        False, "codex", "Fixes in this repo")
    assert not card.enabled_switch.isChecked()


def test_the_agent_editor_keeps_its_edits_when_the_card_changes(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    card, editor = dialog.actions_agent_card, dialog.actions_panel.editor
    card.more_button.click()
    editor.description_edit.setText("My words")
    card.enabled_switch.click()
    card.fields.mode_bar.buttons[1].click()
    # The card's change shows in the editor, and your edit is still there.
    assert editor.agent_fields.mode_bar.currentIndex() == 1
    assert editor.description_edit.text() == "My words"
    assert editor.has_unsaved_changes()
    assert editor.save()
    saved = stored(store, AGENT_ACTION_ID)
    assert (saved.enabled, saved.param("mode"), saved.description) == (False, "ask", "My words")


def test_saving_an_edit_keeps_what_changed_since_it_was_opened(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.edit("a1")
    panel.editor.name_edit.setText("Claude chat")
    change_stored(store, "a1", enabled=False, hotkey="ctrl+alt+j")
    assert panel.editor.save()
    saved = stored(store, "a1")
    assert (saved.name, saved.enabled, saved.hotkey) == ("Claude chat", False, "ctrl+alt+j")


def test_removing_the_agent_hotkey_closes_its_editor_for_good(make_dialog, questions):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    card, panel = dialog.actions_agent_card, dialog.actions_panel
    card.more_button.click()
    panel.editor.description_edit.setText("edit before remove")
    questions.answer = QMessageBox.StandardButton.Yes
    card.remove()
    assert panel.mode == "list" and not panel.editor.isVisible()
    assert panel.notice.text() == "Agent hotkey was removed."
    # Leaving the page has no draft left to save, so nothing comes back.
    assert settings_actions.save_drafts(dialog, ask=True)
    assert stored(store, AGENT_ACTION_ID) is None
    assert card.action is None


def test_an_edit_of_an_action_removed_elsewhere_isnt_saved_back(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    editor = dialog.actions_panel.editor
    dialog.actions_panel.edit("a1")
    editor.name_edit.setText("Claude chat")
    store.mutate_settings(lambda settings: delete_action(settings, "a1"))
    assert not editor.save()
    assert stored(store, "a1") is None
    assert editor.message.text() == (
        "Ask Claude no longer exists, so there's nothing to save these changes to.")
