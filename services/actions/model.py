"""Saved actions: a name, what to do with the words, and how they're shaped first.

Only Settings creates or changes actions; the live API and MCP list and run
them. Parameters are kept per kind and unknown keys are dropped on load, so
an old or hand-edited settings file can't smuggle in fields a kind never
reads. Secrets (a webhook action's signing secret) are in the credential
store, never here.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import quote

from services.settings import SettingsKey

MAX_ACTIONS = 50
NAME_MAX_CHARS = 80
DESCRIPTION_MAX_CHARS = 300
MAX_SPOKEN_NAMES = 8
SPOKEN_NAME_MAX_CHARS = 40
TEMPLATE_MAX_CHARS = 2000
AGENT_ACTION_ID = "agent"


class ActionKind:
    OPEN_LINK = "open_link"
    AGENT = "agent"
    CHANNEL = "channel"
    WEBHOOK = "webhook"
    PROGRAM = "program"
    CLIPBOARD = "clipboard"
    SCRIPTS = "scripts"
    ALL = (OPEN_LINK, AGENT, CHANNEL, WEBHOOK, PROGRAM, CLIPBOARD, SCRIPTS)


class Output:
    """Where a webhook's, program's or agent's answer goes."""
    NONE = "none"
    PASTE = "paste"
    SCRATCHPAD = "scratchpad"
    NOTICE = "notice"
    CLIPBOARD = "clipboard"
    ALL = (NONE, PASTE, SCRATCHPAD, NOTICE, CLIPBOARD)


class AgentMode:
    HANDOFF = "handoff"
    ASK = "ask"
    ALL = (HANDOFF, ASK)


class AgentSurface:
    AUTO = "auto"
    APP = "app"
    TERMINAL = "terminal"
    ALL = (AUTO, APP, TERMINAL)


#: Installed agents an agent action can use (services/installed_agents ids).
AGENTS = ("claude_code", "codex", "opencode_cli")
AGENT_NAMES = {"claude_code": "Claude Code", "codex": "Codex", "opencode_cli": "OpenCode"}

SHAPING_RAW = "raw"
SHAPING_CLEANUP = "cleanup"
_PROFILE = "profile:"
_TRANSFORM = "transform:"

#: Link schemes an open_link action may never use.
REFUSED_SCHEMES = frozenset({"file", "javascript", "data", "vbscript", "about", "blob"})

#: Parameters per kind: name → default. The default's type is the accepted type.
PARAMS: dict[str, dict[str, Any]] = {
    ActionKind.OPEN_LINK: {"template": ""},
    ActionKind.AGENT: {
        "agent": "claude_code",
        "mode": AgentMode.HANDOFF,
        "surface": AgentSurface.AUTO,
        "folder": "",
        "new_folder": False,
        "include_selection": False,
        "model": "",
        "output": Output.SCRATCHPAD,
    },
    ActionKind.CHANNEL: {},
    ActionKind.WEBHOOK: {"url": "", "output": Output.NONE},
    ActionKind.PROGRAM: {"program": "", "args": [], "output": Output.NONE, "timeout_s": 30},
    ActionKind.CLIPBOARD: {},
    ActionKind.SCRIPTS: {},
}


def new_id() -> str:
    return secrets.token_hex(6)


def action_secret_name(action_id: str) -> str:
    return f"OPENWHISPER_ACTION_SECRET_{action_id}"


@dataclass(frozen=True)
class Action:
    id: str
    name: str
    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    hotkey: str = ""
    shaping: str = SHAPING_CLEANUP
    spoken_names: tuple[str, ...] = ()
    description: str = ""
    enabled: bool = True

    def param(self, key: str) -> Any:
        return self.params.get(key, PARAMS.get(self.kind, {}).get(key))

    def profile_id(self) -> str:
        return self.shaping[len(_PROFILE):] if self.shaping.startswith(_PROFILE) else ""

    def transform_id(self) -> str:
        return self.shaping[len(_TRANSFORM):] if self.shaping.startswith(_TRANSFORM) else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.params.items()},
            "hotkey": self.hotkey,
            "shaping": self.shaping,
            "spoken_names": list(self.spoken_names),
            "description": self.description,
            "enabled": self.enabled,
        }

    def public_dict(self) -> dict:
        """What the API and MCP may see: no URLs, paths, folders or arguments."""
        data = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "spoken_names": list(self.spoken_names),
            "hotkey": self.hotkey,
            "shaping": self.shaping,
            "enabled": self.enabled,
        }
        if self.kind == ActionKind.AGENT:
            data["agent"] = self.param("agent")
            data["mode"] = self.param("mode")
        return data


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    kind: str
    params: Mapping[str, Any]
    description: str
    icon: str
    shaping: str = SHAPING_CLEANUP
    note: str = ""


PRESETS: tuple[Preset, ...] = (
    Preset("agent", "Agent hotkey", ActionKind.AGENT,
           {"agent": "claude_code", "mode": AgentMode.HANDOFF},
           "Start a session with your coding agent from what you say.", "wand-purple.svg"),
    Preset("claude-chat", "Ask Claude", ActionKind.OPEN_LINK,
           {"template": "claude://claude.ai/new?q={text}"},
           "Open a new Claude Desktop chat with your words ready to send.", "world-blue.svg"),
    Preset("chatgpt-app", "ChatGPT app", ActionKind.OPEN_LINK,
           {"template": "codex://new?prompt={text}"},
           "Open a new chat in the ChatGPT desktop app with your words ready to send.",
           "world-blue.svg"),
    Preset("chatgpt-web", "ChatGPT on the web", ActionKind.OPEN_LINK,
           {"template": "https://chatgpt.com/?q={text}"},
           "Open chatgpt.com with your words in the message box.", "world-blue.svg",
           note="ChatGPT doesn't document this link, so it may stop working."),
    Preset("quick-answer", "Quick answer", ActionKind.AGENT,
           {"agent": "claude_code", "mode": AgentMode.ASK, "output": Output.SCRATCHPAD},
           "Ask your agent a question and read the answer in the Scratchpad.",
           "wand-purple.svg"),
    Preset("claude-code-session", "Tell my Claude Code session", ActionKind.CHANNEL, {},
           "Send your words into the Claude Code session running the OpenWhisper channel.",
           "notes-blue.svg"),
    Preset("webhook", "Webhook", ActionKind.WEBHOOK, {"url": ""},
           "Send your words to a web address, such as n8n or Home Assistant.",
           "server-blue.svg"),
    Preset("program", "Run a program", ActionKind.PROGRAM, {"program": ""},
           "Run a program or script with your words on its input.", "cpu-blue.svg"),
    Preset("copy", "Copy only", ActionKind.CLIPBOARD, {},
           "Copy your words without pasting them.", "copy-gray.svg"),
    Preset("scripts", "Send to my scripts", ActionKind.SCRIPTS, {},
           "Only tell programs listening to OpenWhisper's events.", "bolt-green.svg"),
)


# -- loading ------------------------------------------------------------------

def _coerce(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, list):
        if not isinstance(value, list):
            return list(default)
        return [v for v in value if isinstance(v, str)][:32]
    if isinstance(default, str):
        return value.strip() if isinstance(value, str) else default
    return default


def _params(kind: str, raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {key: _coerce(raw.get(key, default), default)
            for key, default in PARAMS.get(kind, {}).items()}


def _hotkey(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        from services.cleanup_profiles import normalize_hotkey

        return normalize_hotkey(value)
    except Exception:
        return ""


def _spoken(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = " ".join(item.split())
            if name and name.casefold() not in (n.casefold() for n in names):
                names.append(name[:SPOKEN_NAME_MAX_CHARS])
    return tuple(names[:MAX_SPOKEN_NAMES])


def _parse(item: Any) -> Optional[Action]:
    if not isinstance(item, dict):
        return None
    action_id, name, kind = (item.get(k) for k in ("id", "name", "kind"))
    if not all(isinstance(v, str) and v.strip() for v in (action_id, name, kind)):
        return None
    kind = kind.strip()
    if kind not in ActionKind.ALL:
        return None
    shaping = item.get("shaping")
    if not isinstance(shaping, str) or not shaping.strip():
        shaping = SHAPING_CLEANUP
    description = item.get("description")
    return Action(
        id=action_id.strip(),
        name=name.strip(),
        kind=kind,
        params=_params(kind, item.get("params")),
        hotkey=_hotkey(item.get("hotkey")),
        shaping=shaping.strip(),
        spoken_names=_spoken(item.get("spoken_names")),
        description=description.strip()[:DESCRIPTION_MAX_CHARS] if isinstance(description, str) else "",
        enabled=item.get("enabled") if isinstance(item.get("enabled"), bool) else True,
    )


def load_actions(settings: Mapping) -> list[Action]:
    """Saved actions; malformed rows, repeated ids and extras past the limit are skipped."""
    raw = (settings or {}).get(SettingsKey.ACTIONS)
    if not isinstance(raw, list):
        return []
    actions, seen = [], set()
    for item in raw:
        action = _parse(item)
        if action is None or action.id in seen:
            continue
        seen.add(action.id)
        actions.append(action)
        if len(actions) >= MAX_ACTIONS:
            break
    return actions


def find_action(settings: Mapping, action_id: str) -> Optional[Action]:
    return next((a for a in load_actions(settings) if a.id == action_id), None)


# -- links --------------------------------------------------------------------

def link_scheme(template: str) -> str:
    head, sep, _rest = (template or "").partition(":")
    if not sep or not head or not head[0].isalpha():
        return ""
    if not all(c.isalnum() or c in "+-." for c in head):
        return ""
    return head.lower()


def expand_link(template: str, text: str) -> str:
    """``template`` with ``{text}`` replaced by the percent-encoded words.

    Raises:
        ValueError: The template has no scheme or uses a refused one.
    """
    scheme = link_scheme(template)
    if not scheme:
        raise ValueError("Start the link with a scheme, such as https:// or claude://.")
    if scheme in REFUSED_SCHEMES:
        raise ValueError(f"Links that start with {scheme}: can't be used.")
    return template.replace("{text}", quote(text or "", safe=""))


# -- validation and saving ----------------------------------------------------

def action_hotkey_conflict(hotkey: str, settings: Mapping, *, exclude_id: str = "") -> str:
    """The action, profile, transform or other action already using ``hotkey``, or ""."""
    from services.hotkey_conflicts import ACTION, hotkey_conflict

    return hotkey_conflict(hotkey, settings, exclude=(ACTION, exclude_id))


def _validate_shaping(shaping: str, settings: Mapping) -> None:
    if shaping in (SHAPING_RAW, SHAPING_CLEANUP):
        return
    if shaping.startswith(_PROFILE):
        from services.cleanup_profiles import find_cleanup_profile

        if find_cleanup_profile(settings, shaping[len(_PROFILE):]) is None:
            raise ValueError("That cleanup profile no longer exists. Choose another.")
        return
    if shaping.startswith(_TRANSFORM):
        from services.text_transforms import find_transform

        if find_transform(settings, shaping[len(_TRANSFORM):]) is None:
            raise ValueError("That transform no longer exists. Choose another.")
        return
    raise ValueError("Choose how the words are prepared.")


def _validate_params(action: Action) -> None:
    kind, p = action.kind, action.param
    if kind == ActionKind.OPEN_LINK:
        template = p("template") or ""
        if not template:
            raise ValueError("Enter the link to open.")
        if len(template) > TEMPLATE_MAX_CHARS:
            raise ValueError(f"Use a link of {TEMPLATE_MAX_CHARS} characters or fewer.")
        expand_link(template, "")
    elif kind == ActionKind.AGENT:
        if p("agent") not in AGENTS:
            raise ValueError("Choose Claude Code, Codex or OpenCode.")
        if p("mode") not in AgentMode.ALL:
            raise ValueError("Choose whether to open a session or ask for an answer.")
        if p("surface") not in AgentSurface.ALL:
            raise ValueError("Choose where the session opens.")
        folder = p("folder") or ""
        if not p("new_folder") and folder and not os.path.isabs(folder):
            raise ValueError("Choose the working folder with its full path.")
        if p("output") not in Output.ALL:
            raise ValueError("Choose where the answer goes.")
    elif kind == ActionKind.WEBHOOK:
        from services.automation.models import validate_url

        validate_url(p("url") or "")
        if p("output") not in Output.ALL:
            raise ValueError("Choose where the reply goes.")
    elif kind == ActionKind.PROGRAM:
        program = p("program") or ""
        if not program:
            raise ValueError("Choose the program to run.")
        if not os.path.isabs(program):
            raise ValueError("Choose the program with its full path.")
        if p("output") not in Output.ALL:
            raise ValueError("Choose where the program's output goes.")
        if not 1 <= int(p("timeout_s")) <= 300:
            raise ValueError("Use a time limit from 1 to 300 seconds.")


def validate_action(action: Action, settings: Mapping) -> None:
    """Raise ValueError with a user-facing reason ``action`` can't be saved."""
    name = action.name.strip()
    if not name:
        raise ValueError("Give the action a name.")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"Use a name with {NAME_MAX_CHARS} characters or fewer.")
    if action.kind not in ActionKind.ALL:
        raise ValueError("Choose what the action does.")
    if len(action.description) > DESCRIPTION_MAX_CHARS:
        raise ValueError(f"Use a description of {DESCRIPTION_MAX_CHARS} characters or fewer.")
    existing = load_actions(settings)
    others = [a for a in existing if a.id != action.id]
    if any(a.name.casefold() == name.casefold() for a in others):
        raise ValueError("An action with that name already exists.")
    taken = {n.casefold(): a.name for a in others for n in (a.name, *a.spoken_names)}
    for spoken in (name, *action.spoken_names):
        owner = taken.get(spoken.casefold())
        if owner:
            raise ValueError(f'"{spoken}" already names {owner}. Choose another name.')
    if len(others) == len(existing) and len(existing) >= MAX_ACTIONS:
        raise ValueError(f"You can keep up to {MAX_ACTIONS} actions. Delete one to add another.")
    _validate_shaping(action.shaping, settings)
    _validate_params(action)
    conflict = action_hotkey_conflict(action.hotkey, settings, exclude_id=action.id)
    if conflict:
        raise ValueError(f"That shortcut is already used by {conflict}. Choose another.")


def save_action(settings: dict, action: Action) -> None:
    """Add or replace ``action``; a ``mutate_settings`` mutator.

    Raises:
        ValueError: With a user-facing reason; nothing is changed.
    """
    action = Action(
        id=action.id.strip(),
        name=action.name.strip(),
        kind=action.kind,
        params=_params(action.kind, dict(action.params)),
        hotkey=_hotkey(action.hotkey),
        shaping=(action.shaping or SHAPING_CLEANUP).strip(),
        spoken_names=_spoken(list(action.spoken_names)),
        description=action.description.strip(),
        enabled=bool(action.enabled),
    )
    if not action.id:
        raise ValueError("This action has no id.")
    validate_action(action, settings)
    actions = load_actions(settings)
    index = next((i for i, a in enumerate(actions) if a.id == action.id), len(actions))
    actions[index:index + 1] = [action]
    settings[SettingsKey.ACTIONS] = [a.to_dict() for a in actions]


def delete_action(settings: dict, action_id: str) -> None:
    """Remove an action; a ``mutate_settings`` mutator."""
    settings[SettingsKey.ACTIONS] = [
        a.to_dict() for a in load_actions(settings) if a.id != action_id
    ]
