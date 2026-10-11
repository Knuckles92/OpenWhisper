"""Settings → Automate → Actions: what a hotkey, a spoken name or the API does with your words.

The page has three parts: the featured Agent hotkey card, your own actions
(cards, an Add action gallery and an editor), and Route by voice. Settings
imports this module when it registers its pages, so module-level imports stay
light; ``build`` imports the widgets.
"""

TITLE = "Actions"
SUBTITLE = "Send what you say somewhere other than the cursor: a chat, your agent, or your own tools."

_AGENT_COPY = "Press a shortcut, say what you need, and your coding agent takes it from there."
_ROUTING_COPY = "Say an action's name first, like “Claude, …”, and your words go to it."
_ON_DICTATION_COPY = "A normal dictation that starts with an action's name goes to that action too."
_ROUTE_SHORTCUT_COPY = "Record, then send your words to the action they name, or paste them as usual."
#: What Route by voice sends, by the ways it can be used.
_PRIVACY_SHORTCUT = (
    "Everything you record with the Route by voice shortcut is sent to TypeSafe or "
    "OpenRouter to choose the action."
)
_PRIVACY_SHORTCUT_AND_DICTATION = (
    "Everything you record with the Route by voice shortcut, and normal dictations that "
    "start with an action's name, are sent to TypeSafe or OpenRouter to choose the action."
)
#: routing.status(...).fix when Fast judgments are what Route by voice still needs.
_FIX_FAST_JUDGMENTS = "fast_judgments"

SEARCH_FIELDS = [
    ("actions_agent_card", "Agent hotkey", _AGENT_COPY),
    (
        "actions_panel",
        "Your actions",
        "Send your words to a chat, a webhook or your own program. Give each a "
        "shortcut, or say its name.",
    ),
    ("actions_routing_tile", "Route by voice", _ROUTING_COPY),
    ("actions_routing_dictation_tile", "Also listen to normal dictations", _ON_DICTATION_COPY),
    ("actions_route_shortcut_tile", "Route by voice shortcut", _ROUTE_SHORTCUT_COPY),
]
CONTROL_ATTRS = tuple(attr for attr, _title, _copy in SEARCH_FIELDS) + (
    "actions_routing_status",
    "actions_routing_privacy",
    "actions_route_shortcut",
)

_FIELDS = {attr: (title, description) for attr, title, description in SEARCH_FIELDS}


def _manager():
    # The store the dialog itself reads and writes, so the page never
    # disagrees with the rest of Settings about what is saved.
    from ui_qt.dialogs import settings_dialog

    return settings_dialog.settings_manager


def _try_action(dialog, action, sample_text: str, done) -> None:
    """Try ``action`` through the app (ActionRuntime.test); ``done(ActionResult)`` later."""
    from services.actions.runtime import ActionResult

    handler = dialog.on_try_action
    if handler is None:
        done(ActionResult(False, "Trying an action works while OpenWhisper is running."))
        return
    try:
        handler(action, sample_text, done)
    except Exception as exc:  # The runtime failed before it could answer.
        done(ActionResult(False, f"Couldn't try it: {exc}"))


def build(dialog, layout) -> None:
    from ui_qt.dialogs.settings_destinations import MCP, MEETING_FAST, STYLES
    from ui_qt.dialogs.settings_fields import settings_caption
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.action_agent_card import AgentHotkeyCard
    from ui_qt.widgets.action_fields import RouteShortcutField, StatusLine
    from ui_qt.widgets.action_panel import ActionsPanel
    from ui_qt.widgets.setting_tile import InfoTile, SettingTile

    def try_action(action, sample_text, done):
        _try_action(dialog, action, sample_text, done)

    destinations = {"styles": STYLES, "mcp": MCP}

    card = dialog.actions_agent_card = AgentHotkeyCard(manager=_manager(), try_action=try_action)
    card.changed.connect(lambda: _on_card_changed(dialog))
    card.capture_changed.connect(dialog.set_hotkey_capture_suspended)
    card.styles_requested.connect(lambda: dialog.select_destination(STYLES))
    layout.addWidget(card)
    layout.addSpacing(10)

    panel = dialog.actions_panel = ActionsPanel(manager=_manager(), try_action=try_action)
    panel.actions_changed.connect(lambda: _on_actions_changed(dialog))
    # Back from another page, say Fast judgments or Styles: read it all again.
    panel.shown.connect(lambda: refresh(dialog))
    panel.capture_changed.connect(dialog.set_hotkey_capture_suspended)
    panel.destination_requested.connect(
        lambda key: dialog.select_destination(destinations.get(key, key)))
    card.edit_requested.connect(lambda action: panel.edit(action.id))
    layout.addWidget(panel)
    layout.addSpacing(12)

    # No group caption: the first tile is called Route by voice itself.
    title, description = _FIELDS["actions_routing_tile"]
    dialog.actions_routing_tile = SettingTile(title, description, design_icon("microphone-blue.svg"))
    dialog.actions_routing_tile.checkbox.toggled.connect(
        lambda on: _on_routing_toggled(dialog, "voice_routing_enabled", on))
    title, description = _FIELDS["actions_routing_dictation_tile"]
    dialog.actions_routing_dictation_tile = SettingTile(
        title, description, design_icon("text-plus-green.svg"))
    dialog.actions_routing_dictation_tile.checkbox.toggled.connect(
        lambda on: _on_routing_toggled(dialog, "voice_routing_on_dictation", on))
    dialog._tile_group(layout, "", [dialog.actions_routing_tile, dialog.actions_routing_dictation_tile])
    # Spoken routing's own words for where it stands: off, what it still
    # needs, or ready (services/actions/routing.py).
    status = dialog.actions_routing_status = StatusLine(
        "Fast judgments", "Open Settings → Meeting Mode → Fast judgments")
    status.link.clicked.connect(lambda: dialog.select_destination(MEETING_FAST))
    layout.addWidget(status)

    title, description = _FIELDS["actions_route_shortcut_tile"]
    tile = dialog.actions_route_shortcut_tile = InfoTile(
        title, description, design_icon("keyboard-green.svg"))
    dialog.actions_route_shortcut = RouteShortcutField(dialog)
    tile.add_body(dialog.actions_route_shortcut)
    dialog._tile_group(layout, "", [tile], columns=1)
    dialog.actions_routing_privacy = settings_caption(_PRIVACY_SHORTCUT)
    layout.addWidget(dialog.actions_routing_privacy)


def _on_actions_changed(dialog) -> None:
    dialog.notify_changed("actions")
    settings = dialog._settings_snapshot()
    dialog.actions_agent_card.load(settings)
    _show_routing(dialog, settings)


def _on_card_changed(dialog) -> None:
    dialog.notify_changed("actions")
    settings = dialog._settings_snapshot()
    # The full editor may be open on the same action.
    dialog.actions_panel.refresh(settings)
    _show_routing(dialog, settings)


def _on_routing_toggled(dialog, key: str, on: bool) -> None:
    from PyQt6.QtCore import QSignalBlocker

    if dialog._loading:
        return
    tile = (dialog.actions_routing_tile if key == "voice_routing_enabled"
            else dialog.actions_routing_dictation_tile)
    if not dialog._persist(key, bool(on)):
        with QSignalBlocker(tile.checkbox):
            tile.checkbox.setChecked(not on)
    _show_routing(dialog, dialog._settings_snapshot())


def _show_routing(dialog, settings: dict) -> None:
    from PyQt6.QtCore import QSignalBlocker

    from services.actions import routing
    from services.settings import (
        resolve_voice_routing_enabled,
        resolve_voice_routing_on_dictation,
    )
    from ui_qt.utils.restyle import set_style_property
    from ui_qt.widgets.action_fields import StatusLine

    enabled = resolve_voice_routing_enabled(settings)
    on_dictation = resolve_voice_routing_on_dictation(settings)
    for tile, value in (
        (dialog.actions_routing_tile, enabled),
        (dialog.actions_routing_dictation_tile, on_dictation),
    ):
        with QSignalBlocker(tile.checkbox):
            tile.checkbox.setChecked(value)
        set_style_property(tile, "checked", value)
    dialog.actions_routing_dictation_tile.setEnabled(enabled)
    state = routing.status(settings)
    tone = (StatusLine.READY if state.ready
            else StatusLine.ATTENTION if state.enabled else StatusLine.OFF)
    # The link leads only where the fix is: Fast judgments live on another page.
    dialog.actions_routing_status.show_state(
        tone, state.message, link=state.fix == _FIX_FAST_JUDGMENTS)
    privacy = dialog.actions_routing_privacy
    privacy.setText(_PRIVACY_SHORTCUT_AND_DICTATION if on_dictation else _PRIVACY_SHORTCUT)
    # Ready, routing's own message carries the privacy sentence.
    privacy.setVisible(not state.ready)
    dialog.actions_route_shortcut.sync()


def _show(dialog, settings: dict) -> None:
    if "actions_agent_card" not in dialog.__dict__:
        return
    dialog.actions_agent_card.load(settings)
    dialog.actions_panel.refresh(settings)
    _show_routing(dialog, settings)


def load(dialog, settings: dict) -> None:
    _show(dialog, settings)


def rail_value(settings: dict) -> str:
    from services.actions.model import AGENT_ACTION_ID, load_actions

    actions = load_actions(settings)
    count = sum(action.id != AGENT_ACTION_ID for action in actions)
    parts = []
    if len(actions) != count:
        parts.append("Agent hotkey set")
    if count:
        parts.append(f"{count} action{'' if count == 1 else 's'}")
    return " · ".join(parts) if parts else "None yet"


def refresh(dialog) -> None:
    _show(dialog, dialog._settings_snapshot())


def save_drafts(dialog, *, ask: bool = False) -> bool:
    panel = dialog.__dict__.get("actions_panel")
    return panel is None or panel.save_draft(ask=ask)


def cancel_capture(dialog) -> None:
    for name in ("actions_agent_card", "actions_panel", "actions_route_shortcut"):
        widget = dialog.__dict__.get(name)
        if widget is not None:
            widget.cancel_capture()
