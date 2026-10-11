"""Agent actions review fixes: Claude's verbose JSON, npm shims, the .exe for terminals."""

import json

import pytest

from services.actions import agents

RESULT = {"type": "result", "subtype": "success", "is_error": False,
          "result": "Lima.", "session_id": "x"}


# -- finding 4: `claude -p --output-format json` with verbose output on ---------------------


@pytest.mark.parametrize("stdout", [
    json.dumps(RESULT),
    # What Claude Code prints when verbose is on in the user's config: every
    # message of the turn as one JSON array.
    json.dumps([
        {"type": "system", "subtype": "init", "session_id": "x"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Lima."}]}},
        RESULT,
    ]),
    # Pretty-printed across lines.
    json.dumps([{"type": "system", "subtype": "init"}, RESULT], indent=2),
], ids=["object", "verbose-array", "indented-array"])
def test_claude_answers_from_either_json_shape(stdout):
    result = agents._claude_answer(agents._Run("done", 0, stdout + "\n", ""))
    assert (result.ok, result.output) == (True, "Lima.")


def test_claude_verbose_errors_are_reported():
    failed = dict(RESULT, subtype="error_max_turns", is_error=True, result="")
    stdout = json.dumps([{"type": "system", "subtype": "init"}, failed])
    result = agents._claude_answer(agents._Run("done", 1, stdout, ""))
    assert not result.ok and result.message == "Claude Code couldn't answer: error_max_turns"


def test_claude_output_without_a_result_says_it_stopped():
    stdout = json.dumps([{"type": "system", "subtype": "init"}])
    result = agents._claude_answer(agents._Run("done", 1, stdout, "boom"))
    assert not result.ok and result.message == "Claude Code stopped before answering: boom"
