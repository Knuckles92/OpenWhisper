"""What the live API and MCP may do for the user; checked on every request."""

from __future__ import annotations

from typing import Mapping

from services.settings import SETTING_DEFAULTS, SettingsKey

LIVE_EVENTS = SettingsKey.API_LIVE_EVENTS
RUN_ACTIONS = SettingsKey.API_RUN_ACTIONS
CONTROL_RECORDING = SettingsKey.API_CONTROL_RECORDING
TEXT_FILTERS = SettingsKey.API_TEXT_FILTERS
#: Running an agent or Claude Code channel action for a program, which hands
#: that program's words to the user's coding agent; needs RUN_ACTIONS as well.
AGENT_PROMPTS = SettingsKey.API_AGENT_PROMPTS
ALL: tuple[str, ...] = (LIVE_EVENTS, RUN_ACTIONS, CONTROL_RECORDING, TEXT_FILTERS, AGENT_PROMPTS)

#: Short names used in API and MCP responses.
PUBLIC_NAMES: dict[str, str] = {
    LIVE_EVENTS: "live_events",
    RUN_ACTIONS: "run_actions",
    CONTROL_RECORDING: "control_recording",
    TEXT_FILTERS: "text_filters",
    AGENT_PROMPTS: "agent_prompts",
}
#: What a refusal tells the caller to do, without naming internals.
_DENIED: dict[str, str] = {
    LIVE_EVENTS: "Allow live events in OpenWhisper Settings > API & MCP.",
    RUN_ACTIONS: "Allow running actions in OpenWhisper Settings > API & MCP.",
    CONTROL_RECORDING: "Allow starting recordings in OpenWhisper Settings > API & MCP.",
    TEXT_FILTERS: "Allow changing dictated text in OpenWhisper Settings > API & MCP.",
    AGENT_PROMPTS: "Allow sending words to your coding agents in OpenWhisper Settings > API & MCP.",
}


class PermissionDenied(Exception):
    def __init__(self, permission: str) -> None:
        self.permission = permission
        self.message = _DENIED.get(permission, "This action is not allowed.")
        super().__init__(self.message)


def granted(settings: Mapping, permission: str) -> bool:
    """True when ``permission`` is on; a missing or malformed value means its default."""
    if permission not in ALL:
        return False
    value = (settings or {}).get(permission, SETTING_DEFAULTS[permission])
    if not isinstance(value, bool):
        value = SETTING_DEFAULTS[permission]
    return value is True


def require(settings: Mapping, permission: str) -> None:
    if not granted(settings, permission):
        raise PermissionDenied(permission)


def describe(settings: Mapping) -> dict[str, bool]:
    return {PUBLIC_NAMES[key]: granted(settings, key) for key in ALL}
