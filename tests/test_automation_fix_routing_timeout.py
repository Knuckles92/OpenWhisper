"""Route by voice gives up on the judge after ROUTE_TIMEOUT_S of wall-clock time,
or at once on cancel (review: two waits that each stayed under the socket
timeout added up to 2.4 s)."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from services import typesafe
from services.actions import routing
from tests import test_automation_routing_decide as base
from tests.test_automation_routing_decide import (
    ASKED,
    FakeJudge,
    answering,
    decided,
    make_job,
    make_settings,
)

bus = base.bus

#: Generous for a loaded test machine, and still well below the 2.4 s seen.
SLACK_S = 0.4


class SlowJudge(FakeJudge):
    """Answers after ``delay`` seconds, unless released earlier."""

    def __init__(self, delay: float, answer=None):
        super().__init__(answer or answering("c1", 0.99))
        self.delay = delay
        self.release = threading.Event()

    def ask(self, state, questions, timeout_s=None):
        self.release.wait(self.delay)
        return super().ask(state, questions, timeout_s)


@pytest.fixture
def trickle():
    """A local judge endpoint that answers in two parts, each just under the timeout."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    closing = threading.Event()

    def serve():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            request = routing.plan(ASKED, routing.ROUTE_AUTO, routing.load_actions(make_settings()))
            body = json.dumps({"answers": answering("c1", 0.99)(
                request.state, request.questions)}).encode()
            closing.wait(1.2)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode())
                closing.wait(1.2)
                conn.sendall(body)
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/systemone"
    closing.set()
    server.close()


def test_a_judge_answering_in_slow_parts_is_cut_off_at_the_timeout(bus, trickle):
    judge = typesafe.TypeSafeJudge("test-key", endpoint=trickle)
    job = make_job()
    started = time.monotonic()
    decision = routing.decide(ASKED, job, make_settings(), judge=judge)
    elapsed = time.monotonic() - started

    assert elapsed < routing.ROUTE_TIMEOUT_S + SLACK_S
    assert decision == routing.RouteDecision("", "", 0.0, failure="timeout")
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": None,
                             "fallback": True}]


def test_a_judge_that_never_answers_is_cut_off_at_the_timeout(bus):
    judge = SlowJudge(30)
    started = time.monotonic()
    decision = routing.decide(ASKED, make_job(), make_settings(), judge=judge)
    elapsed = time.monotonic() - started
    judge.release.set()

    assert routing.ROUTE_TIMEOUT_S - 0.05 <= elapsed < routing.ROUTE_TIMEOUT_S + SLACK_S
    assert decision.failure == "timeout" and decision.action_id == ""


def test_an_answer_inside_the_timeout_still_routes(bus):
    judge = SlowJudge(0.3)
    decision = routing.decide(ASKED, make_job(), make_settings(), judge=judge)
    assert decision is not None and decision.action_id == "c1" and decision.failure == ""


def test_a_cancel_stops_waiting_at_once(bus):
    judge = SlowJudge(30)
    cancel = threading.Event()
    result = {}

    def run():
        result["decision"] = routing.decide(ASKED, make_job(), make_settings(), judge=judge,
                                            cancel=cancel)
        result["at"] = time.monotonic()

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.2)
    cancel.set()
    canceled_at = time.monotonic()
    worker.join(5)
    judge.release.set()

    assert result["decision"] is None
    assert result["at"] - canceled_at < 0.25
    # A canceled dictation was neither routed nor pasted: nothing to announce.
    assert decided(bus) == []


def test_a_cancel_before_routing_sends_nothing(bus):
    judge = FakeJudge(answering("c1", 0.99))
    cancel = threading.Event()
    cancel.set()
    assert routing.decide(ASKED, make_job(), make_settings(), judge=judge, cancel=cancel) is None
    assert judge.calls == [] and decided(bus) == []
