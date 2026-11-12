"""When the judge can't answer, Route by voice says so instead of looking like
"no action matched" (review: route failures looked like no match)."""

from __future__ import annotations

import pytest

from services import credentials, typesafe
from services.actions import routing
from tests import test_automation_routing_decide as base
from tests.test_automation_routing_decide import (
    ASKED,
    decided,
    make_job,
    make_settings,
)

bus = base.bus
no_env_keys = base.no_env_keys

UNREACHABLE = routing.RouteDecision("", "", 0.0, failure="unreachable")


def test_a_missing_key_at_call_time_is_a_failure_and_sends_nothing(bus, monkeypatch, no_env_keys):
    credentials.store().delete(typesafe.CREDENTIAL_ENV)
    monkeypatch.setattr(typesafe, "_http_post", lambda *a, **k: pytest.fail("sent"))
    job = make_job(routing.ROUTE_HOTKEY)
    assert routing.decide(ASKED, job, make_settings()) == UNREACHABLE
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": None,
                             "fallback": True}]
