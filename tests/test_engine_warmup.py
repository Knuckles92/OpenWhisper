"""Optional speech engines take their first-decode cost before the first dictation."""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from config import config
# At collection, before test_application_controller swaps stubs into
# sys.modules: importing it after those tests re-imports services.models and
# sqlalchemy rejects the second declaration of its tables.
from services.application_controller import ApplicationController
from services.local_asr import cache
from transcriber.optional_backend import LocalSpeechBackend


class FakeProcess:
    """Stands in for SpeechProcess: records requests and can hold a decode open."""

    def __init__(self, _python, *, fail_load=False, fail_transcribe=False, hold=False,
                 on_transcribe=None):
        self.requests = []
        self.closed = False
        self.fail_load = fail_load
        self.fail_transcribe = fail_transcribe
        self.hold = hold
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.on_transcribe = on_transcribe
        self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)

    def request(self, op, *, timeout=180., **payload):
        if self.closed:
            raise RuntimeError("Transcription canceled")
        entry = dict(payload, op=op)
        if payload.get("audio_path"):
            # The caller deletes the file once the request returns.
            entry["audio"] = np.fromfile(payload["audio_path"], dtype=np.float32)
        self.requests.append(entry)
        if op == "load":
            if self.fail_load:
                raise RuntimeError("native load failed")
            return {"device": payload["device"]}
        if self.on_transcribe:
            self.on_transcribe()
        if self.hold:
            self.entered.set()
            self.resume.wait(5)
            if self.closed:
                raise RuntimeError("Transcription canceled")
        if self.fail_transcribe:
            raise RuntimeError("CUDA error: out of memory")
        return {"text": "", "segments": []}

    def close(self):
        self.closed = True
        self.resume.set()

    def ops(self):
        return [r["op"] for r in self.requests]


@pytest.fixture
def workers(monkeypatch):
    """Route every speech worker the backend starts to a FakeProcess."""
    started = []
    options = {}

    def spawn(python):
        process = FakeProcess(python, **options)
        started.append(process)
        return process

    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.is_installed", lambda _: True)
    monkeypatch.setattr("services.components.component_dir", lambda _: ".")
    monkeypatch.setattr(cache, "is_cached", lambda _: True)
    monkeypatch.setattr(cache, "load_path", lambda _: "model.gguf")
    monkeypatch.setattr("services.local_asr.process.SpeechProcess", spawn)
    return SimpleNamespace(started=started, options=options)


def _loaded(backend_id="parakeet", device="cuda"):
    backend = LocalSpeechBackend(backend_id, device=device)
    backend.reload_model()
    assert backend.is_available()
    return backend


def test_warmup_sends_quiet_non_silent_audio_through_transcribe(workers):
    backend = _loaded()
    seen_transcribing = []
    workers.started[0].on_transcribe = lambda: seen_transcribing.append(backend.is_transcribing)

    assert backend.warmup() is True
    assert backend.warmup() is True

    process = workers.started[0]
    assert process.ops() == ["load", "transcribe", "transcribe"]
    first, second = process.requests[1]["audio"], process.requests[2]["audio"]
    assert len(first) == 16000
    # _transcribe_audio would drop a window this quiet or quieter, so the
    # warmup must stay above it to reach the GPU at all.
    assert np.max(np.abs(first)) > .00025
    assert np.max(np.abs(first)) < .1
    assert np.array_equal(first, second)
    # A cancel press while idle must not see a job to tear down.
    assert seen_transcribing == [False, False]


def test_warmup_failure_is_non_fatal_and_keeps_engine_loaded(workers, caplog):
    workers.options["fail_transcribe"] = True
    backend = _loaded()

    with caplog.at_level(logging.WARNING, logger="transcriber.optional_backend"):
        assert backend.warmup() is False

    assert backend.is_available()
    assert "warmup failed" in caplog.text
    assert backend._decode_lock.acquire(blocking=False)
    backend._decode_lock.release()


def test_warmup_logs_its_duration(workers, caplog):
    backend = _loaded()
    with caplog.at_level(logging.INFO, logger="transcriber.optional_backend"):
        backend.warmup()
    assert "Parakeet warmed up in" in caplog.text


def test_warmup_skips_an_unloaded_engine(workers):
    backend = LocalSpeechBackend("parakeet", device="cuda")
    assert backend.warmup() is False
    assert workers.started == []


def test_warmup_never_queues_behind_another_decode(workers):
    backend = _loaded()
    with backend._decode_lock:
        assert backend.warmup() is False
    assert workers.started[0].ops() == ["load"]


@pytest.mark.parametrize("interrupt", ["cleanup", "cancel_transcription"])
def test_cleanup_during_warmup_ends_it_quietly(workers, caplog, interrupt):
    workers.options["hold"] = True
    backend = _loaded()
    process = workers.started[0]
    result = []
    with caplog.at_level(logging.INFO, logger="transcriber.optional_backend"):
        thread = threading.Thread(target=lambda: result.append(backend.warmup()))
        thread.start()
        assert process.entered.wait(2)
        getattr(backend, interrupt)()
        thread.join(2)

    assert result == [False]
    assert "stopped by a reload or cancel" in caplog.text
    assert "warmup failed" not in caplog.text
    assert backend._decode_lock.acquire(blocking=False)
    backend._decode_lock.release()


def test_reload_during_warmup_leaves_the_new_worker_untouched(workers):
    workers.options["hold"] = True
    backend = _loaded()
    old = workers.started[0]
    result = []
    thread = threading.Thread(target=lambda: result.append(backend.warmup()))
    thread.start()
    assert old.entered.wait(2)
    workers.options["hold"] = False
    backend.reload_model()
    thread.join(2)

    assert result == [False]
    assert old.closed
    new = workers.started[1]
    assert backend._process is new and backend.is_available()
    # The stale warmup must not have spilled a decode onto the new worker.
    assert new.ops() == ["load"]


def test_generation_change_while_waiting_for_the_lock_skips_the_decode(workers, monkeypatch):
    backend = _loaded()
    process = workers.started[0]
    lock = backend._decode_lock

    class ReloadingLock:
        """A reload lands between reading the generation and taking the lock."""

        def acquire(self, blocking=True):
            backend._generation += 1
            return lock.acquire(blocking)

        def release(self):
            lock.release()

    monkeypatch.setattr(backend, "_decode_lock", ReloadingLock())
    assert backend.warmup() is False
    assert process.ops() == ["load"]
    assert lock.acquire(blocking=False)
    lock.release()


class _Signal:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def emit(self, *args):
        self.events.append((self.name, *args))


def _controller(backend, events):
    """The controller's reload methods bound onto a minimal stand-in."""
    controller = SimpleNamespace(
        _engine_lock=threading.RLock(),
        _reload_in_flight=True,
        _pending_streaming_setup=True,
        current_backend=backend,
        _current_model_name=backend.backend_id,
        transcription_backends={backend.backend_id: backend},
        recorder=SimpleNamespace(is_recording=False),
        is_meeting_active=lambda: False,
        is_transcribing=lambda: False,
        ensure_local_model_available=Mock(),
        _engine_released_for_lease=False,
        _restore_after_reload=False,
        _reload_handoff_lock=threading.Lock(),
        executor=Mock(),
    )
    for name in ("device_info_update", "status_update", "engine_busy_changed",
                 "runtime_consent_requested", "streaming_setup_requested"):
        setattr(controller, name, _Signal(name, events))
    for name in ("_reload_worker", "_reload_selected_engine",
                 "_finish_speech_reload", "_flush_pending_streaming_setup",
                 "release_local_engine", "restore_local_engine",
                 "_submit_restore_reload"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    return controller


def _names(events):
    return [event[0] for event in events]


def test_reload_warms_after_load_then_clears_busy_and_sets_up_preview(workers):
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)
    lock_free = []

    def probe_engine_lock():
        # Another thread (a Meeting Mode lease) can take the engine lock.
        if controller._engine_lock.acquire(timeout=0):
            controller._engine_lock.release()
            lock_free.append(True)
        else:
            lock_free.append(False)

    def on_transcribe():
        events.append(("warmup",))
        probe = threading.Thread(target=probe_engine_lock)
        probe.start()
        probe.join(2)

    workers.options["on_transcribe"] = on_transcribe
    controller._reload_worker()

    assert workers.started[0].ops() == ["load", "transcribe"]
    names = _names(events)
    # Ready as soon as the model is loaded, so a recording may start while it
    # warms; busy and the shared-worker preview wait for the warmup.
    assert names.index("device_info_update") < names.index("warmup")
    assert names.index("warmup") < names.index("engine_busy_changed")
    assert names.index("engine_busy_changed") < names.index("streaming_setup_requested")
    assert ("device_info_update", "Parakeet TDT 0.6B v3 | cuda", True) in events
    assert ("engine_busy_changed", False) in events
    assert lock_free == [True]
    assert not controller._reload_in_flight
    assert "runtime_consent_requested" not in names


def test_failed_warmup_still_finishes_the_reload(workers):
    workers.options["fail_transcribe"] = True
    events = []
    backend = LocalSpeechBackend("nemotron", device="cuda")
    controller = _controller(backend, events)

    controller._reload_worker()

    assert workers.started[0].ops() == ["load", "transcribe"]
    assert backend.is_available()
    assert ("engine_busy_changed", False) in events
    assert "streaming_setup_requested" in _names(events)
    assert not controller._reload_in_flight


def test_failed_load_is_not_warmed(workers):
    workers.options["fail_load"] = True
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)

    controller._reload_worker()

    assert workers.started[0].ops() == ["load"]
    assert ("status_update", "Engine load failed: native load failed") in events
    assert ("engine_busy_changed", False) in events
    assert "streaming_setup_requested" in _names(events)
    assert not controller._reload_in_flight


def test_missing_model_is_not_warmed(workers, monkeypatch):
    monkeypatch.setattr(cache, "is_cached", lambda _: False)
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)

    controller._reload_worker()

    assert workers.started == []
    controller.ensure_local_model_available.assert_called_once()
    assert ("engine_busy_changed", False) in events
    assert not controller._reload_in_flight


def test_missing_runtime_is_not_warmed(workers, monkeypatch):
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)

    controller._reload_worker()

    assert workers.started == []
    assert ("runtime_consent_requested", "parakeet-v3") in events
    assert not controller._reload_in_flight


def test_engines_outside_the_warmup_list_are_not_warmed(workers):
    assert "moonshine" not in config.SPEECH_WARMUP_BACKENDS
    events = []
    backend = LocalSpeechBackend("moonshine", device="cpu")
    controller = _controller(backend, events)

    controller._reload_worker()

    assert workers.started[0].ops() == ["load"]
    assert backend.is_available()
    assert ("engine_busy_changed", False) in events


def test_engine_switched_during_load_is_released_not_warmed(workers):
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)
    load = backend.reload_model

    def switch_while_loading():
        load()
        controller.current_backend = object()

    backend.reload_model = switch_while_loading
    controller._reload_worker()

    assert workers.started[0].ops() == ["load"]
    assert workers.started[0].closed and not backend.is_available()
    assert ("engine_busy_changed", False) in events


def test_meeting_lease_during_warmup_ends_it_and_finishes_the_reload(workers):
    workers.options["hold"] = True
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)

    thread = threading.Thread(target=controller._reload_worker)
    thread.start()
    assert _wait_for_worker(workers).entered.wait(2)
    # release_local_engine takes the engine lock and cleans up; it must not
    # wait for the warmup to finish.
    assert controller._engine_lock.acquire(timeout=1)
    try:
        backend.cleanup()
    finally:
        controller._engine_lock.release()
    thread.join(2)

    assert not thread.is_alive()
    assert not backend.is_available()
    assert ("engine_busy_changed", False) in events
    assert not controller._reload_in_flight


def test_lease_returned_during_warmup_reloads_the_engine_it_closed(workers):
    # A meeting that fails fast: release and restore both land while the
    # reload is still warming, so its finish has to load the engine again.
    workers.options["hold"] = True
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)
    controller.streaming_runtime = Mock()
    controller._streaming_backend = None
    controller.transcription_backends = {"parakeet": backend}

    thread = threading.Thread(target=controller._reload_worker)
    thread.start()
    assert _wait_for_worker(workers).entered.wait(2)
    assert controller.release_local_engine()
    controller.restore_local_engine()
    thread.join(2)

    assert not thread.is_alive()
    controller.executor.submit.assert_called_once_with(controller._reload_worker)
    assert controller._reload_in_flight
    assert "runtime_consent_requested" not in _names(events)
    assert events[-1] == ("status_update", "Reloading speech engine...")


def test_lease_still_held_after_warmup_asks_for_no_runtime(workers):
    workers.options["hold"] = True
    events = []
    backend = LocalSpeechBackend("parakeet", device="cuda")
    controller = _controller(backend, events)
    controller.streaming_runtime = Mock()
    controller._streaming_backend = None
    controller.transcription_backends = {"parakeet": backend}

    thread = threading.Thread(target=controller._reload_worker)
    thread.start()
    assert _wait_for_worker(workers).entered.wait(2)
    assert controller.release_local_engine()  # the meeting keeps it
    thread.join(2)

    assert not thread.is_alive()
    assert "runtime_consent_requested" not in _names(events)
    controller.executor.submit.assert_not_called()
    assert ("engine_busy_changed", False) in events


def _wait_for_worker(workers, timeout=2.):
    deadline = time.monotonic() + timeout
    while not workers.started:
        if time.monotonic() > deadline:
            raise AssertionError("the reload never started a worker")
        time.sleep(.01)
    return workers.started[0]
