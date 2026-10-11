"""Settings → Automate → Actions: the agent card, your actions, the editor, Route by voice."""

import itertools
import os
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from services.actions.model import (
    AGENT_ACTION_ID,
    ActionKind,
    find_action,
    load_actions,
)
from services.actions.runtime import ActionResult
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_actions
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import ACTIONS, GENERAL
from ui_qt.widgets import action_editor

pytestmark = pytest.mark.qt

ASK_CLAUDE = {
    "id": "a1", "name": "Ask Claude", "kind": "open_link",
    "params": {"template": "claude://claude.ai/new?q={text}"}, "hotkey": "ctrl+alt+c",
    "spoken_names": ["Claude"],
}
HOOK = {
    "id": "h1", "name": "Home Assistant", "kind": "webhook",
    "params": {"url": "http://homeassistant.local/api/webhook/voice", "output": "notice"},
}


@dataclass(frozen=True)
class _RoutingStatus:
    """A routing.RoutingStatus: ``fix`` names what Route by voice still needs."""

    enabled: bool
    ready: bool
    message: str
    fix: str = ""


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
    """The runtime's signing-secret calls over a dict, noting the thread each ran on."""
    from services.actions import runtime

    state = MagicMock()
    state.saved = {}
    state.threads = []
    numbers = itertools.count(1)

    def ensure(action_id):
        state.threads.append(threading.current_thread())
        return state.saved.setdefault(action_id, f"whsec_{next(numbers)}")

    def delete(action_id):
        state.threads.append(threading.current_thread())
        state.saved.pop(action_id, None)

    monkeypatch.setattr(runtime, "ensure_action_secret", ensure, raising=False)
    monkeypatch.setattr(runtime, "delete_action_secret", delete, raising=False)
    return state


@pytest.fixture
def make_dialog(questions, agents):
    stacks, dialogs = [], []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, *, select=True):
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


def actions_in(store):
    return load_actions(store.load_all_settings())


def changes(dialog):
    return [call.args[0] for call in dialog.on_settings_changed.call_args_list]


# ---- building ----


def test_the_page_builds_only_when_shown(make_dialog):
    dialog, _store = make_dialog(select=False)
    assert "actions_agent_card" not in dialog.__dict__
    dialog.select_destination(ACTIONS)
    for attr in settings_actions.CONTROL_ATTRS:
        assert attr in dialog.__dict__, attr
    for attr, title, _copy in settings_actions.SEARCH_FIELDS:
        assert settings_metadata.CONTROL_DESTINATIONS[attr] == ACTIONS
        assert title


@pytest.mark.parametrize("actions,expected", [
    ([], "None yet"),
    ([{"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent"}], "Agent hotkey set"),
    ([ASK_CLAUDE], "1 action"),
    ([{"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent"}, ASK_CLAUDE, HOOK],
     "Agent hotkey set · 2 actions"),
])
def test_rail_value_names_the_agent_hotkey_and_counts_the_rest(actions, expected):
    assert settings_actions.rail_value({SettingsKey.ACTIONS: actions}) == expected


# ---- your actions ----


def test_an_empty_page_invites_the_first_action(make_dialog):
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    assert panel.list.empty.isVisible()
    assert not panel.add_button.isVisible()
    panel.list.empty_add.click()
    assert panel.gallery.isVisible()
    assert "agent" not in [tile.preset.id for tile in panel.gallery.tiles]


def test_adding_from_a_preset_saves_it_and_tells_the_app(make_dialog):
    dialog, store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    tile = panel.gallery.tile("claude-chat")
    tile.clicked.emit(tile.preset)
    editor = panel.editor
    assert editor.isVisible()
    assert editor.name_edit.text() == "Ask Claude"
    assert editor.template_edit.text() == "claude://claude.ai/new?q={text}"
    assert "claude://claude.ai/new?q=What%27s%20on" in editor.preview.text()
    editor.spoken_edit.setText("Claude, ask Claude")
    editor.shortcut.captured.emit("ctrl+alt+c")
    assert editor.save()

    [saved] = actions_in(store)
    assert (saved.name, saved.kind, saved.hotkey) == ("Ask Claude", ActionKind.OPEN_LINK, "ctrl+alt+c")
    assert saved.spoken_names == ("Claude", "ask Claude")
    assert saved.param("template") == "claude://claude.ai/new?q={text}"
    assert changes(dialog) == ["actions"]
    assert panel.list.isVisible() and not editor.isVisible()
    assert panel.list.card(saved.id) is not None
    assert panel.notice.text().startswith("Saved Ask Claude. Press ")
    assert settings_actions.rail_value(store.load_all_settings()) == "1 action"


def test_adding_the_same_preset_twice_gets_a_new_name(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("claude-chat").preset)
    assert panel.editor.name_edit.text() == "Ask Claude 2"
    assert panel.editor.save()
    assert [a.name for a in actions_in(store)] == ["Ask Claude", "Ask Claude 2"]


def test_the_web_preset_carries_its_caveat(make_dialog):
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("chatgpt-web").preset)
    assert panel.editor.preset_note.isVisible()
    assert "doesn't document" in panel.editor.preset_note.text()


def test_reasons_an_action_cant_be_saved_show_in_the_editor(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("own-link").preset)
    editor = panel.editor
    assert not editor.save()
    assert editor.message.text() == "Enter the link to open."
    editor.template_edit.setText("javascript:alert({text})")
    assert editor.preview.text() == "Links that start with javascript: can't be used."
    assert not editor.save()
    assert editor.message.text() == "Links that start with javascript: can't be used."
    editor.template_edit.setText("https://www.perplexity.ai/search")
    assert editor.preview.text() == "Add {text} where your words should go."
    editor.template_edit.setText("https://www.perplexity.ai/search?q={text}")
    editor.name_edit.setText("ask claude")
    assert not editor.save()
    assert editor.message.text() == "An action with that name already exists."
    editor.name_edit.setText("Perplexity")
    editor.spoken_edit.setText("Claude")
    assert not editor.save()
    assert editor.message.text() == '"Claude" already names Ask Claude. Choose another name.'
    assert [a.name for a in actions_in(store)] == ["Ask Claude"]
    assert changes(dialog) == []


def test_a_taken_shortcut_is_refused_with_its_owner(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    editor = panel.editor
    editor.shortcut.captured.emit("ctrl+alt+k")
    assert editor.shortcut.hotkey == "ctrl+alt+k"
    editor.shortcut.input.begin_capture()
    editor.shortcut.input.cancel_capture()
    editor.shortcut.captured.emit("alt+ctrl+c")
    assert editor.message.text() == "That shortcut is already used by Ask Claude. Choose another."
    assert editor.shortcut.hotkey == "ctrl+alt+k"
    editor.shortcut.captured.emit("ctrl+alt+m")
    assert editor.message.text() == "That shortcut is already used by Minimize to tray. Choose another."
    editor.shortcut.captured.emit("ctrl+alt+j")
    assert editor.message.text() == ""
    assert editor.save()
    assert find_action(store.load_all_settings(), editor.action_id).hotkey == "ctrl+alt+j"


def test_capturing_a_shortcut_pauses_the_app_hotkeys(make_dialog):
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    panel.editor.shortcut.input.begin_capture()
    panel.editor.shortcut.input.cancel_capture()
    assert [c.args[0] for c in dialog.on_profile_hotkey_capture.call_args_list] == [True, False]


def test_the_enabled_switch_saves_at_once(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    card = dialog.actions_panel.list.card("a1")
    card.switch.click()
    assert find_action(store.load_all_settings(), "a1").enabled is False
    assert card.property("off") is True
    assert changes(dialog) == ["actions"]
    card.switch.click()
    assert find_action(store.load_all_settings(), "a1").enabled is True


def test_editing_keeps_the_id_and_cancel_keeps_the_saved_action(make_dialog):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE]})
    panel = dialog.actions_panel
    panel.list.card("a1").edit_link.click()
    editor = panel.editor
    assert editor.title_label.text() == "Edit Ask Claude"
    editor.name_edit.setText("Claude chat")
    editor.cancel()
    assert find_action(store.load_all_settings(), "a1").name == "Ask Claude"
    panel.edit("a1")
    editor.name_edit.setText("Claude chat")
    assert editor.save()
    assert [(a.id, a.name) for a in actions_in(store)] == [("a1", "Claude chat")]


def test_deleting_asks_first_and_forgets_a_webhooks_secret(make_dialog, questions, secrets):
    dialog, store = make_dialog({SettingsKey.ACTIONS: [ASK_CLAUDE, HOOK]})
    secrets.saved["h1"] = "whsec_old"
    panel = dialog.actions_panel
    questions.answer = QMessageBox.StandardButton.Cancel
    panel.list.card("h1").delete_link.click()
    assert len(actions_in(store)) == 2
    questions.answer = QMessageBox.StandardButton.Yes
    panel.list.card("h1").delete_link.click()
    assert [a.id for a in actions_in(store)] == ["a1"]
    assert wait_for(lambda: "h1" not in secrets.saved)
    assert threading.main_thread() not in secrets.threads
    assert panel.notice.text() == "Deleted Home Assistant."
    assert changes(dialog) == ["actions"]
    panel.list.card("a1").delete_link.click()
    pump()
    assert len(secrets.threads) == 1  # Only a webhook has a secret to forget.


# ---- the editor's kinds ----


def test_shaping_offers_profiles_and_transforms(make_dialog):
    profile = {"id": "p1", "name": "Ticket", "instructions": "As a ticket."}
    dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [profile]})
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    shaping = panel.editor.shaping
    assert not shaping.combo.isVisible()
    shaping.bar.buttons[2].click()
    assert shaping.combo.isVisible() and shaping.combo.currentText() == "Ticket"
    assert shaping.value() == "profile:p1"
    shaping.bar.buttons[3].click()
    assert shaping.value().startswith("transform:")
    shaping.bar.buttons[1].click()
    assert shaping.value() == "raw"
    shaping.bar.buttons[2].click()
    assert panel.editor.save()
    assert actions_in(store)[0].shaping == "profile:p1"


def test_a_webhook_secret_is_copied_and_dropped_with_an_unsaved_draft(make_dialog, monkeypatch,
                                                                      secrets):
    copied = []
    monkeypatch.setattr(action_editor, "copy_text", copied.append)
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    editor = panel.editor
    draft_id = editor.action_id
    editor.copy_secret()
    assert not editor.copy_secret_button.isEnabled()
    assert wait_for(lambda: copied)
    assert copied == [secrets.saved[draft_id]]
    assert editor.message.text() == "Signing secret copied. Paste it into your service."
    assert editor.copy_secret_button.isEnabled()
    assert threading.main_thread() not in secrets.threads
    editor.cancel()
    assert wait_for(lambda: draft_id not in secrets.saved)

    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    editor.url_edit.setText("https://example.com/hook")
    saved_id = editor.action_id
    editor.copy_secret()
    assert wait_for(lambda: len(copied) == 2)
    assert editor.save()
    pump()
    assert secrets.saved[saved_id] == copied[-1]


def test_a_new_secret_replaces_the_old_one_after_asking(make_dialog, monkeypatch, questions,
                                                       secrets):
    copied = []
    monkeypatch.setattr(action_editor, "copy_text", copied.append)
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [HOOK]})
    secrets.saved["h1"] = "whsec_old"
    panel = dialog.actions_panel
    panel.edit("h1")
    questions.answer = QMessageBox.StandardButton.Cancel
    panel.editor.new_secret()
    pump()
    assert secrets.saved["h1"] == "whsec_old" and copied == []
    questions.answer = QMessageBox.StandardButton.Yes
    panel.editor.new_secret()
    assert wait_for(lambda: copied)
    assert copied == [secrets.saved["h1"]] and copied[0] != "whsec_old"
    assert panel.editor.message.text() == "New signing secret copied. Update your service with it."
    panel.editor.cancel()
    pump()
    # A saved action's secret stays when its edit is cancelled.
    assert secrets.saved["h1"] == copied[0]


def test_a_secret_the_store_cant_give_is_explained(make_dialog, monkeypatch):
    from services.actions import runtime

    def refuse(_action_id):
        raise RuntimeError("Windows Credential Manager is locked")

    monkeypatch.setattr(runtime, "ensure_action_secret", refuse, raising=False)
    monkeypatch.setattr(runtime, "delete_action_secret", lambda _id: None, raising=False)
    monkeypatch.setattr(action_editor, "copy_text", MagicMock())
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    panel.editor.copy_secret()
    assert wait_for(lambda: panel.editor.copy_secret_button.isEnabled())
    assert panel.editor.message.text() == (
        "Couldn't get the signing secret: Windows Credential Manager is locked")
    action_editor.copy_text.assert_not_called()


def test_a_plain_http_address_off_this_network_is_flagged(make_dialog):
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    editor = panel.editor
    editor.url_edit.setText("http://example.com/hook")
    assert editor.url_note.isVisible()
    editor.url_edit.setText("http://192.168.1.20:5678/hook")
    assert not editor.url_note.isVisible()
    editor.url_edit.setText("https://example.com/hook")
    assert not editor.url_note.isVisible()


def test_a_program_needs_its_full_path_and_keeps_arguments_one_per_line(make_dialog, tmp_path,
                                                                        monkeypatch):
    dialog, store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("program").preset)
    editor = panel.editor
    assert not editor.save()
    assert editor.message.text() == "Choose the program to run."
    program = str(tmp_path / "journal.exe")
    monkeypatch.setattr("ui_qt.widgets.action_fields.choose_program", lambda *_args: program)
    editor.program_field.choose_button.click()
    assert editor.program_field.path() == os.path.normpath(program)
    editor.args_edit.setPlainText("--append\n\n  --title My day  \n")
    editor.timeout_spin.setValue(45)
    editor.program_output.set_value("scratchpad")
    assert editor.save()
    saved = actions_in(store)[0]
    assert saved.param("args") == ["--append", "--title My day"]
    assert (saved.param("timeout_s"), saved.param("output")) == (45, "scratchpad")


def test_an_agent_action_keeps_its_agent_choices(make_dialog):
    dialog, store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("quick-answer").preset)
    fields = panel.editor.agent_fields
    assert not fields.session_box.isVisible() and fields.answer_box.isVisible()
    assert not fields.chooser.choices["opencode_cli"].isEnabled()
    fields.chooser.choices["codex"].click()
    fields.output_combo.set_value("notice")
    assert panel.editor.save()
    saved = actions_in(store)[0]
    assert (saved.param("agent"), saved.param("mode"), saved.param("output")) == (
        "codex", "ask", "notice")


# ---- try it ----


def test_try_it_shows_what_the_runtime_answered(make_dialog):
    dialog, _store = make_dialog()
    calls = []

    def try_action(action, sample, done):
        calls.append((action.name, sample))
        done(ActionResult(True, "Opened Claude.", None))

    dialog.on_try_action = try_action
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("claude-chat").preset)
    editor = panel.editor
    editor.sample_edit.setText("hello there")
    editor.try_it()
    pump()
    assert calls == [("Ask Claude", "hello there")]
    assert editor.try_result.text() == "Opened Claude."
    assert editor.try_result.property("tone") == "ok"
    assert editor.try_button.isEnabled()


def test_try_it_waits_for_an_answer_from_another_thread(make_dialog):
    dialog, _store = make_dialog()
    release = threading.Event()

    def try_action(_action, _sample, done):
        def worker():
            release.wait(5)
            done(ActionResult(False, "No app opens claude: links.", None))

        threading.Thread(target=worker, daemon=True).start()

    dialog.on_try_action = try_action
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("claude-chat").preset)
    editor = panel.editor
    editor.try_it()
    assert editor.try_result.text() == "Trying…"
    assert not editor.try_button.isEnabled()
    release.set()
    assert wait_for(lambda: editor.try_result.text() == "No app opens claude: links.")
    assert editor.try_result.property("tone") == "error"
    assert editor.try_button.isEnabled()


def test_try_it_explains_a_draft_it_cant_run(make_dialog):
    dialog, _store = make_dialog()
    dialog.on_try_action = MagicMock()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("webhook").preset)
    panel.editor.try_it()
    assert panel.editor.try_result.text() == "Enter the address to send to."
    dialog.on_try_action.assert_not_called()


def test_try_it_without_the_app_says_so(make_dialog):
    dialog, _store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    panel.editor.try_it()
    pump()
    assert panel.editor.try_result.text() == "Trying an action works while OpenWhisper is running."


# ---- drafts ----


def test_an_edited_draft_is_saved_on_leaving_and_an_untouched_one_is_not(make_dialog):
    dialog, store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("copy").preset)
    dialog.select_destination(GENERAL)
    assert actions_in(store) == []
    dialog.select_destination(ACTIONS)
    panel.editor.name_edit.setText("Stash it")
    dialog.select_destination(GENERAL)
    assert [a.name for a in actions_in(store)] == ["Stash it"]


def test_an_invalid_draft_asks_before_it_is_discarded(make_dialog, questions):
    dialog, store = make_dialog()
    panel = dialog.actions_panel
    panel.add_action()
    panel.start_from_preset(panel.gallery.tile("own-link").preset)
    panel.editor.name_edit.setText("Search")
    questions.answer = QMessageBox.StandardButton.Cancel
    assert settings_actions.save_drafts(dialog, ask=True) is False
    assert "Enter the link to open." in questions[-1][1]
    questions.answer = QMessageBox.StandardButton.Discard
    assert settings_actions.save_drafts(dialog, ask=True) is True
    assert panel.list.isVisible()
    assert actions_in(store) == []


# ---- the agent hotkey card ----


def test_the_agent_card_sets_up_the_agent_action(make_dialog):
    dialog, store = make_dialog()
    card = dialog.actions_agent_card
    assert card.setup_button.isEnabled()
    assert not card.fields.details.isVisible()
    assert card.fields.chooser.current() == "claude_code"
    card.fields.chooser.choices["codex"].click()
    card.setup_button.click()
    action = find_action(store.load_all_settings(), AGENT_ACTION_ID)
    assert (action.kind, action.param("agent"), action.param("mode")) == (
        ActionKind.AGENT, "codex", "handoff")
    assert action.spoken_names == ("Codex",)
    assert card.fields.details.isVisible() and card.shortcut_box.isVisible()
    assert card.shortcut.input._capturing
    card.shortcut.input.cancel_capture()
    card.shortcut.captured.emit("ctrl+alt+a")
    assert find_action(store.load_all_settings(), AGENT_ACTION_ID).hotkey == "ctrl+alt+a"
    assert card.message.text().startswith("Press ")
    assert "actions" in changes(dialog)
    assert settings_actions.rail_value(store.load_all_settings()) == "Agent hotkey set"
    # The agent action has its own card, not one in the list.
    assert dialog.actions_panel.list.cards == []


def test_agent_card_choices_save_as_they_change(make_dialog):
    agent = {"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
             "params": {"agent": "claude_code"}, "spoken_names": ["Claude Code"],
             "description": "Coding tasks for Claude Code"}
    dialog, store = make_dialog({SettingsKey.ACTIONS: [agent]})
    card = dialog.actions_agent_card
    fields = card.fields
    fields.mode_bar.buttons[1].click()
    saved = find_action(store.load_all_settings(), AGENT_ACTION_ID)
    assert saved.param("mode") == "ask"
    assert card.description_label.text() == "Asks Claude Code and shows the answer in the Scratchpad."
    fields.chooser.choices["codex"].click()
    saved = find_action(store.load_all_settings(), AGENT_ACTION_ID)
    # The name you say follows the agent, since it was never changed by hand.
    assert (saved.param("agent"), saved.spoken_names) == ("codex", ("Codex",))
    assert saved.description == "Coding tasks for Codex"
    fields.selection_row.switch.click()
    assert find_action(store.load_all_settings(), AGENT_ACTION_ID).param("include_selection")
    # App awareness is on by default, so there is nothing to point at.
    assert not fields.selection_note.isVisible()
    card.enabled_switch.click()
    assert find_action(store.load_all_settings(), AGENT_ACTION_ID).enabled is False


def test_agent_selection_points_at_app_awareness_when_it_is_off(make_dialog):
    agent = {"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
             "params": {"agent": "claude_code", "include_selection": True}}
    dialog, _store = make_dialog({SettingsKey.ACTIONS: [agent],
                                  SettingsKey.APP_CONTEXT_ENABLED: False})
    fields = dialog.actions_agent_card.fields
    assert fields.selection_note.isVisible()
    fields.styles_link.click()
    assert dialog.rail.current_key() == "styles"


def test_agent_card_looks_for_agents_then_says_none_were_found(make_dialog, agents):
    agents.found = {}
    dialog, _store = make_dialog()
    card = dialog.actions_agent_card
    # The conftest scan finds nothing, the way an empty computer does.
    assert wait_for(lambda: card.status.text() == "None found — install one, or use another action.")
    assert not card.setup_button.isEnabled()
    assert card.look_again.isVisible()
    agents.found = {"claude_code": True}
    card.look_again.click()
    assert card.status.text() == "Looking for Claude Code, Codex or OpenCode…"
    assert wait_for(lambda: card.setup_button.isEnabled())
    assert card.fields.chooser.choices["claude_code"].detail_label.text() == "Found"
    assert not card.status_row.isVisible()


def test_opencode_sessions_open_in_a_terminal(make_dialog, agents):
    agents.found = {"claude_code": True, "codex": False, "opencode_cli": True}
    agent = {"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
             "params": {"agent": "claude_code", "surface": "app"}}
    dialog, store = make_dialog({SettingsKey.ACTIONS: [agent]})
    fields = dialog.actions_agent_card.fields
    app = fields.surface_bar.buttons[1]
    assert app.isEnabled() and app.isChecked()
    fields.chooser.choices["opencode_cli"].click()
    saved = find_action(store.load_all_settings(), AGENT_ACTION_ID)
    assert (saved.param("agent"), saved.param("surface")) == ("opencode_cli", "auto")
    assert not app.isEnabled()
    assert fields.surface_hint.text() == (
        "OpenCode has no app, so it opens in a new terminal and starts on your words right away.")


def test_more_options_opens_the_full_editor_on_the_agent_action(make_dialog):
    agent = {"id": AGENT_ACTION_ID, "name": "Agent hotkey", "kind": "agent",
             "params": {"agent": "claude_code"}}
    dialog, store = make_dialog({SettingsKey.ACTIONS: [agent]})
    dialog.actions_agent_card.more_button.click()
    editor = dialog.actions_panel.editor
    assert editor.isVisible() and editor.action_id == AGENT_ACTION_ID
    editor.description_edit.setText("Fixes in this repo")
    assert editor.save()
    assert find_action(store.load_all_settings(), AGENT_ACTION_ID).description == "Fixes in this repo"
    assert dialog.actions_agent_card.action.description == "Fixes in this repo"


# ---- route by voice ----


def test_routing_switches_save_and_gate_each_other(make_dialog):
    dialog, store = make_dialog()
    routing, on_dictation = dialog.actions_routing_tile, dialog.actions_routing_dictation_tile
    assert not on_dictation.isEnabled()
    routing.checkbox.click()
    assert store.load_all_settings()[SettingsKey.VOICE_ROUTING_ENABLED] is True
    assert on_dictation.isEnabled()
    on_dictation.checkbox.click()
    assert store.load_all_settings()[SettingsKey.VOICE_ROUTING_ON_DICTATION] is True
    routing.checkbox.click()
    assert store.load_all_settings()[SettingsKey.VOICE_ROUTING_ENABLED] is False
    assert not on_dictation.isEnabled()


def test_routing_status_shows_routings_own_words_and_links_to_its_fix(make_dialog, monkeypatch):
    from services.actions import routing

    monkeypatch.setattr(routing, "status", lambda settings: _RoutingStatus(
        False, False, "Route by voice is off."))
    dialog, _store = make_dialog()
    status = dialog.actions_routing_status
    assert status.message.text() == "Route by voice is off."
    assert status.state == "off" and not status.link.isVisible()
    assert dialog.actions_routing_privacy.isVisible()
    monkeypatch.setattr(routing, "status", lambda settings: _RoutingStatus(
        True, False, "Turn on Fast judgments to route by voice.", routing.FIX_FAST_JUDGMENTS))
    dialog.actions_routing_tile.checkbox.click()
    assert status.message.text() == "Turn on Fast judgments to route by voice."
    assert status.state == "attention" and status.link.isVisible()
    status.link.click()
    assert dialog.rail.current_key() == "meeting_fast"
    monkeypatch.setattr(routing, "status", lambda settings: _RoutingStatus(
        True, True, "Ready. Words that name an action go to TypeSafe to choose it."))
    dialog.select_destination(ACTIONS)
    settings_actions.refresh(dialog)
    pump()
    assert status.state == "ready" and not status.link.isVisible()
    assert status.message.text() == "Ready. Words that name an action go to TypeSafe to choose it."
    # The ready message carries the privacy sentence itself.
    assert not dialog.actions_routing_privacy.isVisible()


def test_the_route_shortcut_saves_through_the_standard_hotkey_path(make_dialog):
    dialog, store = make_dialog()
    field = dialog.actions_route_shortcut
    with patch.object(dialog, "set_standard_hotkey", wraps=dialog.set_standard_hotkey) as spy:
        field.input.captured.emit("ctrl+alt+v")
    spy.assert_called_once_with("route_record", "ctrl+alt+v")
    assert store.load_hotkey_settings()["route_record"] == "ctrl+alt+v"
    assert field.input.hotkey == "ctrl+alt+v"
    field.input.captured.emit("ctrl+alt+m")
    assert field.message.text() == "That shortcut is already used by Minimize to tray."
    assert store.load_hotkey_settings()["route_record"] == "ctrl+alt+v"
    field.input.begin_capture()
    field.input.cancel_capture()
    assert [c.args[0] for c in dialog.on_profile_hotkey_capture.call_args_list] == [True, False]
