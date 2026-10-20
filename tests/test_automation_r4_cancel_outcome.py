"""Review round 4, F1: Cancel says "Canceled" only for a run it really stopped.

A program that has exited may still be reading the output a helper it left
running holds open (programs.OUTPUT_GRACE_S), and a quick answer may just
have answered. Their work hasn't returned yet, but a Cancel can no longer
stop them: their result says so, and the run is reported as too late to
stop, with what really happened and no answer.
"""

import threading

import pytest

from services.actions import agents, runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionResult
from services.automation import programs
from tests import test_automation_actions_runtime as base

h = base.h  # the runtime harness: fake UI, settings and event bus
_job, _ran, _wait = base._job, base._ran, base._wait

TICKET = Action("p1", "Make a ticket", ActionKind.PROGRAM,
                {"program": "C:/nowhere/ticket.exe", "args": [], "timeout_s": 5,
                 "output": "notice"})
QUICK = Action("q1", "Quick answer", ActionKind.AGENT,
               {"agent": "claude_code", "mode": "ask", "output": "notice"})


class _Drained:
    """programs.run_once for a program that ran to its end: its output is
    still being read when the cancel comes, and the cancel changes nothing."""

    def __init__(self):
        self.exited = threading.Event()

    def __call__(self, argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        self.exited.set()
        cancel.wait(5)
        return 0, b"TICKET-42", ""


@pytest.fixture
def drained(h, monkeypatch):
    run_once = _Drained()
    monkeypatch.setattr(programs, "run_once", run_once)
    monkeypatch.setattr(runtime, "_program_env", lambda extra: {})
    h.add(TICKET)
    return run_once


def test_a_program_that_exited_while_its_output_drains_is_too_late_to_stop(h, drained):
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _wait(drained.exited.is_set)

    assert h.rt.cancel() == 1
    _wait(lambda: _ran(h))

    assert [(r["ok"], r["message"], r["output"]) for r in _ran(h)] == [
        (True, "Sent to Make a ticket", "none")]
    assert h.ui.notices == [("Too late to stop Make a ticket", False)]
    # Its answer goes nowhere.
    assert h.ui.copied == [] and h.ui.scratchpad == []
    assert h.rt.accepting() and h.rt.cancel() == 0


def test_a_try_it_program_that_already_exited_reports_its_answer(h, drained):
    results = []
    h.rt.test(TICKET, "sample", results.append)
    _wait(drained.exited.is_set)

    assert h.rt.cancel(tests_only=True) == 1
    _wait(lambda: results)

    assert results == [ActionResult(True, "Sent to Make a ticket", "TICKET-42")]


def test_a_program_the_cancel_kills_is_canceled(h, monkeypatch):
    started = threading.Event()

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        started.set()
        cancel.wait(5)
        return None, b"", programs.CANCELED

    monkeypatch.setattr(programs, "run_once", run_once)
    monkeypatch.setattr(runtime, "_program_env", lambda extra: {})
    h.add(TICKET)
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _wait(started.is_set)

    assert h.rt.cancel() == 1
    _wait(lambda: _ran(h))

    assert [(r["ok"], r["message"], r["output"]) for r in _ran(h)] == [
        (False, runtime.CANCELED_MESSAGE, "none")]
    assert h.ui.notices == [("Canceled Make a ticket", False)]
    assert h.ui.copied == []


def test_a_quick_answer_that_came_back_as_it_was_canceled_is_too_late(h, monkeypatch):
    answered = threading.Event()

    def ask(agent_id, text, *, model="", timeout_s=120.0, cancel=None, image=None):
        answered.set()
        cancel.wait(5)
        return ActionResult(True, "Claude Code answered", "Lima")

    monkeypatch.setattr(agents, "ask", ask)
    h.add(QUICK)
    h.rt.deliver(_job("q1"), "capital of Peru", raw_text=None, html="")
    _wait(answered.is_set)

    h.rt.cancel()
    _wait(lambda: _ran(h))

    assert [(r["ok"], r["message"], r["output"]) for r in _ran(h)] == [
        (True, "Claude Code answered", "none")]
    assert h.ui.notices == [("Too late to stop Quick answer", False)]


def test_one_cancel_says_once_what_it_stopped_and_what_was_too_late(h, drained, monkeypatch):
    def ask(agent_id, text, *, model="", timeout_s=120.0, cancel=None, image=None):
        cancel.wait(5)
        return ActionResult(False, "Stopped asking Claude Code.", stopped=True)

    monkeypatch.setattr(agents, "ask", ask)
    h.add(QUICK)
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _wait(drained.exited.is_set)
    h.rt.deliver(_job("q1"), "capital of Peru", raw_text=None, html="")

    assert h.rt.cancel() == 2
    _wait(lambda: len(_ran(h)) == 2)

    assert h.ui.notices == [("Canceled Quick answer — too late to stop Make a ticket", False)]
    assert sorted((r["action_id"], r["ok"], r["message"]) for r in _ran(h)) == [
        ("p1", True, "Sent to Make a ticket"), ("q1", False, runtime.CANCELED_MESSAGE)]
