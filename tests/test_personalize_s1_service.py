"""The capture service's promises: deadlines, no queueing, wedges, never raising."""

import sys
import threading
import time

import pytest

from services import diagnostics, focus_context
from services.focus_context import AppIdentity, FocusSnapshot, TextContext
from services.focus_context import _capture
from services.focus_context._capture import CaptureService

OUTLOOK = AppIdentity("outlook.exe", "Outlook", pid=10, window="0x1")
SLACK = AppIdentity("slack.exe", "Slack", pid=11, window="0x2")
TEAMS = AppIdentity("ms-teams.exe", "Teams", pid=12, window="0x3")
WORD = AppIdentity("winword.exe", "Word", pid=13, window="0x4")
TERMINAL = AppIdentity("windowsterminal.exe", "Windows Terminal", pid=14)
SELF = AppIdentity("openwhisper", "OpenWhisper", pid=1, is_self=True)
CONTEXT = TextContext(before="Dear Sam,", caret_known=True, selection_known=True, source="fake")
WAIT = 5.0


class FakeReader:
    def __init__(self, platform):
        self.platform = platform
        self.thread = threading.current_thread()
        self.closed = threading.Event()

    def read(self, identity, *, include_text, include_selection):
        self.platform.reads.append((identity.app_id, include_text, include_selection))
        self.platform.read_threads.append(threading.current_thread())
        gate = self.platform.gates.get(identity.app_id)
        if gate is not None:
            self.platform.blocked.set()
            gate.wait(WAIT)
        if self.platform.read_error is not None:
            raise self.platform.read_error
        return self.platform.contexts.get(identity.app_id, CONTEXT)

    def close(self):
        self.closed.set()


class FakePlatform:
    name = "fake"

    def __init__(self, identity=OUTLOOK, *, sync=True, text=True):
        self.current = identity
        self.sync_identity = sync
        self.text_supported = text
        self.identity_delay = 0.0
        self.identity_error = None
        self.reader_error = None
        self.read_error = None
        self.readers = []
        self.reads = []
        self.read_threads = []
        self.gates = {}
        self.contexts = {}
        self.blocked = threading.Event()

    def identity(self):
        if self.identity_delay:
            time.sleep(self.identity_delay)
        if self.identity_error is not None:
            raise self.identity_error
        return self.current

    def text_reader(self):
        if self.reader_error is not None:
            raise self.reader_error
        reader = FakeReader(self)
        self.readers.append(reader)
        return reader


@pytest.fixture
def make_service():
    services, gates = [], []

    def build(platform=None, *, deadline_s=1.0, wedged_after_s=10.0, excluded=(), settings=None):
        platform = platform or FakePlatform()
        metrics = []
        service = CaptureService(
            platform,
            deadline_s=deadline_s,
            wedged_after_s=wedged_after_s,
            settings=settings or (lambda: {"app_context_excluded_apps": list(excluded)}),
            metrics=lambda **values: metrics.append(values),
        )
        services.append(service)
        gates.append(platform.gates)
        return service, platform, metrics

    yield build
    for platform_gates in gates:
        for gate in platform_gates.values():
            gate.set()
    for service in services:
        service.shutdown()


def _snapshot(future, timeout=WAIT):
    return future.result(timeout=timeout)


def _wait_for(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_identity_only_resolves_at_once_without_a_thread(make_service):
    service, platform, metrics = make_service()

    future = service.request(include_text=False)

    assert future.done() and future.result() == FocusSnapshot(OUTLOOK)
    assert service._worker is None and platform.readers == [] and metrics == []


def test_text_is_read_on_its_own_thread(make_service):
    service, platform, metrics = make_service()

    snapshot = _snapshot(service.request(include_text=True))

    assert snapshot == FocusSnapshot(OUTLOOK, CONTEXT)
    assert platform.reads == [("outlook.exe", True, False)]
    assert platform.read_threads[0] is not threading.current_thread()
    assert platform.readers[0].thread is platform.read_threads[0]
    assert len(metrics) == 1 and metrics[0]["context_capture_timeouts"] == 0
    assert 0 <= metrics[0]["context_capture_ms"] < WAIT * 1000

    selection = _snapshot(service.request(include_text=False, include_selection=True))
    assert selection.text == CONTEXT
    assert platform.reads[-1] == ("outlook.exe", False, True)
    assert len(platform.readers) == 1


def test_the_future_resolves_by_the_deadline_with_identity(make_service):
    service, platform, metrics = make_service(deadline_s=0.05)
    platform.gates["outlook.exe"] = threading.Event()

    started = time.monotonic()
    snapshot = _snapshot(service.request(include_text=True))

    assert snapshot == FocusSnapshot(OUTLOOK)
    assert time.monotonic() - started < 1.0
    assert metrics == [{"context_capture_ms": 50.0, "context_capture_timeouts": 1}]
    platform.gates["outlook.exe"].set()
    assert _wait_for(lambda: service._worker.busy_since is None)
    assert len(metrics) == 1


def test_a_busy_reader_means_identity_only_never_a_queue(make_service):
    service, platform, _metrics = make_service()
    platform.gates["outlook.exe"] = threading.Event()
    first = service.request(include_text=True)
    assert platform.blocked.wait(WAIT)

    platform.current = SLACK
    second = service.request(include_text=True)

    assert second.done() and second.result() == FocusSnapshot(SLACK)
    platform.gates["outlook.exe"].set()
    assert _snapshot(first) == FocusSnapshot(OUTLOOK, CONTEXT)
    assert [app for app, *_flags in platform.reads] == ["outlook.exe"]


def test_a_wedged_reader_is_replaced_and_its_app_left_alone(make_service):
    service, platform, _metrics = make_service(deadline_s=0.05, wedged_after_s=0.05)
    platform.gates["outlook.exe"] = threading.Event()
    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK)
    wedged = service._worker
    time.sleep(0.1)
    service._deadline_s = WAIT

    platform.current = SLACK
    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(SLACK, CONTEXT)
    assert wedged.abandoned and service._worker is not wedged
    assert len(platform.readers) == 2

    platform.current = OUTLOOK
    future = service.request(include_text=True)
    assert future.done() and future.result() == FocusSnapshot(OUTLOOK)
    assert [app for app, *_flags in platform.reads] == ["outlook.exe", "slack.exe"]

    # The abandoned thread finishes its call, closes its reader and exits.
    platform.gates["outlook.exe"].set()
    assert platform.readers[0].closed.wait(WAIT)
    assert not platform.readers[1].closed.is_set()


def test_text_capture_stops_after_a_few_replacements(make_service):
    service, platform, _metrics = make_service(deadline_s=0.02, wedged_after_s=0.02)
    apps = [AppIdentity(f"app{index}.exe", f"App {index}", pid=index) for index in range(6)]
    for app in apps:
        platform.gates[app.app_id] = threading.Event()

    for app in apps[:_capture.MAX_WORKER_REPLACEMENTS + 2]:
        platform.current = app
        _snapshot(service.request(include_text=True))
        time.sleep(0.05)

    assert service._text_off
    assert _wait_for(lambda: len(platform.readers) == _capture.MAX_WORKER_REPLACEMENTS + 1)
    platform.current = apps[-1]
    future = service.request(include_text=True)
    assert future.done() and future.result() == FocusSnapshot(apps[-1])
    assert service.request(include_text=False).result() == FocusSnapshot(apps[-1])


def test_repeated_misses_stop_reading_one_app(make_service):
    service, platform, metrics = make_service(deadline_s=0.02)
    platform.gates["outlook.exe"] = threading.Event()

    for _ in range(_capture.MAX_CONSECUTIVE_MISSES):
        assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK)
        platform.gates["outlook.exe"].set()
        assert _wait_for(lambda: service._worker.busy_since is None)
        platform.gates["outlook.exe"].clear()

    assert "outlook.exe" in service._disabled
    assert sum(sample["context_capture_timeouts"] for sample in metrics) == _capture.MAX_CONSECUTIVE_MISSES
    reads = len(platform.reads)
    assert service.request(include_text=True).result(timeout=0) == FocusSnapshot(OUTLOOK)
    assert len(platform.reads) == reads

    platform.current = SLACK
    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(SLACK, CONTEXT)


def test_a_fast_read_resets_the_miss_count(make_service):
    service, platform, _metrics = make_service(deadline_s=0.02)
    gate = platform.gates["outlook.exe"] = threading.Event()
    for _ in range(_capture.MAX_CONSECUTIVE_MISSES - 1):
        _snapshot(service.request(include_text=True))
        gate.set()
        assert _wait_for(lambda: service._worker.busy_since is None)
        gate.clear()
    del platform.gates["outlook.exe"]
    service._deadline_s = 1.0
    assert _snapshot(service.request(include_text=True)).text == CONTEXT
    assert "outlook.exe" not in service._misses


@pytest.mark.parametrize("identity", [None, SELF, TERMINAL, AppIdentity("", "")])
def test_no_text_is_read_for_unknown_self_or_terminal_apps(make_service, identity):
    service, platform, _metrics = make_service(FakePlatform(identity))

    future = service.request(include_text=True, include_selection=True)

    assert future.done() and future.result() == FocusSnapshot(identity)
    assert platform.reads == [] and service._worker is None


@pytest.mark.parametrize("excluded", [["Outlook"], ["OUTLOOK.EXE"], ["outlook"]])
def test_excluded_apps_are_never_read(make_service, excluded):
    service, platform, _metrics = make_service(excluded=excluded)

    assert _snapshot(service.request(include_text=True, include_selection=True)) == FocusSnapshot(OUTLOOK)
    assert platform.reads == []


def test_unreadable_settings_fail_closed(make_service):
    def broken():
        raise OSError("settings locked")

    service, platform, _metrics = make_service(settings=broken)

    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK)
    assert platform.reads == []


def test_nothing_raises(make_service):
    service, platform, _metrics = make_service()
    platform.identity_error = OSError("no desktop")
    assert service.request(include_text=True).result(timeout=0) == FocusSnapshot()
    assert service.current_identity() is None

    platform.identity_error = None
    platform.read_error = RuntimeError("provider died")
    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK)

    platform.read_error = None
    def broken_metrics(**_values):
        raise ValueError("metrics")
    service._metrics = broken_metrics
    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK, CONTEXT)


def test_a_reader_that_cannot_start_turns_text_off(make_service):
    service, platform, _metrics = make_service()
    platform.reader_error = OSError("no UI Automation")

    assert _snapshot(service.request(include_text=True)) == FocusSnapshot(OUTLOOK)
    assert _wait_for(lambda: service._text_off)
    future = service.request(include_text=True)
    assert future.done() and future.result() == FocusSnapshot(OUTLOOK)


def test_platforms_without_text_only_name_the_app(make_service):
    service, platform, _metrics = make_service(FakePlatform(text=False))

    future = service.request(include_text=True, include_selection=True)

    assert future.result(timeout=0) == FocusSnapshot(OUTLOOK)
    assert service._worker is None


def test_async_identity_resolves_on_its_own_thread_within_the_deadline(make_service):
    service, platform, _metrics = make_service(FakePlatform(sync=False, text=False), deadline_s=0.3)
    calls = []
    original = platform.identity
    platform.identity = lambda: calls.append(threading.current_thread()) or original()

    assert _snapshot(service.request(include_text=False)) == FocusSnapshot(OUTLOOK)
    assert calls and calls[0] is not threading.current_thread()
    assert service.current_identity() is None

    platform.identity_delay = 1.5
    started = time.monotonic()
    assert _snapshot(service.request(include_text=False)) == FocusSnapshot()
    assert time.monotonic() - started < 1.2


def test_current_identity_is_synchronous_where_identity_is(make_service):
    service, platform, _metrics = make_service()
    assert service.current_identity() == OUTLOOK
    platform.current = SLACK
    assert service.current_identity() == SLACK


def test_reread_reads_again_only_while_the_same_app_has_focus(make_service):
    service, platform, _metrics = make_service()
    got = []
    done = threading.Event()

    def callback(context):
        got.append((context, threading.current_thread()))
        done.set()

    service.reread(OUTLOOK, callback)
    assert done.wait(WAIT)
    assert got[0][0] == CONTEXT and got[0][1] is platform.read_threads[0]
    assert platform.reads == [("outlook.exe", True, True)]

    done.clear()
    platform.current = AppIdentity("outlook.exe", "Outlook", pid=10, window="0x9")
    service.reread(OUTLOOK, callback)
    assert done.wait(WAIT) and got[-1][0] is None
    assert len(platform.reads) == 1


def test_reread_answers_none_when_it_cannot_read(make_service):
    service, platform, _metrics = make_service()
    answers = []
    service.reread(TERMINAL, answers.append)
    service.reread(None, answers.append)

    platform.gates["outlook.exe"] = threading.Event()
    service.request(include_text=True)
    assert platform.blocked.wait(WAIT)
    service.reread(OUTLOOK, answers.append)
    assert answers == [None, None, None]

    def broken(_context):
        raise RuntimeError("callback")
    service.reread(TERMINAL, broken)

    async_service, _platform, _metrics = make_service(FakePlatform(sync=False))
    async_service.reread(OUTLOOK, answers.append)
    assert answers == [None] * 4


def test_shutdown_stops_the_thread_and_empties_later_snapshots(make_service):
    service, platform, _metrics = make_service()
    _snapshot(service.request(include_text=True))

    service.shutdown()

    assert platform.readers[0].closed.wait(WAIT)
    assert service.request(include_text=True).result(timeout=0) == FocusSnapshot()
    assert service.current_identity() is None
    answers = []
    service.reread(OUTLOOK, answers.append)
    assert answers == [None]


def test_recent_apps_are_newest_first_without_repeats_or_self(make_service):
    service, platform, _metrics = make_service()
    gmail = AppIdentity("chrome.exe", "Chrome", title_hint="Gmail")
    for identity in (OUTLOOK, SLACK, SELF, gmail, OUTLOOK, None):
        platform.current = identity
        service.request(include_text=False)

    assert service.recent_apps() == (OUTLOOK, gmail, SLACK)
    focus_context.set_service(service)
    assert focus_context.recent_apps() == (OUTLOOK, gmail, SLACK)
    focus_context.set_service(focus_context.NullCaptureService())
    assert focus_context.recent_apps() == ()


def test_the_default_service_is_the_real_one_for_this_platform():
    focus_context.set_service(None)
    try:
        service = focus_context.get_service()
        if sys.platform in ("win32", "darwin") or sys.platform.startswith("linux"):
            assert isinstance(service, CaptureService)
        future = service.request(include_text=False)
        assert future.result(timeout=WAIT) is not None
    finally:
        focus_context.shutdown_service()
        focus_context.set_service(focus_context.NullCaptureService())


def test_capture_metrics_are_allowlisted():
    assert {"context_capture_ms", "context_capture_timeouts"} <= diagnostics.METRIC_NAMES
