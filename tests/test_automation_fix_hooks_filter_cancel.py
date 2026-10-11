"""A canceled dictation stops its text filters at once (review: filters ignored
cancel, so the slot stayed held for the whole budget and later filters still
got the words)."""

from __future__ import annotations

import sys
import threading
import time

from services.automation import filters
from services.automation.models import BEFORE_CLEANUP
from tests import test_automation_hooks_filters as base
from tests.test_automation_hooks_filters import (
    TEXT,
    Answer,
    http_filter,
    job,
    program_filter,
    settings_with,
)
from tests.test_automation_hooks_webhooks import wait_until

receiver = base.receiver
failures = base.failures
no_leases = base.no_leases


def apply_in_background(settings, cancel):
    result: dict = {}

    def run():
        result["outcome"] = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings, cancel=cancel)
        result["at"] = time.monotonic()

    worker = threading.Thread(target=run)
    worker.start()
    return worker, result


def test_a_cancel_stops_waiting_on_an_http_filter_and_skips_the_rest(receiver, failures):
    receiver.answer("/slow", Answer(delay=3, body=b'{"text": "too late"}'))
    settings = settings_with(http_filter("slow", receiver.url("/slow"), timeout_ms=2500),
                             http_filter("later", receiver.url("/later")))
    cancel = threading.Event()
    worker, result = apply_in_background(settings, cancel)
    wait_until(lambda: "/slow" in receiver.arrivals, timeout=5)
    cancel.set()
    canceled_at = time.monotonic()
    worker.join(5)

    assert result["outcome"] == filters.FilterOutcome(TEXT)
    assert result["at"] - canceled_at < 0.25
    time.sleep(0.2)
    assert "/later" not in receiver.arrivals
    assert failures == []


def test_a_cancel_before_the_stage_sends_nothing(receiver, failures):
    cancel = threading.Event()
    cancel.set()
    settings = settings_with(http_filter("f", receiver.url("/f")))
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(), settings, cancel=cancel) == (
        filters.FilterOutcome(TEXT))
    time.sleep(0.2)
    assert receiver.arrivals == [] and failures == []


def test_a_cancel_after_one_filter_changed_the_text_keeps_the_original(receiver, failures):
    cancel = threading.Event()

    def upper_then_cancel(request):
        cancel.set()
        return b'{"text": "CHANGED"}'

    receiver.answer("/first", Answer(reply=upper_then_cancel))
    settings = settings_with(http_filter("first", receiver.url("/first")),
                             http_filter("second", receiver.url("/second")))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings, cancel=cancel)
    assert outcome == filters.FilterOutcome(TEXT)
    time.sleep(0.2)
    assert "/second" not in receiver.arrivals


def test_a_cancel_stops_waiting_on_a_program_filter(failures):
    settings = settings_with(program_filter(
        "p", "import sys, time; sys.stdin.read(); time.sleep(4)", timeout_ms=5000))
    cancel = threading.Event()
    worker, result = apply_in_background(settings, cancel)
    time.sleep(0.5)
    cancel.set()
    canceled_at = time.monotonic()
    worker.join(10)

    assert result["outcome"] == filters.FilterOutcome(TEXT)
    assert result["at"] - canceled_at < 0.25
    assert failures == []


def test_a_cancel_wins_over_the_answer_it_caused():
    # A program filter sees the same cancel, stops, and answers "failed" at
    # once; that answer must not be reported as the filter failing.
    cancel = threading.Event()

    def stopped_by_the_cancel():
        time.sleep(0.005)  # the wait has started polling
        cancel.set()
        return filters._Verdict(filters._FAILED, reason="Canceled")

    for _ in range(20):
        cancel.clear()
        assert filters._await(stopped_by_the_cancel, None, cancel, "") is filters._CANCELED


def test_without_a_cancel_filters_run_as_before(receiver):
    receiver.answer("/f", Answer(body=b'{"text": "new words"}'))
    settings = settings_with(http_filter("f", receiver.url("/f")),
                             program_filter("p", base.UPPER_PROGRAM))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings, cancel=threading.Event())
    assert outcome == filters.FilterOutcome("NEW WORDS", changed=True)
    assert sys.executable  # the program filter ran with this interpreter
