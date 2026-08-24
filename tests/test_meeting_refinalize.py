"""Tests for the headless post-meeting finalization retry pipeline."""
import json
from datetime import datetime

import pytest


from meeting.interfaces import AgentResult, TranscriptSegment
from meeting.refinalize import rerun_finalization
from meeting.state.schema import CardItem, FinalizationState, MeetingState


def make_meeting(repo, meeting_id="m_retry", state_json=None, cloud_enabled=True,
                 **extra):
    fields = dict(
        id=meeting_id, title="Budget sync", status="ended",
        started_at=datetime.now().isoformat(),
        ended_at=datetime.now().isoformat(),
        host_token="host-token", guest_token="guest-token",
        cloud_enabled=cloud_enabled, spool_dir="/tmp/spool",
        agent_provider="openrouter", agent_model="test/model",
        state_json=state_json, state_seq=0,
    )
    fields.update(extra)
    repo.create_meeting(**fields)
    return meeting_id


def add_transcript(repo, meeting_id):
    repo.add_segments([
        TranscriptSegment(
            segment_id="sg_1", meeting_id=meeting_id, chunk_id=None,
            channel="mic", start_s=0.0, end_s=2.0,
            text="We should ship the budget review on Friday.",
        ),
        TranscriptSegment(
            segment_id="sg_2", meeting_id=meeting_id, chunk_id=None,
            channel="loopback", start_s=2.0, end_s=4.0,
            text="Agreed, Friday works.",
        ),
    ])


def seeded_state(meeting_id, steps, cloud_enabled=True, card_deferred=False):
    state = MeetingState(
        meeting_id=meeting_id,
        status="ended",
        cloud_enabled=cloud_enabled,
        title="Budget sync",
        finalization=FinalizationState(
            status="failed",
            message="needs retry",
            steps=steps,
            total_steps=len(steps),
            card_deferred=card_deferred,
        ),
    )
    return json.dumps(state.to_dict())


DEFAULT_STEPS = [
    {
        "id": "redecode",
        "name": "Audio Re-transcription",
        "status": "failed",
        "detail": "Re-decoding failed; kept live transcript",
    },
    {
        "id": "polish",
        "name": "Transcript Cleanup",
        "status": "completed",
        "detail": "Done",
    },
    {
        "id": "consolidation",
        "name": "Summary & Action Items",
        "status": "completed",
        "detail": "Done",
    },
    {
        "id": "finalize",
        "name": "State Finalization",
        "status": "completed",
        "detail": "Done",
    },
]


class FakeAgentCore:
    """Minimal agent that can polish and consolidate."""

    def __init__(self, ops=None, fail_polish=False):
        self.ops = ops or []
        self.fail_polish = fail_polish
        self.cfg = None
        self.tools = None
        self.payload = None
        self.polish_payloads = []
        self.shutdown_calls = 0

    def initialize(self, cfg, tools):
        self.cfg = cfg
        self.tools = tools

    def checkpoint(self, payload):
        self.polish_payloads.append(payload)
        if self.fail_polish:
            return AgentResult(ok=False, error="polish failed")
        return AgentResult(ok=True)

    def consolidate(self, payload):
        self.payload = payload
        results = self.tools.apply_agent_ops(self.ops)
        return AgentResult(ok=True, op_results=results)

    def cancel(self):
        return None

    def is_healthy(self):
        return True

    def shutdown(self):
        self.shutdown_calls += 1


def install_cores(monkeypatch, core):
    def factory(kind, payload_dir=None):
        return core

    monkeypatch.setattr("meeting.reinsight.create_agent_core", factory)
    monkeypatch.setattr("meeting.agent.base.create_agent_core", factory)


def rich_decode(spool_dir, chunks, progress_cb=None):
    return [
        TranscriptSegment(
            segment_id="sg_new_1", meeting_id="m_retry", chunk_id=None,
            channel="mic", start_s=0.0, end_s=2.2,
            text="We should ship the budget review on Friday as planned.",
        ),
        TranscriptSegment(
            segment_id="sg_new_2", meeting_id="m_retry", chunk_id=None,
            channel="loopback", start_s=2.2, end_s=4.4,
            text="Agreed, Friday works for the whole team review.",
        ),
    ]


class TestRedeocdeGuard:
    def test_sparse_redecode_keeps_draft(self, repo, monkeypatch):
        make_meeting(
            repo,
            state_json=seeded_state("m_retry", DEFAULT_STEPS),
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, FakeAgentCore())

        def sparse(spool_dir, chunks, progress_cb=None):
            return [
                TranscriptSegment(
                    segment_id="sg_sparse", meeting_id="m_retry",
                    chunk_id=None, channel="mic", start_s=0.0, end_s=1.0,
                    text="Hi",
                )
            ]

        result = rerun_finalization(
            repo, "m_retry",
            from_step="redecode",
            provider="openrouter",
            model="m",
            transcribe_fn=sparse,
        )
        ids = {row["id"] for row in repo.get_segments("m_retry")}
        assert "sg_1" in ids
        assert "sg_sparse" not in ids
        redecode = next(
            step for step in result["finalization"]["steps"]
            if step["id"] == "redecode"
        )
        assert redecode["status"] == "failed"
        assert result["ok"] is False

    def test_successful_redecode_replaces_draft(self, repo, monkeypatch):
        make_meeting(
            repo,
            state_json=seeded_state("m_retry", DEFAULT_STEPS),
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, FakeAgentCore())

        result = rerun_finalization(
            repo, "m_retry",
            from_step="redecode",
            provider="openrouter",
            model="m",
            transcribe_fn=rich_decode,
        )
        ids = {row["id"] for row in repo.get_segments("m_retry")}
        assert "sg_new_1" in ids
        assert "sg_1" not in ids
        redecode = next(
            step for step in result["finalization"]["steps"]
            if step["id"] == "redecode"
        )
        assert redecode["status"] == "completed"


class TestStepSelection:
    def test_consolidation_retry_does_not_redecode(self, repo, monkeypatch):
        make_meeting(
            repo,
            state_json=seeded_state("m_retry", DEFAULT_STEPS),
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, FakeAgentCore(ops=[
            {
                "op": "add_item",
                "card": "key_points",
                "text": "Budget review ships Friday",
                "evidence": ["sg_1"],
            },
        ]))

        def boom(spool_dir, chunks, progress_cb=None):
            raise AssertionError("redecode should not run")

        result = rerun_finalization(
            repo, "m_retry",
            from_step="consolidation",
            provider="openrouter",
            model="m",
            transcribe_fn=boom,
        )
        ids = {row["id"] for row in repo.get_segments("m_retry")}
        assert "sg_1" in ids
        redecode = next(
            step for step in result["finalization"]["steps"]
            if step["id"] == "redecode"
        )
        consolidation = next(
            step for step in result["finalization"]["steps"]
            if step["id"] == "consolidation"
        )
        assert redecode["status"] == "failed"
        assert consolidation["status"] == "completed"
        # Overall stays failed until the leftover redecode is retried.
        assert result["ok"] is False

    def test_from_redecode_runs_dependents(self, repo, monkeypatch):
        core = FakeAgentCore()
        make_meeting(
            repo,
            state_json=seeded_state("m_retry", DEFAULT_STEPS),
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, core)

        result = rerun_finalization(
            repo, "m_retry",
            from_step="redecode",
            provider="openrouter",
            model="m",
            transcribe_fn=rich_decode,
        )
        assert core.polish_payloads
        assert core.payload is not None
        assert core.payload.is_consolidation is True
        statuses = {
            step["id"]: step["status"]
            for step in result["finalization"]["steps"]
        }
        assert statuses["redecode"] == "completed"
        assert statuses["polish"] == "completed"
        assert statuses["consolidation"] == "completed"
        assert statuses["finalize"] == "completed"

    def test_polish_failure_is_recorded(self, repo, monkeypatch):
        core = FakeAgentCore(fail_polish=True)
        steps = [
            {
                "id": "polish",
                "name": "Transcript Cleanup",
                "status": "failed",
            },
            {
                "id": "consolidation",
                "name": "Summary & Action Items",
                "status": "pending",
            },
            {
                "id": "finalize",
                "name": "State Finalization",
                "status": "pending",
            },
        ]
        make_meeting(repo, state_json=seeded_state("m_retry", steps))
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, core)

        result = rerun_finalization(
            repo, "m_retry",
            from_step="polish",
            provider="openrouter",
            model="m",
        )
        polish = next(
            step for step in result["finalization"]["steps"]
            if step["id"] == "polish"
        )
        assert polish["status"] == "failed"
        assert result["finalization"]["status"] == "failed"
        assert core.payload is not None


class TestProtection:
    def test_human_pinned_cards_survive(self, repo, monkeypatch):
        state = MeetingState(
            meeting_id="m_retry",
            seq=4,
            title="Budget sync",
            cloud_enabled=True,
            finalization=FinalizationState(
                status="failed",
                message="needs retry",
                steps=[
                    {
                        "id": "consolidation",
                        "name": "Summary & Action Items",
                        "status": "failed",
                    },
                    {
                        "id": "finalize",
                        "name": "State Finalization",
                        "status": "pending",
                    },
                ],
                total_steps=2,
            ),
        )
        state.cards["key_points"].append(CardItem(
            id="it_human",
            card="key_points",
            text="Human wrote this",
            status="edited",
            author_type="user",
        ))
        make_meeting(repo, state_json=json.dumps(state.to_dict()))
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, FakeAgentCore(ops=[
            {
                "op": "update_item",
                "id": "it_human",
                "base_revision": 1,
                "set": {"text": "Agent overwrite"},
            },
        ]))

        result = rerun_finalization(
            repo, "m_retry",
            from_step="consolidation",
            provider="openrouter",
            model="m",
        )
        items = result["state"]["cards"]["key_points"]
        human = next(item for item in items if item["id"] == "it_human")
        assert human["text"] == "Human wrote this"
        assert human["status"] == "edited"


class TestEndpointSnapshot:
    def test_stored_snapshot_is_passed_to_agent(self, repo, monkeypatch):
        endpoint = {
            "profile_id": "custom_abcd1234",
            "name": "LM Studio",
            "kind": "custom",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key_env": "",
        }
        core = FakeAgentCore()
        make_meeting(
            repo,
            state_json=seeded_state("m_retry", DEFAULT_STEPS),
            agent_endpoint_json=json.dumps(endpoint),
            agent_provider="custom_abcd1234",
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, core)

        rerun_finalization(
            repo, "m_retry",
            from_step="consolidation",
            provider="custom_abcd1234",
            model="local-qwen",
        )
        assert core.cfg is not None
        assert core.cfg.endpoint["base_url"] == endpoint["base_url"]
        assert core.cfg.endpoint["profile_id"] == "custom_abcd1234"

    def test_old_row_reconstructs_builtin_endpoint(self):
        from meeting.refinalize import _meeting_endpoint

        snapshot = _meeting_endpoint({"agent_provider": "openai"})
        assert snapshot["profile_id"] == "openai"
        assert snapshot["kind"] == "openai"


class TestCardDeferred:
    def test_rerun_preserves_card_deferred(self, repo, monkeypatch):
        make_meeting(
            repo,
            state_json=seeded_state(
                "m_retry", DEFAULT_STEPS, card_deferred=True
            ),
        )
        add_transcript(repo, "m_retry")
        install_cores(monkeypatch, FakeAgentCore())

        result = rerun_finalization(
            repo, "m_retry",
            from_step="redecode",
            provider="openrouter",
            model="m",
            transcribe_fn=rich_decode,
        )
        assert result["finalization"]["card_deferred"] is True
        assert result["state"]["finalization"]["card_deferred"] is True
        stored = json.loads(repo.get_meeting("m_retry")["state_json"])
        assert stored["finalization"]["card_deferred"] is True
