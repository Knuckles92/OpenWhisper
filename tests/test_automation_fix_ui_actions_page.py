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


# ---- 2. "More options" while another action is being edited ----


def test_more_options_asks_before_leaving_an_unsaved_edit(make_dialog, questions):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [AGENT, ASK_CLAUDE]})
    card, panel = dialog.actions_agent_card, dialog.actions_panel
    editor = panel.editor
    panel.edit("a1")
    editor.name_edit.setText("Claude chat")

    questions.answer = QMessageBox.StandardButton.Cancel
    card.more_button.click()
    assert questions == [("Unsaved action", "Save your changes to Claude chat?")]
    assert (editor.action_id, editor.name_edit.text()) == ("a1", "Claude chat")

    questions.answer = QMessageBox.StandardButton.Discard
    card.more_button.click()
    assert editor.action_id == AGENT_ACTION_ID
    assert stored(store, "a1").name == "Ask Claude"

    panel.edit("a1")
    editor.name_edit.setText("Claude chat")
    questions.answer = QMessageBox.StandardButton.Save
    card.more_button.click()
    assert stored(store, "a1").name == "Claude chat"
    assert editor.action_id == AGENT_ACTION_ID and panel.mode == "editor"


def test_more_options_keeps_an_edit_it_cant_save(make_dialog, questions):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [AGENT, ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.edit("a1")
    panel.editor.template_edit.setText("")
    questions.answer = QMessageBox.StandardButton.Save
    dialog.actions_agent_card.more_button.click()
    assert panel.editor.action_id == "a1"
    assert panel.editor.message.text() == "Enter the link to open."
    assert stored(store, "a1").param("template") == "claude://claude.ai/new?q={text}"


def test_more_options_on_the_open_agent_editor_keeps_its_edits(make_dialog, questions):
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    card, editor = dialog.actions_agent_card, dialog.actions_panel.editor
    card.more_button.click()
    editor.description_edit.setText("Mine")
    card.more_button.click()
    assert questions == []
    assert editor.description_edit.text() == "Mine"


def test_replacing_a_new_webhook_drops_the_secret_it_made(make_dialog, questions, secrets,
                                                         monkeypatch):
    from ui_qt.widgets import action_editor

    copied = []
    monkeypatch.setattr(action_editor, "copy_text", copied.append)
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    draft_id = panel.editor.action_id
    panel.editor.copy_secret()
    assert wait_for(lambda: copied)
    assert draft_id in secrets.saved
    # Copying a secret changes nothing in the draft, so nothing is asked.
    dialog.actions_agent_card.more_button.click()
    assert questions == []
    assert panel.editor.action_id == AGENT_ACTION_ID
    assert wait_for(lambda: draft_id not in secrets.saved)


# ---- 3. Route by voice's status keeps up ----


@pytest.fixture
def typesafe_key():
    from services import credentials

    credentials.store().set("TYPESAFE_API_KEY", "ts_fake")


def routing_words(store) -> str:
    return routing.status(store.load_all_settings()).message


def test_route_by_voice_status_follows_the_actions(make_dialog, typesafe_key):
    dialog, store = make_dialog({SettingsKey.VOICE_ROUTING_ENABLED: True,
                                 SettingsKey.TYPESAFE_ENABLED: True})
    status, panel = dialog.actions_routing_status.message, dialog.actions_panel
    no_actions = status.text()
    assert no_actions == routing_words(store)
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("claude-chat").preset)
    assert panel.editor.save()
    assert status.text() == routing_words(store) != no_actions
    ready = status.text()
    panel.set_enabled(panel.list.cards[0].action.id, False)
    assert status.text() == routing_words(store) not in (no_actions, ready)


def test_route_by_voice_status_follows_the_agent_card(make_dialog, typesafe_key):
    dialog, store = make_dialog({SettingsKey.VOICE_ROUTING_ENABLED: True,
                                 SettingsKey.TYPESAFE_ENABLED: True})
    status = dialog.actions_routing_status.message
    no_actions = status.text()
    dialog.actions_agent_card.set_up()
    dialog.actions_agent_card.cancel_capture()
    assert status.text() == routing_words(store) != no_actions


def test_route_by_voice_status_is_read_again_when_the_page_returns(make_dialog, typesafe_key):
    from ui_qt.dialogs.settings_destinations import GENERAL

    dialog, store = make_dialog({SettingsKey.VOICE_ROUTING_ENABLED: True,
                                 SettingsKey.ACTIONS: [ASK_CLAUDE]})
    status = dialog.actions_routing_status
    needs_fast = status.message.text()
    assert status.state == "attention"
    dialog.select_destination(GENERAL)
    # Turned on elsewhere, as the Fast judgments link leads you to.
    store.save_setting(SettingsKey.TYPESAFE_ENABLED, True)
    dialog.select_destination(ACTIONS)
    pump()
    assert status.message.text() == routing_words(store) != needs_fast
    assert status.state == "ready"


# ---- 4/5. Route by voice's status, links and privacy line ----


@pytest.mark.parametrize("fix,linked", [
    ("fast_judgments", True), ("actions", False), ("trigger", False), ("", False)])
def test_the_fast_judgments_link_shows_only_when_they_are_the_fix(make_dialog, routing_status,
                                                                  fix, linked):
    routing_status.fixed = _Status(True, False, "Route by voice needs something.", fix)
    dialog, _store = make_dialog({SettingsKey.VOICE_ROUTING_ENABLED: True})
    status = dialog.actions_routing_status
    assert status.state == "attention"
    assert status.link.isVisible() is linked


def test_route_by_voice_off_has_no_link(make_dialog, routing_status):
    routing_status.fixed = _Status(False, False, "Route by voice is off.")
    dialog, _store = make_dialog()
    assert dialog.actions_routing_status.state == "off"
    assert not dialog.actions_routing_status.link.isVisible()


def test_the_privacy_line_says_what_each_way_of_routing_sends(make_dialog, routing_status):
    routing_status.fixed = _Status(True, False, "Route by voice needs Fast judgments.",
                                   "fast_judgments")
    dialog, _store = make_dialog({SettingsKey.VOICE_ROUTING_ENABLED: True})
    privacy = dialog.actions_routing_privacy
    assert privacy.isVisible()
    assert privacy.text() == (
        "Everything you record with the Route by voice shortcut is sent to TypeSafe or "
        "OpenRouter to choose the action.")
    dialog.actions_routing_dictation_tile.checkbox.click()
    assert privacy.text() == (
        "Everything you record with the Route by voice shortcut, and normal dictations that "
        "start with an action's name, are sent to TypeSafe or OpenRouter to choose the action.")
    # Ready, routing's own words say what is sent.
    routing_status.fixed = _Status(True, True, "Route by voice is on.")
    settings_actions.refresh(dialog)
    assert not privacy.isVisible()


def test_route_by_voice_has_no_caption_repeating_its_tile(make_dialog):
    from PyQt6.QtWidgets import QLabel

    dialog, _store = make_dialog()
    page = dialog._pages[ACTIONS]
    captions = [label.text() for label in page.findChildren(QLabel, "settingsTileGroupTitle")
                if label.isVisible()]
    assert "Route by voice" not in captions
    assert dialog.actions_routing_tile.title_label.text() == "Route by voice"


# ---- 6/7. What "Try it" shows ----


def test_the_agent_card_shows_a_quick_answer(make_dialog):
    from services.actions.runtime import ActionResult

    agent = {**AGENT, "params": {"agent": "claude_code", "mode": "ask"}}
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [agent]})
    dialog.on_try_action = lambda _action, _text, done: done(
        ActionResult(True, "Asked Claude Code", "Hello! Nice to meet you."))
    card = dialog.actions_agent_card
    card.try_button.click()
    assert card.message.text() == "Asked Claude Code\nHello! Nice to meet you."
    assert card.message.property("tone") == "ok"


def test_try_it_shows_only_the_start_of_a_long_output(make_dialog):
    from services.actions.runtime import ActionResult

    dialog, _store = make_dialog()
    outputs = ["line of output\n" * 4000, "x" * 5000]
    dialog.on_try_action = lambda _action, _text, done: done(ActionResult(True, "", outputs.pop(0)))
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    editor = panel.editor
    editor.try_it()
    assert editor.try_result.text() == "It worked.\n" + "\n".join(["line of output"] * 6) + "…"
    editor.try_it()
    assert editor.try_result.text() == "It worked.\n" + "x" * 500 + "…"


def test_shortened_keeps_what_fits_as_it_is():
    from ui_qt.widgets.action_copy import shortened

    assert shortened("  one\r\ntwo  ") == "one\ntwo"
    assert shortened("\n".join(str(n) for n in range(7))) == "0\n1\n2\n3\n4\n5…"


# ---- 8. An editor opens with its top in view ----


def _in_view(dialog, widget) -> bool:
    from PyQt6.QtCore import QPoint

    viewport = dialog._page_scrolls[ACTIONS].viewport()
    top = widget.mapTo(viewport, QPoint(0, 0)).y()
    return 0 <= top and top + widget.height() <= viewport.height()


@pytest.mark.parametrize("opened", ["more options", "preset"])
def test_an_editor_opens_with_its_name_field_in_view(make_dialog, opened):
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [AGENT, ASK_CLAUDE]}, size=(1100, 640))
    panel = dialog.actions_panel
    if opened == "more options":
        dialog.actions_agent_card.more_button.click()
    else:
        panel.add_action()
        pump()
        panel.start_from_preset(panel.gallery.tile("webhook").preset)
    pump(12)
    editor = panel.editor
    area = dialog._page_scrolls[ACTIONS]
    assert editor.height() > area.viewport().height()  # Too tall to show whole.
    assert _in_view(dialog, editor.title_label)
    assert _in_view(dialog, editor.name_edit)


# ---- 9. The working folder is for sessions only ----


def test_the_working_folder_shows_only_for_a_session(make_dialog):
    agent = {**AGENT, "params": {"agent": "claude_code", "folder": os.getcwd()}}
    dialog, store = make_dialog({SettingsKey.ACTIONS: [agent]})
    fields = dialog.actions_agent_card.fields
    assert fields.folder.isVisible()
    fields.mode_bar.buttons[1].click()
    assert not fields.folder.isVisible()
    # Kept for when you switch back to a session.
    assert stored(store, AGENT_ACTION_ID).param("folder") == os.getcwd()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("quick-answer").preset)
    assert not panel.editor.agent_fields.folder.isVisible()


# ---- 14. Where a session opens says whether the agent waits ----


def test_opens_in_says_a_terminal_starts_right_away_and_an_app_waits(make_dialog):
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [AGENT]})
    fields = dialog.actions_agent_card.fields
    assert fields.mode_hint.text() == "Claude Code opens in a new session with your words."
    hints = []
    for button in fields.surface_bar.buttons:
        button.click()
        hints.append(fields.surface_hint.text())
    assert hints == [
        "The agent's app when it's installed, with your words filled in for you to send; "
        "otherwise a new terminal, where the agent starts on them right away.",
        "The agent's app opens with your words filled in, ready for you to send.",
        "A new terminal opens, and the agent starts on your words right away.",
    ]
