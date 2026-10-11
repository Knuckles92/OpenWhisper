"""How the Actions page phrases an action, in one line, without Qt."""

import os

import pytest

from services.actions.model import PRESETS, Action, ActionKind
from ui_qt.widgets.action_copy import (
    KIND_BLURBS,
    KIND_ICONS,
    parse_arguments,
    parse_spoken_names,
    spoken_hint,
    summary,
)


def _action(kind, **params):
    return Action("x", "X", kind, params)


@pytest.mark.parametrize("action,expected", [
    (_action(ActionKind.OPEN_LINK, template="claude://claude.ai/new?q={text}"),
     "Opens a new Claude chat with your words"),
    (_action(ActionKind.OPEN_LINK, template="codex://new?prompt={text}"),
     "Opens a new ChatGPT chat with your words"),
    (_action(ActionKind.OPEN_LINK, template="https://chatgpt.com/?q={text}"),
     "Opens ChatGPT in your browser with your words"),
    (_action(ActionKind.OPEN_LINK, template="https://www.perplexity.ai/search?q={text}"),
     "Opens perplexity.ai with your words"),
    (_action(ActionKind.OPEN_LINK, template="obsidian://new?content={text}"),
     "Opens Obsidian with your words"),
    (_action(ActionKind.AGENT, agent="codex", mode="handoff"),
     "Opens a Codex session with your words"),
    (_action(ActionKind.AGENT, agent="codex", mode="handoff", new_folder=True),
     "Opens a Codex session in a new folder with your words"),
    (_action(ActionKind.AGENT, agent="claude_code", mode="ask", output="scratchpad"),
     "Asks Claude Code and shows the answer in the Scratchpad"),
    (_action(ActionKind.AGENT, agent="opencode_cli", mode="ask", output="paste"),
     "Asks OpenCode and pastes the answer where you were"),
    (_action(ActionKind.CHANNEL), "Sends your words to your Claude Code session"),
    (_action(ActionKind.WEBHOOK, url="http://homeassistant.local/api/webhook/x", output="notice"),
     "Sends your words to homeassistant.local, then shows the reply in a notice"),
    (_action(ActionKind.WEBHOOK, url="https://n8n.example.com/hook", output="none"),
     "Sends your words to n8n.example.com"),
    # A path as this system writes it: summaries name the program by its file name.
    (_action(ActionKind.PROGRAM, program=os.path.join("tools", "journal.exe"), output="clipboard"),
     "Runs journal.exe with your words, then copies its output"),
    (_action(ActionKind.CLIPBOARD), "Copies your words without pasting them"),
    (_action(ActionKind.SCRIPTS), "Tells the programs listening to OpenWhisper"),
])
def test_summaries_say_what_happens_to_your_words(action, expected):
    assert summary(action) == expected


def test_every_kind_and_preset_has_an_icon_and_a_line():
    for kind in ActionKind.ALL:
        assert KIND_ICONS[kind].endswith(".svg")
        assert KIND_BLURBS[kind]
        assert summary(_action(kind))
    for preset in PRESETS:
        assert summary(Action("x", preset.name, preset.kind, dict(preset.params)))


def test_spoken_names_and_arguments_are_tidied():
    assert parse_spoken_names(" Claude ,  ask   Claude,, ") == ("Claude", "ask Claude")
    assert parse_arguments("--append\n\n  --title My day \n") == ["--append", "--title My day"]
    assert spoken_hint(Action("x", "X", ActionKind.CLIPBOARD, spoken_names=("House",))) == "Say “House”"
    assert spoken_hint(_action(ActionKind.CLIPBOARD)) == ""
