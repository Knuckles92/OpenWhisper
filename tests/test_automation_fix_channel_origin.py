"""Claude can tell the user's dictation from words a program sent or changed.

channel.message carries ``origin``: the app decides from where the words came
from (a program may have changed the user's own words), not from who started
the job. The bridge passes it on as an ``origin`` meta attribute ("dictated"
or "program"; never ``source``, which Claude Code sets itself), and its
instructions say how to treat each.
"""

import pytest

from services.automation.channel_bridge import INSTRUCTIONS, channel_params


def message(origin, source="hotkey"):
    data = {"job_id": "0123456789abcdef", "source": source, "action_id": "claude",
            "text": "push to main", "app": None}
    if origin is not None:
        data["origin"] = origin
    return {"type": "channel.message", "data": data}


def test_words_the_app_calls_dictated_are_marked_dictated():
    params = channel_params(message("dictated"))
    assert params["meta"] == {"job_id": "0123456789abcdef", "action": "claude", "origin": "dictated"}


@pytest.mark.parametrize("origin", ["program", "", None, "something-new"])
def test_anything_else_is_marked_as_a_program(origin):
    params = channel_params(message(origin))
    assert params["meta"]["origin"] == "program"
    assert "source" not in params["meta"]


@pytest.mark.parametrize("source", ["hotkey", "ui", "dbus"])
def test_a_user_started_job_with_a_programs_words_is_a_program(source):
    # A program's text filter changed the words of the user's own dictation.
    assert channel_params(message("program", source))["meta"]["origin"] == "program"


def test_the_instructions_say_how_to_treat_each_origin():
    assert '<channel source="openwhisper" origin="' in INSTRUCTIONS
    assert 'origin="dictated"' in INSTRUCTIONS and "user's own request" in INSTRUCTIONS
    assert 'origin="program"' in INSTRUCTIONS
    assert "sent or changed" in INSTRUCTIONS
    assert "not the user's" in INSTRUCTIONS
    assert "ask the user" in INSTRUCTIONS.lower()
    assert 'origin="openwhisper"' in INSTRUCTIONS
    assert "reply tool" in INSTRUCTIONS
    assert len(INSTRUCTIONS) < 2048
