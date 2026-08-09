"""
Tests for the headless insight re-run over a stored meeting: guard rails,
op application through the state store, persistence, and agent-core teardown.
"""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.interfaces import AgentResult, TranscriptSegment
from meeting.reinsight import rerun_insights


@pytest.fixture
def db(tmp_path):
    from services.database import DatabaseManager
    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    from meeting.persist.repository import SqlMeetingRepository
    return SqlMeetingRepository(db=db)


def make_meeting(repo, meeting_id="m_rerun", state_json=None):
    repo.create_meeting(
        id=meeting_id, title="Budget sync", status="ended",
        started_at=datetime.now().isoformat(),
        host_token="host-token", guest_token="guest-token",
        cloud_enabled=False, spool_dir="/tmp/spool",
        agent_provider="openrouter", agent_model="test/model",
        state_json=state_json, state_seq=0,
    )
    return meeting_id


def add_transcript(repo, meeting_id):
    repo.add_segments([
        TranscriptSegment(segment_id="sg_1", meeting_id=meeting_id, chunk_id=None,
                          channel="mic", start_s=0.0, end_s=2.0,
                          text="We should ship the budget review on Friday."),
        TranscriptSegment(segment_id="sg_2", meeting_id=meeting_id, chunk_id=None,
                          channel="loopback", start_s=2.0, end_s=4.0,
                          text="Agreed, Friday works."),
    ])


class FakeAgentCore:
    """Minimal ``AgentCore`` that replays a fixed op batch (or raises)."""

    def __init__(self, ops=None, raises=None):
        self.ops = ops or []
        self.raises = raises
        self.cfg = None
        self.tools = None
        self.payload = None
        self.shutdown_calls = 0
        self.canceled = False

    def initialize(self, cfg, tools):
        self.cfg = cfg
        self.tools = tools

    def checkpoint(self, payload):
        raise AssertionError("re-run must not fire rolling checkpoints")

    def consolidate(self, payload):
        self.payload = payload
        if self.raises:
            raise RuntimeError(self.raises)
        results = self.tools.apply_agent_ops(self.ops)
        return AgentResult(ok=True, op_results=results)

    def cancel(self):
        self.canceled = True

    def is_healthy(self):
        return True

    def shutdown(self):
        self.shutdown_calls += 1


def install_core(monkeypatch, core):
    """Point ``rerun_insights`` at a fake core and record the factory args."""
    calls = []

    def factory(kind, payload_dir=None):
        calls.append((kind, payload_dir))
        return core

    monkeypatch.setattr("meeting.reinsight.create_agent_core", factory)
    return calls


class TestGuards:
    def test_unknown_meeting_rejected(self, repo):
        with pytest.raises(ValueError, match="unknown meeting"):
            rerun_insights(repo, "m_nope", provider="openrouter", model="m")

    def test_meeting_without_transcript_rejected(self, repo):
        make_meeting(repo)
        with pytest.raises(ValueError, match="no transcript"):
            rerun_insights(repo, "m_rerun", provider="openrouter", model="m")


class TestHappyPath:
    def test_ops_land_and_persist(self, repo, monkeypatch):
        make_meeting(repo)
        add_transcript(repo, "m_rerun")
        core = FakeAgentCore(ops=[
            {"op": "add_item", "card": "key_points",
             "text": "Budget review ships Friday", "evidence": ["sg_1"]},
            {"op": "set_topic", "text": "Budget review", "evidence": ["sg_2"]},
            # Rejected: evidence validation must behave as it does live.
            {"op": "add_item", "card": "decisions", "text": "Bogus",
             "evidence": ["sg_missing"]},
        ])
        calls = install_core(monkeypatch, core)

        result = rerun_insights(
            repo, "m_rerun", provider="openrouter", model="test/model",
            agent_core_kind="pi", sidecar_payload_dir="/payload",
        )

        assert result["ok"] is True
        assert result["error"] is None
        assert result["applied"] == 2  # the unknown-evidence op was rejected
        assert calls == [("pi", "/payload")]

        state = result["state"]
        assert state["topic"]["current"] == "Budget review"
        key_points = state["cards"]["key_points"]
        assert [item["text"] for item in key_points] == ["Budget review ships Friday"]
        assert key_points[0]["status"] == "proposed"
        assert key_points[0]["author_type"] == "agent"
        assert state["cards"]["decisions"] == []

        # Write-through persistence, exactly as in a live meeting.
        meeting = repo.get_meeting("m_rerun")
        stored = json.loads(meeting["state_json"])
        assert stored["topic"]["current"] == "Budget review"
        assert meeting["state_seq"] == stored["seq"] == state["seq"]

        assert core.shutdown_calls == 1

    def test_agent_receives_full_transcript_and_prompt(self, repo, monkeypatch):
        make_meeting(repo)
        add_transcript(repo, "m_rerun")
        core = FakeAgentCore()
        install_core(monkeypatch, core)

        rerun_insights(repo, "m_rerun", provider="openrouter", model="test/model")

        assert core.payload.is_consolidation is True
        assert [seg["id"] for seg in core.payload.new_segments] == ["sg_1", "sg_2"]
        assert core.payload.request_id
        assert core.cfg.provider == "openrouter"
        assert core.cfg.model == "test/model"
        assert "meeting-intelligence copilot" in core.cfg.system_prompt

    def test_existing_state_is_preserved_and_protected(self, repo, monkeypatch):
        """Human-touched items survive a re-run, as they do live."""
        from meeting.state.schema import CardItem, MeetingState
        state = MeetingState(meeting_id="m_rerun", seq=4, title="Budget sync")
        state.cards["key_points"].append(CardItem(
            id="it_human", card="key_points", text="Human wrote this",
            status="edited", author_type="user",
        ))
        make_meeting(repo, state_json=json.dumps(state.to_dict()))
        add_transcript(repo, "m_rerun")
        core = FakeAgentCore(ops=[
            {"op": "update_item", "id": "it_human", "base_revision": 1,
             "set": {"text": "Agent overwrite"}},
        ])
        install_core(monkeypatch, core)

        result = rerun_insights(repo, "m_rerun", provider="openrouter", model="m")

        assert result["applied"] == 0
        items = result["state"]["cards"]["key_points"]
        assert [item["text"] for item in items] == ["Human wrote this"]


class TestFailure:
    def test_agent_error_is_reported_not_raised(self, repo, monkeypatch):
        make_meeting(repo)
        add_transcript(repo, "m_rerun")
        core = FakeAgentCore(raises="model exploded")
        install_core(monkeypatch, core)

        result = rerun_insights(repo, "m_rerun", provider="openrouter", model="m")

        assert result["ok"] is False
        assert "model exploded" in result["error"]
        assert result["applied"] == 0
        assert result["state"]["meeting_id"] == "m_rerun"
        assert core.shutdown_calls == 1

    def test_core_creation_failure_is_reported(self, repo, monkeypatch):
        make_meeting(repo)
        add_transcript(repo, "m_rerun")

        def boom(kind, payload_dir=None):
            raise RuntimeError("no core")

        monkeypatch.setattr("meeting.reinsight.create_agent_core", boom)

        result = rerun_insights(repo, "m_rerun", provider="openrouter", model="m")

        assert result["ok"] is False
        assert "no core" in result["error"]
        assert result["applied"] == 0
