"""ActionRuntime's workers and the relay that brings their results to the Qt thread."""

import gc
import logging
import threading
import time
import weakref
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QCoreApplication

from services.actions import agents, launch, runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionResult, ActionRuntime
from services.automation import catalog, events, programs
from services.automation.events import EventBus
from services.dictation_pipeline import DictationJob, JobMode
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings


class FakeUI:
    def __init__(self):
        self.notices, self.copied, self.statuses = [], [], []
        self.deleted = False

    def _alive(self):
        if self.deleted:
            raise RuntimeError("wrapped C/C++ object of type UIController has been deleted")

    def discard_clipboard_prefetch(self):
        self._alive()

    def set_status(self, text):
        self._alive()
        self.statuses.append(text)

    def show_notice(self, text, *, done=False):
        self._alive()
        self.notices.append(text)

    def copy_to_clipboard(self, text, html=""):
        self._alive()
        self.copied.append(text)
        return True

    def show_in_scratchpad(self, text):
        self._alive()


ASK = Action("q1", "Quick answer", ActionKind.AGENT, {"mode": "ask", "output": "notice"})
PROGRAM = Action("p1", "Shout", ActionKind.PROGRAM, {"program": "C:\\shout.exe",
                                                     "output": "notice"})
LINK = Action("l1", "Open", ActionKind.OPEN_LINK, {"template": "https://example.com/{text}"})


@pytest.fixture
def h(monkeypatch):
    settings = InMemorySettings({SettingsKey.ACTIONS: [
        a.to_dict() for a in (ASK, PROGRAM, LINK)]})
    monkeypatch.setattr(runtime, "settings_manager", settings)
    bus = EventBus()
    previous = events.set_bus(bus)
    ui = FakeUI()
    rt = ActionRuntime(SimpleNamespace(ui_controller=ui))
    try:
        yield SimpleNamespace(ui=ui, rt=rt, bus=bus)
    finally:
        rt.close()
        events.set_bus(previous)
        bus.close()


def _job(action_id):
    return DictationJob(mode=JobMode.ACTION, action_id=action_id, source="hotkey")


def _spin(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


class BlockedAsk:
    """agents.ask that waits until released or canceled."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancels = []

    def __call__(self, agent_id, text, *, model="", timeout_s=120.0, cancel=None):
        self.cancels.append(cancel)
        self.started.set()
        while not self.release.is_set():
            if cancel is not None and cancel.wait(0.01):
                return ActionResult(False, "Canceled")
        return ActionResult(True, "", "the answer")


def test_close_cancels_an_agent_and_drops_its_late_result(h, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    h.rt.deliver(_job("q1"), "question", raw_text=None, html="")
    assert ask.started.wait(5)

    started = time.monotonic()
    h.rt.close()
    assert time.monotonic() - started < 1.0

    (cancel,) = ask.cancels
    assert cancel.is_set()
    _spin(0.2)
    assert h.ui.notices == [] and h.ui.copied == []
    assert h.bus.recent(types=[catalog.ACTION_RAN]) == []


def test_a_run_after_close_is_not_started(h, monkeypatch):
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: pytest.fail("ran after close"))
    h.rt.close()

    h.rt.deliver(_job("p1"), "words", raw_text=None, html="")

    assert h.ui.notices == ["OpenWhisper is closing — copied instead"]
    assert h.ui.copied == ["words"]


def test_a_destroyed_ui_does_not_crash_the_relay(h, monkeypatch, caplog):
    release = threading.Event()

    def slow_run(*args, **kwargs):
        release.wait(5)
        return 0, b"done", ""

    monkeypatch.setattr(programs, "run_once", slow_run)
    h.rt.deliver(_job("p1"), "words", raw_text=None, html="")
    h.ui.deleted = True
    release.set()

    with caplog.at_level(logging.ERROR, logger=runtime.__name__):
        _wait(lambda: "could not be shown" in caplog.text)

    # The relay still serves later runs.
    h.ui.deleted = False
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"second", ""))
    h.rt.deliver(_job("p1"), "words", raw_text=None, html="")
    _wait(lambda: "second" in h.ui.notices)


class Controller:
    def __init__(self):
        self.ui_controller = FakeUI()


def test_workers_hold_no_reference_to_the_runtime(h, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    controller = Controller()
    rt = ActionRuntime(controller)
    rt.deliver(_job("q1"), "question", raw_text=None, html="")
    assert ask.started.wait(5)
    rt.close()
    alive = weakref.ref(rt)
    controller_alive = weakref.ref(controller)

    del rt, controller
    gc.collect()

    assert alive() is None and controller_alive() is None
    ask.release.set()
    _spin(0.1)


def test_a_slow_agent_does_not_hold_up_other_actions(h, monkeypatch):
    ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", ask)
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"quick", ""))
    monkeypatch.setattr(launch, "open_uri", lambda uri: None)

    h.rt.deliver(_job("q1"), "question", raw_text=None, html="")
    h.rt.deliver(_job("q1"), "another question", raw_text=None, html="")
    assert ask.started.wait(5)
    h.rt.deliver(_job("p1"), "words", raw_text=None, html="")
    h.rt.deliver(_job("l1"), "words", raw_text=None, html="")
    _wait(lambda: "quick" in h.ui.notices and "Sent to Open" in h.ui.notices)

    assert "the answer" not in h.ui.notices
    ask.release.set()
    _wait(lambda: h.ui.notices.count("the answer") == 2)


def test_the_relay_lives_on_the_app_thread_whoever_creates_it(monkeypatch):
    monkeypatch.setattr(runtime, "_relay", None)
    made = []
    maker = threading.Thread(target=lambda: made.append(runtime._result_relay()))
    maker.start()
    maker.join()
    (relay,) = made
    app = QCoreApplication.instance()
    assert relay.thread() is app.thread()

    seen = []
    token = next(runtime._tokens)
    relay.expect(token, lambda result: seen.append((result, threading.current_thread())))
    emitter = threading.Thread(target=lambda: relay.finished.emit(token, "result"))
    emitter.start()
    emitter.join()
    _wait(lambda: seen)

    assert seen == [("result", threading.main_thread())]


def test_a_dropped_token_is_ignored(monkeypatch):
    monkeypatch.setattr(runtime, "_relay", None)
    relay = runtime._result_relay()
    seen = []
    token = next(runtime._tokens)
    relay.expect(token, seen.append)
    relay.drop([token])

    relay.finished.emit(token, "late")
    _spin(0.05)

    assert seen == []
