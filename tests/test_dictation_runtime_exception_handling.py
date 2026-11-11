"""Narrowed exception handlers on the dictation path still absorb what they expect."""

from __future__ import annotations

from concurrent.futures import Future

from services import dictation_pipeline
from services.runtime import command


def test_command_resolve_ignores_a_future_resolved_in_between():
    future: Future = Future()
    future.set_result("first")
    # done() is checked first; force the race by resolving a pending future twice.
    command._resolve(future, "second")
    assert future.result() == "first"

    pending: Future = Future()
    original = pending.done
    pending.done = lambda: False  # type: ignore[method-assign]
    pending.set_result("first")
    command._resolve(pending, "second")  # InvalidStateError is swallowed
    pending.done = original  # type: ignore[method-assign]
    assert pending.result() == "first"


def test_command_snapshot_is_none_when_not_ready_cancelled_or_failed():
    assert command._snapshot(Future(), 0.01) is None
    cancelled: Future = Future()
    cancelled.cancel()
    assert command._snapshot(cancelled, 0.01) is None
    failed: Future = Future()
    failed.set_exception(RuntimeError("capture broke"))
    assert command._snapshot(failed, 0.01) is None


def test_pipeline_resolved_is_none_when_not_ready_cancelled_or_failed():
    assert dictation_pipeline._resolved(Future(), 0.01) is None
    assert dictation_pipeline._resolved(Future(), 0) is None
    cancelled: Future = Future()
    cancelled.cancel()
    assert dictation_pipeline._resolved(cancelled, 0.01) is None
    failed: Future = Future()
    failed.set_exception(RuntimeError("capture broke"))
    assert dictation_pipeline._resolved(failed, 0.01) is None
    done: Future = Future()
    done.set_result("value")
    assert dictation_pipeline._resolved(done, 0) == "value"
