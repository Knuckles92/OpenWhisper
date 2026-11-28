"""What the live API and MCP may do for the user; checked on every request."""

from __future__ import annotations

from typing import Mapping

from services.settings import SettingsKey, resolve_bool_setting

LIVE_EVENTS = SettingsKey.API_LIVE_EVENTS
RUN_ACTIONS = SettingsKey.API_RUN_ACTIONS
CONTROL_RECORDING = SettingsKey.API_CONTROL_RECORDING
TEXT_FILTERS = SettingsKey.API_TEXT_FILTERS
#: Running an agent or Claude Code channel action for a program, which hands
#: that program's words to the user's coding agent; needs RUN_ACTIONS as well.
AGENT_PROMPTS = SettingsKey.API_AGENT_PROMPTS
#: Start, pause, resume, end, retry, recover or open Meeting Mode.
CONTROL_MEETINGS = SettingsKey.API_CONTROL_MEETINGS
#: Read saved walkthroughs (transcripts, AI write-ups, references and screenshots).
READ_WALKTHROUGHS = SettingsKey.API_READ_WALKTHROUGHS
#: Start, snap, pause, edit, finish, cancel walkthroughs or take screenshots.
CONTROL_WALKTHROUGHS = SettingsKey.API_CONTROL_WALKTHROUGHS
#: Manage personal dictionary terms, snippets, app styles, languages and quick-record profile.
MANAGE_PERSONALIZATION = SettingsKey.API_MANAGE_PERSONALIZATION
#: Add, edit or remove learned transcript cleanup rules.
MANAGE_CLEANUP_RULES = SettingsKey.API_MANAGE_CLEANUP_RULES
#: Switch models/engines, audio devices, remote engine, transcribe files, and control windows.
CONTROL_APP = SettingsKey.API_CONTROL_APP

ALL: tuple[str, ...] = (
    LIVE_EVENTS,
    RUN_ACTIONS,
    CONTROL_RECORDING,
    TEXT_FILTERS,
    AGENT_PROMPTS,
    CONTROL_MEETINGS,
    READ_WALKTHROUGHS,
    CONTROL_WALKTHROUGHS,
    MANAGE_PERSONALIZATION,
    MANAGE_CLEANUP_RULES,
    CONTROL_APP,
)

#: Short names used in API and MCP responses.
PUBLIC_NAMES: dict[str, str] = {
    LIVE_EVENTS: "live_events",
    RUN_ACTIONS: "run_actions",
    CONTROL_RECORDING: "control_recording",
    TEXT_FILTERS: "text_filters",
    AGENT_PROMPTS: "agent_prompts",
    CONTROL_MEETINGS: "control_meetings",
    READ_WALKTHROUGHS: "read_walkthroughs",
    CONTROL_WALKTHROUGHS: "control_walkthroughs",
    MANAGE_PERSONALIZATION: "manage_personalization",
    MANAGE_CLEANUP_RULES: "manage_cleanup_rules",
    CONTROL_APP: "control_app",
}
#: What a refusal tells the caller to do, without naming internals.
_DENIED: dict[str, str] = {
    LIVE_EVENTS: "Allow live events in OpenWhisper Settings > API & MCP.",
    RUN_ACTIONS: "Allow running actions in OpenWhisper Settings > API & MCP.",
    CONTROL_RECORDING: "Allow starting recordings in OpenWhisper Settings > API & MCP.",
    TEXT_FILTERS: "Allow changing dictated text in OpenWhisper Settings > API & MCP.",
    AGENT_PROMPTS: "Allow sending words to your coding agents in OpenWhisper Settings > API & MCP.",
    CONTROL_MEETINGS: "Allow controlling Meeting Mode in OpenWhisper Settings > API & MCP.",
    READ_WALKTHROUGHS: "Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP.",
    CONTROL_WALKTHROUGHS: "Allow controlling walkthroughs and screenshots in OpenWhisper Settings > API & MCP.",
    MANAGE_PERSONALIZATION: "Allow managing dictionary, snippets and styles in OpenWhisper Settings > API & MCP.",
    MANAGE_CLEANUP_RULES: "Allow managing learned cleanup rules in OpenWhisper Settings > API & MCP.",
    CONTROL_APP: "Allow controlling models, devices, files and app windows in OpenWhisper Settings > API & MCP.",
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
    return resolve_bool_setting(permission, settings or {})


def require(settings: Mapping, permission: str) -> None:
    if not granted(settings, permission):
        raise PermissionDenied(permission)


def describe(settings: Mapping) -> dict[str, bool]:
    return {PUBLIC_NAMES[key]: granted(settings, key) for key in ALL}
