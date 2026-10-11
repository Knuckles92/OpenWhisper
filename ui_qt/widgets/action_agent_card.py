"""The featured "Agent hotkey" card at the top of Settings → Automate → Actions.

It creates or edits the action with id ``AGENT_ACTION_ID``: press a shortcut,
say a task, and Claude Code, Codex or OpenCode picks it up. Every choice
saves as it is made, like the rest of Settings; only the agents a scan found
can be chosen.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Optional

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QSizePolicy, QVBoxLayout, QWidget

from services.actions.model import (
    AGENT_ACTION_ID,
    AGENT_NAMES,
    AGENTS,
    Action,
    ActionKind,
    AgentMode,
    AgentSurface,
    delete_action,
    find_action,
    save_action,
)
from services.hotkey_manager import format_hotkey_display
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import action_relay
from ui_qt.widgets.action_copy import summary
from ui_qt.widgets.action_fields import (
    AgentFields,
    ShortcutField,
    button_row,
    field_label,
    hint_label,
    page_link,
    tint_icon,
)
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.reflow import Reflow
from ui_qt.widgets.setting_tile import InfoTile
from ui_qt.widgets.settings_switch import SettingsSwitch

TITLE = "Agent hotkey"
INVITE = "Press a shortcut, say what you need, and your coding agent takes it from there."
LOOKING = "Looking for Claude Code, Codex or OpenCode…"
NONE_FOUND = "None found — install one, or use another action."
#: What "Try it" sends: harmless in a session and cheap as a quick answer.
AGENT_SAMPLE = "Say hello in one short sentence."


def _spoken_for(agent_id: str) -> tuple[str, ...]:
    return (AGENT_NAMES[agent_id],) if agent_id in AGENT_NAMES else ()


def _description_for(agent_id: str) -> str:
    return f"Coding tasks for {AGENT_NAMES.get(agent_id, 'my coding agent')}"


def available_agents() -> dict:
    """``agents.available()``: the last scan's agents, never blocking."""
    from services.actions import agents

    return dict(agents.available() or {})


class AgentHotkeyCard(InfoTile):
    """Sets up, edits, tries and removes the agent hotkey action."""

    #: The saved action changed (created, edited, switched, removed).
    changed = pyqtSignal()
    capture_changed = pyqtSignal(bool)
    styles_requested = pyqtSignal()
    #: "More options": open the full editor on the agent action.
    edit_requested = pyqtSignal(object)

    def __init__(self, *, manager, try_action: Callable, parent=None):
        super().__init__(TITLE, INVITE, design_icon("wand-purple.svg"), parent)
        self.setProperty("tileId", "actionAgentCard")
        tint_icon(self, "wand-purple.svg", side=36)
        self.title_label.setProperty("tileId", "actionAgentTitle")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager
        self._try_action = try_action
        self._action: Optional[Action] = None
        self._scan_token = 0
        self._try_token = 0
        self._scanned = False

        self.enabled_switch = SettingsSwitch()
        self.enabled_switch.setAccessibleName("Agent hotkey on")
        self.enabled_switch.toggled.connect(self._on_enabled_toggled)
        self.add_trailing(self.enabled_switch)

        self.fields = AgentFields()
        self.fields.changed.connect(self._on_fields_changed)
        self.fields.styles_requested.connect(self.styles_requested)
        self.add_body(self.fields)

        self.status = hint_label("")
        self.look_again = page_link("Look again", "Check this computer again, for example after installing an agent")
        self.look_again.clicked.connect(lambda: self.start_scan(refresh=True))
        self.status_row = Reflow(self.status, self.look_again, lead_width=200,
                                 stacked_alignment=Qt.AlignmentFlag.AlignLeft)
        self.add_body(self.status_row)

        self.setup_button = compact_primary_button(Button("Set it up"))
        self.setup_button.setAccessibleName("Set up the agent hotkey")
        self.setup_button.clicked.connect(self.set_up)
        self.setup_row = button_row(self.setup_button)
        self.add_body(self.setup_row)

        self.shortcut_box = QWidget()
        self.shortcut_box.setProperty("tileId", "actionRow")
        shortcut_layout = QVBoxLayout(self.shortcut_box)
        shortcut_layout.setContentsMargins(0, 4, 0, 0)
        shortcut_layout.setSpacing(6)
        self.shortcut = ShortcutField("Agent hotkey shortcut")
        self.shortcut.captured.connect(self._on_shortcut_captured)
        self.shortcut.capture_changed.connect(self.capture_changed)
        shortcut_layout.addWidget(field_label("Shortcut", self.shortcut.input))
        shortcut_layout.addWidget(self.shortcut)
        self.add_body(self.shortcut_box)

        self.message = hint_label("")
        self.message.setProperty("tileId", "actionAgentMessage")
        self.message.hide()
        self.add_body(self.message)

        self.try_button = neutral_button(Button("Try it"))
        self.try_button.setToolTip(f"Sends “{AGENT_SAMPLE}” to the agent, as if you'd said it")
        self.try_button.clicked.connect(self.try_it)
        self.more_button = neutral_button(Button("More options"))
        self.more_button.setToolTip("Spoken names, how your words are prepared, and the rest")
        self.more_button.clicked.connect(
            lambda: self._action is not None and self.edit_requested.emit(self._action))
        self.remove_button = page_link("Remove", "Remove the agent hotkey and its shortcut")
        self.remove_button.setProperty("tone", "danger")
        self.remove_button.clicked.connect(self.remove)
        self.footer = Reflow(button_row(self.try_button, self.more_button), self.remove_button,
                             stacked_alignment=Qt.AlignmentFlag.AlignLeft)
        self.add_body(self.footer)

        relay = action_relay.relay()
        relay.scan_done.connect(self._on_scan_done)
        relay.try_done.connect(self._on_try_done)
        self.fields.set_available(None)
        self._show_available(available_agents(), animate=False)

    # ---- state ----

    @property
    def action(self) -> Optional[Action]:
        return self._action

    def load(self, settings: Mapping) -> None:
        """Show the saved agent action, or the invitation to set one up."""
        from services.settings import resolve_app_context_enabled

        self.fields.set_app_awareness(resolve_app_context_enabled(dict(settings)))
        if self.shortcut.input._capturing:
            return
        self._action = find_action(settings, AGENT_ACTION_ID)
        if self._action is not None and self._action.kind != ActionKind.AGENT:
            self._action = None  # An id clash from a hand-edited file; leave it alone.
        self._render()

    def _render(self) -> None:
        action = self._action
        configured = action is not None
        self.fields.details.setVisible(configured)
        self.shortcut_box.setVisible(configured)
        self.footer.setVisible(configured)
        self.enabled_switch.setVisible(configured)
        self.setup_row.setVisible(not configured)
        if configured:
            self.fields.set_params(action.params)
            self.shortcut.set_hotkey(action.hotkey)
            with QSignalBlocker(self.enabled_switch):
                self.enabled_switch.setChecked(action.enabled)
            set_style_property(self, "checked", action.enabled)
            self.set_description(summary(action) + ".")
        else:
            self.fields.set_params({"agent": self._preferred_agent()})
            set_style_property(self, "checked", False)
            self.set_description(INVITE)
        self._sync_status()

    def _preferred_agent(self) -> str:
        found = self.fields.chooser.found()
        return found[0] if found else AGENTS[0]

    def _sync_status(self) -> None:
        available = self.fields.chooser.available()
        configured = self._action is not None
        if available is None:
            text = LOOKING
        elif not self.fields.chooser.found():
            text = NONE_FOUND
        else:
            text = ""
        self.status.setText(text)
        self.status_row.setVisible(bool(text))
        self.look_again.setVisible(available is not None and not self.fields.chooser.found())
        self.setup_button.setEnabled(not configured and bool(self.fields.chooser.found()))

    # ---- finding agents ----

    def ensure_scanned(self) -> None:
        """Ask for a scan once, when ``agents.available()`` knows nothing yet."""
        if self._scanned or self.fields.chooser.available():
            return
        self.start_scan(refresh=False)

    def start_scan(self, *, refresh: bool) -> None:
        self._scanned = True
        self._scan_token = token = action_relay.next_token()
        self.fields.set_available(None)
        self._sync_status()
        action_relay.start_agent_scan(token, refresh=refresh)

    def _on_scan_done(self, token: int) -> None:
        if token != self._scan_token:
            return
        self._show_available(available_agents(), animate=True)

    def _show_available(self, available: dict, *, animate: bool) -> None:
        # An empty answer before any scan means "not looked yet", after one "none found".
        self.fields.set_available(available if (available or self._scanned) else None,
                                  animate=animate)
        if self._action is None:
            self.fields.chooser.set_current(self._preferred_agent())
        self._sync_status()

    # ---- changes ----

    def _save(self, action: Action) -> bool:
        try:
            self.manager.mutate_settings(lambda settings: save_action(settings, action))
        except ValueError as exc:
            self.show_message(str(exc), "error")
            return False
        except Exception as exc:  # The settings file couldn't be written.
            self.show_message(f"Couldn't save the agent hotkey: {exc}", "error")
            return False
        self._action = action
        self.changed.emit()
        return True

    def set_up(self) -> None:
        """Create the agent action with the chosen agent, then ask for its shortcut."""
        agent = self.fields.chooser.current()
        action = Action(
            AGENT_ACTION_ID, TITLE, ActionKind.AGENT,
            params={"agent": agent, "mode": AgentMode.HANDOFF, "surface": AgentSurface.AUTO},
            spoken_names=_spoken_for(agent),
            description=_description_for(agent),
        )
        if not self._save(action):
            # A spoken name another action already uses: set up without it.
            action = replace(action, spoken_names=())
            if not self._save(action):
                return
        self._render()
        self.show_message("Now press the shortcut you'd like to use.", "")
        self.shortcut.begin_capture()

    def _on_fields_changed(self) -> None:
        if self._action is None:
            return
        params = self.fields.params()
        action = replace(self._action, params=params)
        before, after = self._action.param("agent"), params["agent"]
        if before != after:
            # The name you say and what it's for follow the agent, unless you changed them.
            if action.spoken_names == _spoken_for(before):
                action = replace(action, spoken_names=_spoken_for(after))
            if action.description == _description_for(before):
                action = replace(action, description=_description_for(after))
        if self._save(action):
            self.set_description(summary(action) + ".")
            self.show_message("", "")
        else:
            self._render()

    def _on_shortcut_captured(self, hotkey: str) -> None:
        if self._action is None:
            return
        if self._save(replace(self._action, hotkey=hotkey)):
            self.shortcut.set_hotkey(hotkey)
            shortcut = format_hotkey_display(hotkey)
            self.show_message(
                f"Press {shortcut}, say what you need, and press it again." if shortcut
                else "Shortcut removed. Route by voice and the API can still use it.", "ok")
        else:
            self.shortcut.restore()

    def _on_enabled_toggled(self, on: bool) -> None:
        if self._action is None:
            return
        if self._save(replace(self._action, enabled=bool(on))):
            set_style_property(self, "checked", bool(on))
            self.show_message("", "")
        else:
            with QSignalBlocker(self.enabled_switch):
                self.enabled_switch.setChecked(not on)

    def remove(self) -> None:
        if self._action is None:
            return
        answer = QMessageBox.question(
            self, "Remove the agent hotkey?", "Remove the agent hotkey and its shortcut?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.mutate_settings(lambda settings: delete_action(settings, AGENT_ACTION_ID))
        except Exception as exc:
            self.show_message(f"Couldn't remove the agent hotkey: {exc}", "error")
            return
        self.shortcut.cancel_capture()
        self._action = None
        self._render()
        self.show_message("", "")
        self.changed.emit()

    def cancel_capture(self) -> None:
        self.shortcut.cancel_capture()

    # ---- trying ----

    def try_it(self) -> None:
        if self._action is None:
            return
        self._try_token = token = action_relay.next_token()
        self.try_button.setEnabled(False)
        self.show_message("Trying…", "")
        self._try_action(self._action, AGENT_SAMPLE, action_relay.try_callback(token))

    def _on_try_done(self, token: int, result) -> None:
        if token != self._try_token:
            return
        self._try_token = 0
        self.try_button.setEnabled(True)
        ok = bool(getattr(result, "ok", False))
        message = getattr(result, "message", "") or ("It worked." if ok else "It didn't work.")
        self.show_message(message, "ok" if ok else "error")

    # ---- messages ----

    def show_message(self, text: str, tone: str) -> None:
        self.message.setText(text)
        set_style_property(self.message, "tone", tone)
        self.message.setVisible(bool(text))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.ensure_scanned()

