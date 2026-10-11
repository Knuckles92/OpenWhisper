"""ActionRuntime review fixes: a cap on pending runs, cancel, and what a failure copies."""

import threading
import time

import pytest

from services.actions import agents, runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionResult
from services.automation import programs
from services.dictation_pipeline import DictationJob, JobMode
from tests import test_automation_actions_runtime as base

h = base.h  # the runtime harness: fake UI, settings and event bus
_job, _ran, _wait = base._job, base._ran, base._wait


class BlockedAsk:
    """agents.ask that waits until released or canceled."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, agent_id, text, *, model="", timeout_s=120.0, cancel=None):
        with self.lock:
            self.calls.append((text, cancel))
        while not self.release.is_set():
            if cancel is not None and cancel.wait(0.01):
                return ActionResult(False, "Stopped asking Claude Code.")
        return ActionResult(True, "", "the answer: " + text)

    def started(self, count):
        with self.lock:
            return len(self.calls) >= count


ASK = Action("q1", "Quick answer", ActionKind.AGENT,
             {"agent": "claude_code", "mode": "ask", "output": "notice"})


def _source_job(action_id, source):
    job = _job(action_id)
    return DictationJob(mode=JobMode.ACTION, action_id=action_id, source=source,
                        focus=job.focus, recognition=job.recognition)


@pytest.fixture
def blocked(h, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    h.add(ASK)
    yield ask
    ask.release.set()


# -- finding 1: the pending cap -----------------------------------------------------


def test_runs_beyond_the_pending_cap_are_refused(h, blocked):
    for n in range(runtime.MAX_PENDING):
        assert h.rt.accepting()
        h.rt.deliver(_job("q1"), f"question {n}", raw_text=None, html="")
    _wait(lambda: blocked.started(runtime.MAX_WORKERS))

    assert not h.rt.accepting()
    h.rt.deliver(_job("q1"), "one too many", raw_text=None, html="")

    ran = _ran(h)[-1]
    assert ran["ok"] is False
    assert ran["message"].startswith(runtime.BUSY_MESSAGE)
    time.sleep(0.2)
    assert len(blocked.calls) == runtime.MAX_WORKERS  # the rest wait their turn

    blocked.release.set()
    _wait(lambda: len(_ran(h)) == 1 + runtime.MAX_PENDING, timeout=10)
    assert h.rt.accepting()
    assert sum(1 for text, _ in blocked.calls if text == "one too many") == 0


# -- finding 2: cancel -------------------------------------------------------------------


def test_cancel_stops_running_and_waiting_runs_and_shows_nothing(h, blocked):
    total = runtime.MAX_WORKERS + 2
    for n in range(total):
        h.rt.deliver(_job("q1"), f"question {n}", raw_text=None, html="")
    _wait(lambda: blocked.started(runtime.MAX_WORKERS))
    notices_before = list(h.ui.notices)

    assert h.rt.cancel() == total

    assert all(cancel.is_set() for _text, cancel in blocked.calls)
    assert h.rt.accepting()
    time.sleep(0.3)
    from PyQt6.QtCore import QCoreApplication

    QCoreApplication.processEvents()
    assert len(blocked.calls) == runtime.MAX_WORKERS  # the waiting ones never start
    assert h.ui.notices == notices_before and h.ui.copied == [] and h.ui.scratchpad == []
    ran = _ran(h)
    assert len(ran) == total
    assert {(r["ok"], r["message"], r["output"]) for r in ran} == {
        (False, runtime.CANCELED_MESSAGE, "none")}
    assert h.rt.cancel() == 0


def test_cancel_kills_a_running_program(h, monkeypatch):
    h.add(Action("p1", "Shout", ActionKind.PROGRAM, {"program": "C:\\shout.exe"}))
    seen = []

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        seen.append(cancel)
        cancel.wait(10)
        return None, b"", programs.CANCELED

    monkeypatch.setattr(programs, "run_once", run_once)
    h.rt.deliver(_job("p1"), "words", raw_text=None, html="")
    _wait(lambda: seen)

    assert h.rt.cancel() == 1
    assert seen[0].is_set()
    time.sleep(0.2)
    _wait(lambda: len(_ran(h)) == 1)
    assert _ran(h)[-1]["message"] == runtime.CANCELED_MESSAGE
    assert h.ui.copied == []


def test_a_try_it_run_is_canceled_and_told_so(h, blocked):
    results = []
    h.rt.test(ASK, "sample", results.append)
    _wait(lambda: blocked.started(1))

    assert h.rt.cancel() == 1

    assert results == [ActionResult(False, runtime.CANCELED_MESSAGE)]
    assert _ran(h)[-1]["source"] == "ui"


def test_cancel_can_spare_the_users_own_runs(h, blocked):
    results = []
    h.rt.deliver(_job("q1"), "mine", raw_text=None, html="")
    h.rt.test(ASK, "sample", results.append)
    _wait(lambda: blocked.started(2))

    assert h.rt.cancel(tests_only=True) == 1

    assert results == [ActionResult(False, runtime.CANCELED_MESSAGE)]
    (mine,) = [cancel for text, cancel in blocked.calls if text == "mine"]
    assert not mine.is_set()
    blocked.release.set()
    _wait(lambda: ("the answer: mine", True) in h.ui.notices)


# -- finding 3: a failure copies the words only for the user's own runs ----------------------


def _refuse_links(monkeypatch):
    from services.actions import launch

    def refuse(uri):
        raise launch.LaunchError("No app on this computer opens that link.")

    monkeypatch.setattr(launch, "open_uri", refuse)
    monkeypatch.setattr(launch, "handler_available", lambda scheme: True)


@pytest.mark.parametrize("source", ["api", "mcp"])
def test_a_failed_run_from_a_program_leaves_the_clipboard_alone(h, monkeypatch, source):
    _refuse_links(monkeypatch)
    h.add(Action("l1", "Ask Claude", ActionKind.OPEN_LINK,
                 {"template": "claude://claude.ai/new?q={text}"}))

    h.rt.deliver(_source_job("l1", source), "words from a script", raw_text=None, html="")
    ran = base._finished(h)

    assert h.ui.copied == []
    message = "Ask Claude: No app on this computer opens that link"
    assert h.ui.statuses[-1] == message
    assert (ran["ok"], ran["message"], ran["output"], ran["source"]) == (
        False, message, "none", source)


@pytest.mark.parametrize("source", ["api", "mcp"])
def test_a_missing_action_run_from_a_program_is_not_copied(h, source):
    h.rt.deliver(_source_job("gone", source), "words", raw_text=None, html="")

    assert h.ui.copied == []
    assert _ran(h)[-1]["message"] == runtime.MISSING_MESSAGE


@pytest.mark.parametrize("source", ["hotkey", "routing", "ui", "dbus"])
def test_a_failed_run_the_user_started_still_copies_the_words(h, monkeypatch, source):
    _refuse_links(monkeypatch)
    h.add(Action("l1", "Ask Claude", ActionKind.OPEN_LINK,
                 {"template": "claude://claude.ai/new?q={text}"}))

    h.rt.deliver(_source_job("l1", source), "my words", raw_text=None, html="")
    ran = base._finished(h)

    assert h.ui.copied == [("my words", "")]
    assert ran["output"] == "clipboard" and ran["message"].endswith(runtime.COPIED_INSTEAD)


def test_a_refusal_at_the_cap_from_the_api_is_not_copied(h, blocked):
    for n in range(runtime.MAX_PENDING):
        h.rt.deliver(_job("q1"), f"question {n}", raw_text=None, html="")
    copied = list(h.ui.copied)

    h.rt.deliver(_source_job("q1", "api"), "one too many", raw_text=None, html="")

    assert h.ui.copied == copied
    assert _ran(h)[-1]["message"] == runtime.BUSY_MESSAGE
