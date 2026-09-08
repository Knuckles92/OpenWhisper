"""Explicit note requests run without speech and share the scheduler worker."""
import threading
from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from meeting.agent.prompts import build_notes_user_prompt
from meeting.agent.scheduler import CheckpointScheduler
from meeting.interfaces import AgentResult, OpResult
from meeting.web.api import create_app
from meeting.web.ws import WsHub
from tests.test_meeting_notes_agent import FakeAgent, FakeEngine, _seg
from tests.test_meeting_web_auth import FakeEngine as WebEngine, FakeRepo, HOST_TOKEN, GUEST_TOKEN


def test_prompt_includes_user_context_but_not_deleted_notes():
    prompt = build_notes_user_prompt({"cards": {"user_notes": [
        {"text": "Customer spells it Acme", "status": "edited"},
        {"text": "deleted-secret", "status": "removed"},
    ]}}, [])
    assert "Customer spells it Acme" in prompt
    assert "deleted-secret" not in prompt


def test_adjustment_replaces_automatic_append_instructions():
    prompt = build_notes_user_prompt({"cards": {}, "note_adjustment_request": "Use bullets"}, [])
    assert "Use bullets" in prompt
    assert "No new speech is required" in prompt
    assert "Extend the notes page" not in prompt


def test_request_runs_without_new_speech_and_preserves_watermarks():
    agent = FakeAgent()
    agent.supports_notes_pass = True
    engine = FakeEngine([_seg("sg_old", 10)])
    scheduler = CheckpointScheduler(engine, agent)
    scheduler.start()
    try:
        result = scheduler.request_note_adjustment("Shorten the first section").result(timeout=3)
        assert result.ok
        payload = agent.calls[0]
        assert payload.is_notes
        assert payload.state_snapshot["note_adjustment_request"] == "Shorten the first section"
        assert payload.new_segments[0]["id"] == "sg_old"
        assert scheduler._sent_starts == {}
        assert scheduler._notes_sent_starts == {}
        assert "note_adjustment_request" not in engine.store.snapshot()
    finally:
        scheduler.stop()


def test_requests_are_serial_and_queued_request_is_settled_on_stop():
    entered, release = threading.Event(), threading.Event()
    agent = FakeAgent()
    agent.supports_notes_pass = True
    def checkpoint(payload):
        entered.set()
        release.wait(timeout=3)
        return AgentResult(ok=True)
    agent.checkpoint = checkpoint
    scheduler = CheckpointScheduler(FakeEngine([]), agent)
    scheduler.start()
    try:
        first = scheduler.request_note_adjustment("First")
        assert entered.wait(timeout=2)
        second = scheduler.request_note_adjustment("Second")
        assert not second.done()
        scheduler.prepare_for_end()
        assert not second.result(timeout=1).ok
        release.set()
        assert first.result(timeout=2).ok
        with pytest.raises(RuntimeError):
            scheduler.request_note_adjustment("Too late")
    finally:
        release.set()
        scheduler.stop()


def test_agent_failure_reaches_requester():
    agent = FakeAgent(fail_times=1)
    agent.supports_notes_pass = True
    scheduler = CheckpointScheduler(FakeEngine([]), agent)
    scheduler.start()
    try:
        assert not scheduler.request_note_adjustment("Shorten").result(timeout=3).ok
    finally:
        scheduler.stop()


@pytest.mark.parametrize("token", [HOST_TOKEN, GUEST_TOKEN])
def test_request_api_auth_validation_and_result(token):
    engine, repo = WebEngine(), FakeRepo()
    calls = []
    def request(text):
        calls.append(text)
        future = Future()
        future.set_result(AgentResult(ok=True, op_results=[OpResult(ok=True, op={})]))
        return future
    engine.request_note_adjustment = request
    with TestClient(create_app(engine, repo, WsHub(engine, repo))) as client:
        url = "/api/meeting/notes/request"
        assert client.post(url, json={"text": "Shorten"}).status_code == 401
        for invalid in ("", " " * 5, "x" * 4001, 42):
            assert client.post(url, params={"token": token}, json={"text": invalid}).status_code == 400
        assert calls == []
        response = client.post(url, params={"token": token}, json={"text": "Shorten"})
        assert response.status_code == 200
        assert response.json()["applied"] == 1
        assert calls == ["Shorten"]
