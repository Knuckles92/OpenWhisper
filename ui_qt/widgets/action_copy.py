"""What the Actions page says about an action: icons, labels, one-line summaries.

Plain functions with no Qt, so the cards, the editor and the rail all phrase
an action the same way.
"""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit

from services.actions.model import (
    AGENT_NAMES,
    Action,
    ActionKind,
    AgentMode,
    AgentSurface,
    Output,
    SHAPING_CLEANUP,
    SHAPING_RAW,
    link_scheme,
)

#: Words "Try it" and the link preview start with.
SAMPLE_WORDS = "What's on my calendar tomorrow?"

KIND_ICONS = {
    ActionKind.OPEN_LINK: "world-blue.svg",
    ActionKind.AGENT: "wand-purple.svg",
    ActionKind.CHANNEL: "notes-blue.svg",
    ActionKind.WEBHOOK: "server-blue.svg",
    ActionKind.PROGRAM: "cpu-blue.svg",
    ActionKind.CLIPBOARD: "copy-gray.svg",
    ActionKind.SCRIPTS: "bolt-green.svg",
}

KIND_LABELS = {
    ActionKind.OPEN_LINK: "Open a link",
    ActionKind.AGENT: "Coding agent",
    ActionKind.CHANNEL: "Claude Code session",
    ActionKind.WEBHOOK: "Webhook",
    ActionKind.PROGRAM: "Program",
    ActionKind.CLIPBOARD: "Copy only",
    ActionKind.SCRIPTS: "Send to my scripts",
}

#: What each kind does, in a line, under the editor's title.
KIND_BLURBS = {
    ActionKind.OPEN_LINK: "Opens a link with your words in it",
    ActionKind.AGENT: "Hands your words to a coding agent on this computer",
    ActionKind.CHANNEL: "Sends your words into a running Claude Code session",
    ActionKind.WEBHOOK: "Posts your words, signed, to an address you choose",
    ActionKind.PROGRAM: "Runs a program with your words on its input",
    ActionKind.CLIPBOARD: "Copies your words, ready to paste anywhere",
    ActionKind.SCRIPTS: "Tells the programs following OpenWhisper's events",
}

#: Where an answer, reply or program output goes, as the choosers show it.
OUTPUT_LABELS = {
    Output.NONE: "Nowhere",
    Output.PASTE: "Paste it where I was",
    Output.SCRATCHPAD: "Scratchpad",
    Output.NOTICE: "A notice",
    Output.CLIPBOARD: "Clipboard",
}

MODE_LABELS = {AgentMode.HANDOFF: "Open a session", AgentMode.ASK: "Quick answer"}
SURFACE_LABELS = {AgentSurface.AUTO: "Auto", AgentSurface.APP: "App", AgentSurface.TERMINAL: "Terminal"}
SURFACE_HINTS = {
    AgentSurface.AUTO: "Opens the agent's app when it's installed, otherwise a terminal.",
    AgentSurface.APP: "Opens the agent's desktop app with your words ready to send.",
    AgentSurface.TERMINAL: "Opens a new terminal running the agent with your words.",
}

_THE_RESULT = {
    Output.PASTE: "pastes {it} where you were",
    Output.SCRATCHPAD: "shows {it} in the Scratchpad",
    Output.NOTICE: "shows {it} in a notice",
    Output.CLIPBOARD: "copies {it}",
}


def agent_name(agent_id: str) -> str:
    return AGENT_NAMES.get(agent_id, "your agent")


def link_destination(template: str) -> str:
    """Where a link template goes, for "Opens …": "a new Claude chat", "example.com"."""
    scheme = link_scheme(template)
    if scheme == "claude":
        return "a new Claude chat"
    if scheme == "codex":
        return "a new ChatGPT chat"
    if scheme in ("http", "https"):
        try:
            host = (urlsplit(template).hostname or "").removeprefix("www.")
        except ValueError:
            host = ""
        if host == "chatgpt.com":
            return "ChatGPT in your browser"
        if host == "claude.ai":
            return "Claude in your browser"
        return host or "a web page"
    return f"a {scheme} link" if scheme else "a link"


def _then(output: str, it: str) -> str:
    phrase = _THE_RESULT.get(output, "")
    return f", then {phrase.format(it=it)}" if phrase else ""


def summary(action: Action) -> str:
    """One line on what ``action`` does with the words, without a full stop."""
    kind, p = action.kind, action.param
    if kind == ActionKind.OPEN_LINK:
        return f"Opens {link_destination(p('template') or '')} with your words"
    if kind == ActionKind.AGENT:
        name = agent_name(p("agent"))
        if p("mode") == AgentMode.ASK:
            phrase = _THE_RESULT.get(p("output"), "")
            return (f"Asks {name} and {phrase.format(it='the answer')}" if phrase
                    else f"Asks {name} quietly")
        return f"Opens a {name} session with your words"
    if kind == ActionKind.CHANNEL:
        return "Sends your words to your Claude Code session"
    if kind == ActionKind.WEBHOOK:
        try:
            host = urlsplit(p("url") or "").hostname or ""
        except ValueError:
            host = ""
        return f"Sends your words to {host or 'a web address'}{_then(p('output'), 'the reply')}"
    if kind == ActionKind.PROGRAM:
        program = os.path.basename(p("program") or "") or "a program"
        return f"Runs {program} with your words{_then(p('output'), 'its output')}"
    if kind == ActionKind.CLIPBOARD:
        return "Copies your words without pasting them"
    if kind == ActionKind.SCRIPTS:
        return "Tells the programs listening to OpenWhisper"
    return "Does something with your words"


def spoken_hint(action: Action) -> str:
    """'Say “Claude”' for the first spoken name, or ""."""
    return f"Say “{action.spoken_names[0]}”" if action.spoken_names else ""


def shaping_label(shaping: str, settings: Mapping) -> str:
    """How an action prepares the words, in a few words."""
    if shaping == SHAPING_RAW:
        return "Exactly what you said"
    if shaping == SHAPING_CLEANUP or not shaping:
        return "Cleaned up"
    probe = Action("", "", ActionKind.CLIPBOARD, shaping=shaping)
    if probe.profile_id():
        from services.cleanup_profiles import find_cleanup_profile

        profile = find_cleanup_profile(dict(settings), probe.profile_id())
        return f"Profile · {profile.name}" if profile else "A profile that was deleted"
    if probe.transform_id():
        from services.text_transforms import find_transform

        transform = find_transform(settings, probe.transform_id())
        return f"Transform · {transform.name}" if transform else "A transform that was deleted"
    return "Cleaned up"


def parse_spoken_names(text: str) -> tuple[str, ...]:
    """A comma-separated list of spoken names, trimmed, empty ones dropped."""
    return tuple(name for name in (" ".join(part.split()) for part in (text or "").split(",")) if name)


def parse_arguments(text: str) -> list[str]:
    """Program arguments, one per line; blank lines are dropped, spaces inside kept."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
