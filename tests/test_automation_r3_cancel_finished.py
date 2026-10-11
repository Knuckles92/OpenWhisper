"""Cancel says it was too late for a program or Quick answer that already finished.

A Cancel posted just before such a run's work returned, and handled after,
found a future that could no longer be canceled and a kind that can be
stopped: it dropped the run's queued result and said "Canceled", though the
program ran to the end. A run whose work has returned is now past stopping,
like a webhook already posting: ``action.ran`` reports what happened, its
answer goes nowhere, and the notice says it was too late.
"""

import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QCoreApplication

from services.actions import agents, runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionResult
from tests import test_automation_actions_runtime as base
from tests.test_automation_fix_actions_runtime import BlockedAsk

h = base.h  # the runtime harness: fake UI, settings and event bus
_job, _ran, _wait = base._job, base._ran, base._wait

TICKET = Action("p1", "Make a ticket", ActionKind.PROGRAM,
                {"program": "C:/nowhere/ticket.exe", "args": [], "timeout_s": 5,
                 "output": "notice"})
QUICK = Action("q1", "Quick answer", ActionKind.AGENT,
               {"agent": "claude_code", "mode": "ask", "output": "notice"})


@pytest.fixture
def ticket(h, monkeypatch):
    """A program that runs to the end and prints its ticket number."""
    ran = []

    def program(argv, stdin, timeout_s, env, cancel):
        ran.append(True)
        return ActionResult(True, "", "TICKET-42")

    monkeypatch.setattr(runtime, "_program", program)
    monkeypatch.setattr(runtime, "_program_env", lambda extra: {})
    h.add(TICKET)
    return ran


def _finished_and_queued(h, action_id):
    """The run's work returned and its result waits for the Qt thread."""
    pending, = [p for p in h.rt._pending.values() if p.run.action.id == action_id]
    pending.future.result(5)


def test_a_program_that_already_finished_reports_what_happened(h, ticket):
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _finished_and_queued(h, "p1")

    assert h.rt.cancel() == 1
    QCoreApplication.processEvents()

    assert ticket == [True]
    assert [(r["ok"], r["message"], r["output"]) for r in _ran(h)] == [
        (True, "Sent to Make a ticket", "none")]
    # Its answer goes nowhere.
    assert h.ui.notices == [("Too late to stop Make a ticket", False)]
    assert h.ui.copied == [] and h.ui.scratchpad == []


def test_a_quick_answer_that_already_came_back_reports_what_happened(h, monkeypatch):
    monkeypatch.setattr(agents, "ask", lambda agent_id, text, **k: ActionResult(True, "", "Lima"))
    h.add(QUICK)
    h.rt.deliver(_job("q1"), "capital of Peru", raw_text=None, html="")
    _finished_and_queued(h, "q1")

    h.rt.cancel()
    QCoreApplication.processEvents()

    assert [(r["ok"], r["output"]) for r in _ran(h)] == [(True, "none")]
    assert h.ui.notices == [("Too late to stop Quick answer", False)]


def test_a_failure_that_already_happened_is_reported_without_copying(h, monkeypatch):
    monkeypatch.setattr(runtime, "_program", lambda *args: ActionResult(
        False, "the program stopped with code 2"))
    monkeypatch.setattr(runtime, "_program_env", lambda extra: {})
    h.add(TICKET)
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _finished_and_queued(h, "p1")

    h.rt.cancel()
    QCoreApplication.processEvents()

    assert [(r["ok"], r["message"], r["output"]) for r in _ran(h)] == [
        (False, "Make a ticket: the program stopped with code 2", "none")]
    assert h.ui.copied == []


def test_a_cancel_says_which_it_stopped_and_which_had_finished(h, ticket, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    h.add(QUICK)
    h.rt.deliver(_job("q1"), "capital of Peru", raw_text=None, html="")
    _wait(lambda: ask.started(1))
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    _finished_and_queued(h, "p1")

    assert h.rt.cancel() == 2

    # One notice, once the Quick answer reports that it stopped.
    _wait(lambda: h.ui.notices)
    assert h.ui.notices == [
        ("Canceled Quick answer — too late to stop Make a ticket", False)]
    (_text, cancel), = ask.calls
    assert cancel.is_set()


class _HeldAfterSending:
    """The session's relay, with each worker held just after it sends its result.

    The run's future isn't done yet then, though its result is on the way.
    """

    def __init__(self, relay):
        self.relay = relay
        self.sent = threading.Event()
        self.hold = threading.Event()
        self.finished = SimpleNamespace(emit=self._emit)

    def expect(self, token, callback):
        self.relay.expect(token, callback)

    def drop(self, tokens):
        self.relay.drop(tokens)

    def _emit(self, token, result):
        self.relay.finished.emit(token, result)
        self.sent.set()
        self.hold.wait(5)


def test_a_result_already_sent_is_past_stopping_before_its_worker_is_done(
        h, ticket, monkeypatch):
    held = _HeldAfterSending(runtime._result_relay())
    monkeypatch.setattr(runtime, "_relay", held)
    h.rt.deliver(_job("p1"), "file a ticket", raw_text=None, html="")
    assert held.sent.wait(5)
    pending, = h.rt._pending.values()
    assert not pending.future.done()

    try:
        h.rt.cancel()
    finally:
        held.hold.set()
    _wait(lambda: _ran(h))

    assert [(r["ok"], r["message"]) for r in _ran(h)] == [(True, "Sent to Make a ticket")]
    assert h.ui.notices == [("Too late to stop Make a ticket", False)]
