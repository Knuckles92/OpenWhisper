"""Behavioral regressions for live agent work, including silence and human input."""

import threading
import time
from concurrent.futures import Future

from meeting.agent.prompts import build_checkpoint_user_prompt, build_notes_user_prompt
from meeting.agent.scheduler import CheckpointScheduler
from meeting.interfaces import AgentResult, OpResult
from tests.test_meeting_notes_agent import FakeAgent, FakeEngine, _seg


def test_guidance_revisits_consumed_notes_without_new_speech():
    agent = FakeAgent()
    agent.supports_notes_pass = True
    engine = FakeEngine([_seg("sg_old", 1, "Anthropic makes Claude.")])
    scheduler = CheckpointScheduler(engine, agent)
    scheduler._fire()
    assert scheduler._notes_sent_starts == {"sg_old": 1}
    scheduler.notify_guidance()
    scheduler._fire()
    reviews = [
        p
        for p in agent.calls
        if p.is_notes and p.state_snapshot.get("notes_review_requested")
    ]
    assert len(reviews) == 1
    assert reviews[0].new_segments[0]["text"] == "Anthropic makes Claude."
    assert "No new speech is required" in build_notes_user_prompt(
        reviews[0].state_snapshot, reviews[0].new_segments
    )


def test_first_cleanup_runs_during_silence(monkeypatch):
    from meeting.agent import scheduler as module

    monkeypatch.setattr(module, "_TICK_S", 0.01)
    monkeypatch.setattr(module, "_POLISH_INITIAL_DELAY_S", 0.02)
    cleaned = threading.Event()
    agent = FakeAgent()
    original = agent.checkpoint

    def checkpoint(payload):
        result = original(payload)
        if payload.is_polish:
            cleaned.set()
        return result

    agent.checkpoint = checkpoint
    scheduler = CheckpointScheduler(
        FakeEngine([_seg("sg_1", 1)]), agent, min_interval_s=0.01
    )
    scheduler.start()
    try:
        scheduler.notify_segments(1)
        assert cleaned.wait(2), (
            "A short meeting must not need six checkpoints to get cleanup"
        )
        assert len([p for p in agent.calls if not p.is_polish]) == 1
        time.sleep(0.04)
        assert len([p for p in agent.calls if p.is_polish]) == 1
    finally:
        scheduler.stop()


def test_user_request_precedes_follow_on_background_passes():
    agent = FakeAgent()
    agent.supports_notes_pass = True
    scheduler = CheckpointScheduler(FakeEngine([_seg("sg_1", 1)]), agent)
    scheduler._successful_checkpoints = 5
    request = Future()
    original = agent.checkpoint

    def checkpoint(payload):
        result = original(payload)
        if not payload.is_notes:
            scheduler._note_requests.append(("Use bullets", request))
        return result

    agent.checkpoint = checkpoint
    scheduler._fire()
    assert len(agent.calls) == 1  # Neither notes nor polish jump ahead.
    scheduler._fire_note_request()
    assert request.result().ok
    assert agent.calls[1].state_snapshot["note_adjustment_request"] == "Use bullets"


def test_later_speech_can_correct_a_previously_consumed_name():
    engine = FakeEngine([_seg("sg_old", 1, "We are evaluating Entropic.")])
    agent = FakeAgent()
    scheduler = CheckpointScheduler(engine, agent)
    scheduler._fire()
    engine._segments.append(_seg("sg_new", 5, "Their Claude model is the candidate."))
    scheduler._fire()
    payload = agent.calls[-1]
    assert [s["id"] for s in payload.new_segments] == ["sg_new"]
    prompt = build_checkpoint_user_prompt(payload.state_snapshot, payload.new_segments)
    assert "[sg_old]" in prompt and "evaluating Entropic" in prompt
    assert "[sg_new]" in prompt and "Claude" in prompt


def test_polish_changes_trigger_dashboard_and_notes_review():
    agent = FakeAgent()

    def checkpoint(payload):
        return AgentResult(
            ok=True,
            op_results=[
                OpResult(
                    ok=True,
                    op={
                        "op": "revise_segment_text",
                        "segment_id": "sg_1",
                        "text": "Anthropic",
                    },
                )
            ],
        )

    agent.checkpoint = checkpoint
    scheduler = CheckpointScheduler(FakeEngine([_seg("sg_1", 1)]), agent)
    scheduler._successful_checkpoints = 6
    scheduler._maybe_fire_polish()
    assert scheduler._guidance_pending and scheduler._notes_guidance_pending


def test_failed_notes_retry_in_silence_and_do_not_spin(monkeypatch):
    from meeting.agent import scheduler as module

    monkeypatch.setattr(module, "_TICK_S", 0.01)
    monkeypatch.setattr(module, "_NOTES_MIN_INTERVAL_S", 0.08)
    finished = threading.Event()
    agent = FakeAgent()
    agent.supports_notes_pass = True
    attempts = []

    def checkpoint(payload):
        if payload.is_notes:
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                return AgentResult(ok=False, error="temporary")
            finished.set()
        return AgentResult(ok=True)

    agent.checkpoint = checkpoint
    scheduler = CheckpointScheduler(
        FakeEngine([_seg("sg_1", 1)]), agent, min_interval_s=0.01
    )
    scheduler.start()
    try:
        scheduler.notify_segments(1)
        assert finished.wait(2)
        assert len(attempts) == 2
        assert attempts[1] - attempts[0] >= 0.08
    finally:
        scheduler.stop()


def test_notes_backlog_keeps_earliest_unprocessed_segments(monkeypatch):
    from meeting.agent import scheduler as module

    monkeypatch.setattr(module, "_NOTES_MAX_SEGMENTS", 2)
    agent = FakeAgent()
    agent.supports_notes_pass = True
    scheduler = CheckpointScheduler(
        FakeEngine([_seg(f"sg_{i}", i * 200) for i in range(5)]), agent
    )
    scheduler._successful_checkpoints = 1
    scheduler._maybe_fire_notes()
    assert [s["id"] for s in agent.calls[0].new_segments] == ["sg_0", "sg_1"]
    scheduler._successful_checkpoints += 2
    scheduler._maybe_fire_notes()
    assert [s["id"] for s in agent.calls[1].new_segments] == ["sg_2", "sg_3"]


def test_sidecar_retains_transcript_revision_operations_and_shared_prompt(
    monkeypatch, tmp_path
):
    from meeting.agent.pi_sidecar import PiSidecarAgent
    from meeting.agent.prompts import build_system_prompt
    from meeting.interfaces import AgentConfig, CheckpointPayload

    agent = PiSidecarAgent(str(tmp_path))
    agent._cfg = AgentConfig(
        "synthetic", "openrouter", "model", None, build_system_prompt()
    )
    agent._initialized = True

    class Tools:
        def apply_agent_ops(self, ops):
            return [OpResult(ok=True, op=op) for op in ops]

    agent._tools = Tools()
    monkeypatch.setattr(agent, "is_healthy", lambda: True)
    monkeypatch.setattr(agent, "_write_msg", lambda *args, **kwargs: None)

    def rpc(method, params, **kwargs):
        assert "REVIEW EXISTING NOTES NOW" in params["user_prompt"]
        assert "REVIEW EXISTING NOTES NOW" in params["system_prompt"]
        assert "No new speech" in params["state"]["note_adjustment_request"]
        agent._handle_tool_request(
            {
                "id": 1,
                "method": "tool.patch_state",
                "params": {
                    "ops": [
                        {
                            "op": "add_item",
                            "card": "live_notes",
                            "text": "Anthropic makes Claude.",
                            "evidence": ["sg_1"],
                        },
                    ]
                },
            }
        )
        return {"applied": 1, "rejected": 0}

    monkeypatch.setattr(agent, "_rpc", rpc)
    payload = CheckpointPayload(
        "request", {"cards": {}, "notes_review_requested": True}, [], is_notes=True
    )
    result = agent.checkpoint(payload)
    assert result.ok and result.op_results[0].op["op"] == "add_item"
    assert "note_adjustment_request" not in payload.state_snapshot
    assert agent._checkpoint_op_results == {}


def test_sidecar_returns_actual_polish_revision(monkeypatch, tmp_path):
    from meeting.agent.pi_sidecar import PiSidecarAgent
    from meeting.agent.prompts import build_system_prompt
    from meeting.interfaces import AgentConfig, CheckpointPayload

    agent = PiSidecarAgent(str(tmp_path))
    agent._cfg = AgentConfig(
        "synthetic", "openrouter", "model", None, build_system_prompt()
    )
    agent._initialized = True

    class Tools:
        def apply_agent_ops(self, ops):
            return [OpResult(ok=True, op=op) for op in ops]

    agent._tools = Tools()
    monkeypatch.setattr(agent, "is_healthy", lambda: True)
    monkeypatch.setattr(agent, "_write_msg", lambda *args, **kwargs: None)

    def rpc(method, params, **kwargs):
        agent._handle_tool_request(
            {
                "id": 1,
                "method": "tool.patch_state",
                "params": {
                    "ops": [
                        {
                            "op": "revise_segment_text",
                            "segment_id": "sg_1",
                            "text": "Anthropic",
                            "evidence": ["sg_1"],
                        },
                    ]
                },
            }
        )
        return {"applied": 1, "rejected": 1}

    monkeypatch.setattr(agent, "_rpc", rpc)
    result = agent.checkpoint(CheckpointPayload("request", {}, [], is_polish=True))
    assert result.op_results[0].op["op"] == "revise_segment_text"
    assert sum(r.ok for r in result.op_results) == 1
    assert sum(not r.ok for r in result.op_results) == 1


def test_guidance_review_does_not_skip_unprocessed_note_backlog(monkeypatch):
    from meeting.agent import scheduler as module

    monkeypatch.setattr(module, "_NOTES_MAX_SEGMENTS", 2)
    agent = FakeAgent()
    agent.supports_notes_pass = True
    scheduler = CheckpointScheduler(
        FakeEngine([_seg(f"sg_{i}", i * 200) for i in range(5)]), agent
    )
    scheduler._notes_guidance_pending = True
    scheduler._maybe_fire_notes()
    assert [s["id"] for s in agent.calls[0].new_segments] == ["sg_3", "sg_4"]
    assert scheduler._notes_sent_starts == {}
    scheduler._successful_checkpoints = 2
    scheduler._maybe_fire_notes()
    assert [s["id"] for s in agent.calls[1].new_segments] == ["sg_0", "sg_1"]


def test_new_human_guidance_gets_an_attempt_despite_earlier_failure_backoff():
    scheduler = CheckpointScheduler(FakeEngine([]), FakeAgent())
    scheduler._retry_not_before = time.monotonic() + 300
    scheduler._notes_retry_not_before = time.monotonic() + 45
    scheduler.notify_guidance()
    assert scheduler._retry_not_before == 0
    assert scheduler._notes_retry_not_before == 0
