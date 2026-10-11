"""When the judge can't answer, Route by voice says so instead of looking like
"no action matched" (review: route failures looked like no match)."""

from __future__ import annotations

import pytest

from services import credentials, typesafe
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
no_env_keys = base.no_env_keys

UNREACHABLE = routing.RouteDecision("", "", 0.0, failure="unreachable")


@pytest.mark.parametrize("judge", [
    FakeJudge(None),                                   # TypeSafeJudge's failure (HTTP, network)
    FakeJudge(raises=RuntimeError("boom")),            # a judge that raises
    FakeJudge({"route": {"choice": "gone", "confidence": 1.0}}),   # not a candidate
    FakeJudge({"payload": {"choice": routing.NONE}}),  # no route answer
], ids=["no-answer", "raises", "unknown-choice", "no-route"])
def test_a_judge_that_could_not_answer_is_a_failure_not_a_miss(bus, judge):
    job = make_job()
    assert routing.decide(ASKED, job, make_settings(), judge=judge) == UNREACHABLE
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": None,
                             "fallback": True}]


def test_a_missing_key_at_call_time_is_a_failure_and_sends_nothing(bus, monkeypatch, no_env_keys):
    credentials.store().delete(typesafe.CREDENTIAL_ENV)
    monkeypatch.setattr(typesafe, "_http_post", lambda *a, **k: pytest.fail("sent"))
    job = make_job(routing.ROUTE_HOTKEY)
    assert routing.decide(ASKED, job, make_settings()) == UNREACHABLE
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": None,
                             "fallback": True}]


def test_an_answer_below_the_threshold_or_none_is_still_no_match(bus):
    for answer in (answering("c1", routing.ROUTE_CONFIDENCE - 0.1), answering(routing.NONE, 0.9)):
        assert routing.decide(ASKED, make_job(), make_settings(), judge=FakeJudge(answer)) is None


def test_words_that_name_no_action_are_no_match_without_asking(bus):
    judge = FakeJudge(None)
    assert routing.decide("Remind me later.", make_job(), make_settings(), judge=judge) is None
    assert judge.calls == [] and decided(bus) == []
