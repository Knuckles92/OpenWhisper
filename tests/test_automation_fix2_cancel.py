"""Review round 2: what a Cancel reaches, and what it says it did."""

import threading
from types import SimpleNamespace

import pytest

from services.actions import agents, launch, runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionResult
from services.automation.webhooks import DeliveryRecord
from tests import test_automation_actions_runtime as base
from tests.test_automation_fix_actions_runtime import BlockedAsk

h = base.h  # the runtime harness: fake UI, settings and event bus
_job, _ran, _wait = base._job, base._ran, base._wait

TRANSLATE = Action("w1", "Translate", ActionKind.WEBHOOK,
                   {"url": "http://127.0.0.1:9/hook", "output": "scratchpad"})
LINK = Action("l1", "Ask Claude", ActionKind.OPEN_LINK,
              {"template": "claude://claude.ai/new?q={text}"})
QUICK = Action("q1", "Quick answer", ActionKind.AGENT,
               {"agent": "claude_code", "mode": "ask", "output": "notice"})


class Blocked:
    """A blocking call that waits until released, counting who got in."""

    def __init__(self, result):
        self.result = result
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = []

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls.append(args)
        self.release.wait(10)
        return self.result

    def started(self, count=1):
        with self.lock:
            return len(self.calls) >= count


@pytest.fixture
def posting(h, monkeypatch):
    """A webhook action whose POST is under way until released."""
    post = Blocked((DeliveryRecord("", "", "", True, 200, 3), b'{"text": "Hallo Welt"}'))
    monkeypatch.setattr(runtime, "_post_webhook", post)
    h.add(TRANSLATE)
    yield post
    post.release.set()


# -- finding 2: a run too far along to stop reports what really happened ------------


def test_a_webhook_already_posting_finishes_and_reports_what_happened(h, posting):
    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    _wait(posting.started)

    assert h.rt.cancel() == 1
    assert _ran(h) == []  # not "Canceled": the receiver may already be acting on it
    notices = list(h.ui.notices)

    posting.release.set()
    _wait(lambda: _ran(h))
    ran, = _ran(h)
    assert (ran["ok"], ran["message"], ran["output"]) == (True, "Sent to Translate", "none")
    # Its answer is dropped: nothing reaches the Scratchpad, the clipboard or a notice.
    assert h.ui.scratchpad == [] and h.ui.copied == [] and h.ui.notices == notices


def test_a_failure_canceled_too_late_is_reported_without_copying_the_words(h, monkeypatch):
    post = Blocked((DeliveryRecord("", "", "", False, 500, 3), b""))
    monkeypatch.setattr(runtime, "_post_webhook", post)
    h.add(TRANSLATE)
    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    _wait(post.started)

    h.rt.cancel()
    post.release.set()
    _wait(lambda: _ran(h))

    ran, = _ran(h)
    assert (ran["ok"], ran["message"], ran["output"]) == (
        False, "Translate: the address answered 500", "none")
    assert h.ui.copied == []


def test_runs_still_waiting_never_start_and_say_canceled(h, monkeypatch):
    opened = Blocked(None)
    monkeypatch.setattr(launch, "open_uri", opened)
    h.add(LINK)
    for n in range(runtime.MAX_WORKERS + 2):
        h.rt.deliver(_job("l1"), f"question {n}", raw_text=None, html="")
    _wait(lambda: opened.started(runtime.MAX_WORKERS))

    assert h.rt.cancel() == runtime.MAX_WORKERS + 2

    # The two that were waiting: canceled at once, and never opened.
    assert [(r["ok"], r["message"]) for r in _ran(h)] == [
        (False, runtime.CANCELED_MESSAGE)] * 2
    opened.release.set()
    _wait(lambda: len(_ran(h)) == runtime.MAX_WORKERS + 2)
    assert len(opened.calls) == runtime.MAX_WORKERS
    finished = [(r["ok"], r["message"]) for r in _ran(h)[2:]]
    assert finished == [(True, "Sent to Ask Claude")] * runtime.MAX_WORKERS
    assert h.rt.accepting()


def test_a_canceled_try_it_still_learns_what_happened(h, posting):
    results = []
    h.rt.test(TRANSLATE, "sample", results.append)
    _wait(posting.started)

    assert h.rt.cancel(tests_only=True) == 1
    assert results == []

    posting.release.set()
    _wait(lambda: results)
    assert results == [ActionResult(True, "Sent to Translate", "Hallo Welt")]


def test_a_second_cancel_leaves_a_run_already_reached_alone(h, posting):
    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    _wait(posting.started)
    assert h.rt.cancel() == 1
    assert h.rt.cancel() == 0


# -- finding 1: a Cancel cancels what is in progress, and says what it canceled -----


@pytest.fixture
def thinking(h, monkeypatch):
    """A Quick answer still thinking until released."""
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    h.add(QUICK)
    h.rt.deliver(_job("q1"), "what's the capital of Peru", raw_text=None, html="")
    _wait(lambda: ask.started(1))
    yield ask
    ask.release.set()


def _users_cancel(h, *, canceled_a_job):
    """The controller's Cancel, with a dictation in progress or not."""
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        transcription_runtime=SimpleNamespace(cancel=lambda: canceled_a_job),
        action_runtime=h.rt,
    )
    # On the Qt thread the signal calls its slot at once.
    controller.actions_cancel_requested = SimpleNamespace(
        emit=lambda: ApplicationController._cancel_actions(controller))
    ApplicationController.cancel(controller)


def test_a_cancel_from_the_hotkey_thread_reaches_actions_on_the_qt_thread():
    from PyQt6.QtCore import QCoreApplication, QObject

    from services.application_controller import ApplicationController

    controller = ApplicationController.__new__(ApplicationController)
    QObject.__init__(controller)
    threads = []
    controller.transcription_runtime = SimpleNamespace(cancel=lambda: False)
    controller.action_runtime = SimpleNamespace(
        cancel=lambda: threads.append(threading.current_thread()))
    controller.actions_cancel_requested.connect(controller._cancel_actions)  # as _connect_signals

    hotkey = threading.Thread(target=controller.cancel)
    hotkey.start()
    hotkey.join(5)
    assert threads == []
    _wait(lambda: threads)
    assert threads == [threading.main_thread()]
    QCoreApplication.processEvents()


def test_canceling_a_new_dictation_leaves_the_quick_answer_thinking(h, thinking):
    _users_cancel(h, canceled_a_job=True)

    (_text, cancel), = thinking.calls
    assert not cancel.is_set()
    thinking.release.set()
    _wait(lambda: ("the answer: what's the capital of Peru", True) in h.ui.notices)
    assert _ran(h)[-1]["ok"] is True


def test_cancel_with_nothing_recording_stops_the_quick_answer_and_says_so(h, thinking):
    _users_cancel(h, canceled_a_job=False)

    (_text, cancel), = thinking.calls
    assert cancel.is_set()
    assert h.ui.statuses[-1] == "Canceled Quick answer"
    assert h.ui.notices[-1] == ("Canceled Quick answer", False)


def test_a_cancel_too_late_to_stop_a_run_says_so(h, thinking, posting):
    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    _wait(posting.started)

    _users_cancel(h, canceled_a_job=False)

    assert h.ui.statuses[-1] == "Canceled Quick answer — too late to stop Translate"
    assert h.ui.notices[-1] == (h.ui.statuses[-1], False)


def test_closing_settings_cancels_its_try_it_runs_quietly(h, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    results = []
    h.rt.test(QUICK, "sample", results.append)
    _wait(lambda: ask.started(1))

    assert h.rt.cancel(tests_only=True) == 1
    assert results == [ActionResult(False, runtime.CANCELED_MESSAGE)]
    assert h.ui.statuses == [] and h.ui.notices == []
