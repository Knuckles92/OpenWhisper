"""A run a program started never copies to the clipboard or pastes into an app.

Only a failed run's fallback copy checked who started it, so a webhook, a
program or a Quick answer run over the live API or MCP still copied its
answer, or pasted it (Ctrl+V) into whatever app the user had in front; a
shortened notice copied the whole answer too. Now an answer a program's run
would paste or copy goes to the Scratchpad, which shows it without acting on
it, and a shortened notice copies nothing for it.
"""

import pytest

from services.actions import runtime
from services.actions.model import Action, ActionKind
from services.automation import programs
from services.automation.webhooks import DeliveryRecord
from tests import test_automation_actions_runtime as base
from tests.test_automation_fix_actions_runtime import _source_job

h = base.h  # the runtime harness: fake UI, settings and event bus
_job, _finished = base._job, base._finished

#: Sources of runs a program started: the live API, MCP, and an unknown one.
PROGRAMS = ["api", "mcp", ""]


def _replying(h, monkeypatch, output):
    h.add(Action("w1", "Translate", ActionKind.WEBHOOK,
                 {"url": "http://127.0.0.1:9/hook", "output": output}))
    monkeypatch.setattr(runtime, "ensure_action_secret", lambda action_id: "s")
    monkeypatch.setattr(runtime, "_post_webhook", lambda url, payload, secret: (
        DeliveryRecord("", "", "", True, 200, 3), b'{"text": "Hallo Welt"}'))


@pytest.mark.parametrize("output", ["paste", "clipboard"])
@pytest.mark.parametrize("source", PROGRAMS)
def test_a_programs_answer_goes_to_the_scratchpad(h, monkeypatch, source, output):
    _replying(h, monkeypatch, output)

    h.rt.deliver(_source_job("w1", source), "hello world", raw_text=None, html="")
    ran = _finished(h)

    assert h.ui.copied == [] and h.controller.transcription_runtime.pasted == []
    assert h.ui.scratchpad == ["Hallo Welt"]
    assert h.ui.notices == [("Translate answered in the Scratchpad", True)]
    assert (ran["ok"], ran["output"]) == (True, "scratchpad")


@pytest.mark.parametrize("output, where", [("paste", "paste"), ("clipboard", "clipboard")])
def test_the_users_own_answer_still_pastes_or_copies(h, monkeypatch, output, where):
    _replying(h, monkeypatch, output)

    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    ran = _finished(h)

    used = h.controller.transcription_runtime.pasted if output == "paste" else h.ui.copied
    assert [item[0] for item in used] == ["Hallo Welt"]
    assert h.ui.scratchpad == [] and ran["output"] == where


@pytest.mark.parametrize("source", PROGRAMS)
def test_a_programs_shortened_notice_copies_nothing(h, monkeypatch, source):
    h.add(Action("p1", "Explain", ActionKind.PROGRAM,
                 {"program": "C:\\x.exe", "output": "notice"}))
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"word " * 100, ""))

    h.rt.deliver(_source_job("p1", source), "x", raw_text=None, html="")
    ran = _finished(h)

    ((shown, done),) = h.ui.notices
    assert done and shown.endswith("…") and len(shown) == runtime.NOTICE_MAX_CHARS
    assert h.ui.copied == []
    assert (ran["ok"], ran["output"], ran["message"]) == (True, "notice", "Showed Explain's answer")


@pytest.mark.parametrize("source", PROGRAMS)
def test_a_programs_copy_only_run_puts_the_words_in_the_scratchpad(h, source):
    h.add(Action("c1", "Copy only", ActionKind.CLIPBOARD))

    h.rt.deliver(_source_job("c1", source), "from a script", raw_text=None,
                 html="<b>from a script</b>")
    ran = _finished(h)

    assert h.ui.copied == []
    assert h.ui.scratchpad == ["from a script"]
    # Like the user's own Copy only run, it has no answer to report.
    assert (ran["ok"], ran["message"], ran["output"]) == (True, "Put in the Scratchpad", "none")
    assert h.ui.notices == [("Put in the Scratchpad", True)]
