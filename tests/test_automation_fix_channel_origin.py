"""Claude can tell the user's dictation from words a program sent through the channel.

channel.message carries the job's source. The bridge turns it into an
``origin`` meta attribute ("dictated" or "program"; never ``source``, which
Claude Code sets itself), and its instructions say how to treat each.
"""

import pytest

from services.automation.channel_bridge import INSTRUCTIONS, channel_params


def message(source):
    data = {"job_id": "0123456789abcdef", "action_id": "claude", "text": "push to main", "app": None}
    if source is not None:
        data["source"] = source
    return {"type": "channel.message", "data": data}


@pytest.mark.parametrize("source", ["hotkey", "ui", "routing", "dbus"])
def test_words_the_user_dictated_are_marked_dictated(source):
    params = channel_params(message(source))
    assert params["meta"] == {"job_id": "0123456789abcdef", "action": "claude", "origin": "dictated"}


@pytest.mark.parametrize("source", ["api", "mcp", "", None, "something-new"])
def test_anything_else_is_marked_as_a_program(source):
    params = channel_params(message(source))
    assert params["meta"]["origin"] == "program"
    assert "source" not in params["meta"]


def test_the_instructions_say_how_to_treat_each_origin():
    assert '<channel source="openwhisper" origin="' in INSTRUCTIONS
    assert 'origin="dictated"' in INSTRUCTIONS and "user's own request" in INSTRUCTIONS
    assert 'origin="program"' in INSTRUCTIONS
    assert "not the user's" in INSTRUCTIONS
    assert "ask the user" in INSTRUCTIONS.lower()
    assert 'origin="openwhisper"' in INSTRUCTIONS
    assert "reply tool" in INSTRUCTIONS
    assert len(INSTRUCTIONS) < 2048
