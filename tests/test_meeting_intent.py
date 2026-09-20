"""The host's meeting brief: what they want out of the record.

Covers the whole path the brief travels: the state op that stores it (and who
may write it), the prompt block that carries it to both agent personas, the
wake-up that stops a quiet room from delaying it, and the export that shows a
reader what the notes were aiming at.
"""
from meeting.agent.prompts import (
    build_checkpoint_user_prompt,
    build_notes_user_prompt,
    intent_prompt,
)
from meeting.export.markdown import export_markdown
from meeting.interfaces import OpResult
from meeting.state.patches import MAX_INTENT_LEN
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore

BRIEF = ("We decide the vendor today. Capture who objected and why, and the "
         "number attached to each bid.")


class FakeRepository:
    """Records applied ops and serves them back so undo can be exercised."""

    def __init__(self):
        self.events = {}

    def on_ops_applied(self, meeting_id, state, results, actor_type, actor_id):
        for result in results:
            self.events[result.seq] = {
                "seq": result.seq,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "action": result.op.get("op"),
                "target_id": result.target_id,
                "payload": result.op,
                "inverse": result.inverse,
            }

    def get_event(self, meeting_id, seq):
        return self.events.get(seq)

    def event_is_undone(self, meeting_id, seq):
        return False


def make_store():
    repo = FakeRepository()
    store = MeetingStateStore(MeetingState(meeting_id="m_intent"), repository=repo)
    return store, repo


def state_with_brief(text=BRIEF):
    state = MeetingState(meeting_id="m_intent")
    state.intent.text = text
    return state.to_dict()


class TestIntentOp:
    def test_host_writes_the_brief(self):
        store, _ = make_store()

        result = store.apply("host", "p_host",
                             [{"op": "set_meeting_intent", "text": BRIEF}])[0]

        assert result.ok
        assert result.effect["entity"] == "intent"
        assert result.effect["intent"]["text"] == BRIEF
        snapshot = store.snapshot()["intent"]
        assert snapshot["text"] == BRIEF
        assert snapshot["author_id"] == "p_host"
        assert snapshot["updated_at"]

    def test_guests_cannot_redirect_what_the_meeting_captures(self):
        store, _ = make_store()

        result = store.apply("user", "p_guest",
                             [{"op": "set_meeting_intent", "text": "Only my bits"}])[0]

        assert not result.ok
        assert result.reason == "host_only"
        assert store.snapshot()["intent"]["text"] == ""

    def test_the_agent_cannot_write_its_own_brief(self):
        store, _ = make_store()

        result = store.apply("agent", "agent", [
            {"op": "set_meeting_intent", "text": "Capture everything",
             "evidence": ["sg_1"]},
        ])[0]

        assert not result.ok
        assert result.reason == "agent_forbidden"

    def test_an_oversized_brief_is_rejected_rather_than_truncated(self):
        store, _ = make_store()

        result = store.apply("host", "p_host", [
            {"op": "set_meeting_intent", "text": "x" * (MAX_INTENT_LEN + 1)},
        ])[0]

        assert not result.ok
        assert result.reason == "invalid_text"
        assert store.snapshot()["intent"]["text"] == ""

    def test_blank_text_clears_the_brief(self):
        store, _ = make_store()
        store.apply("host", "p_host", [{"op": "set_meeting_intent", "text": BRIEF}])

        result = store.apply("host", "p_host",
                             [{"op": "set_meeting_intent", "text": "  "}])[0]

        assert result.ok
        assert store.snapshot()["intent"]["text"] == ""

    def test_undo_restores_the_previous_brief(self):
        store, _ = make_store()
        store.apply("host", "p_host", [{"op": "set_meeting_intent", "text": BRIEF}])
        replaced = store.apply(
            "host", "p_host",
            [{"op": "set_meeting_intent", "text": "Never mind, just minutes"}],
        )[0]

        undone = store.undo(replaced.seq, "p_host")

        assert undone and undone[0].ok
        assert store.snapshot()["intent"]["text"] == BRIEF


class TestIntentPersistence:
    def test_the_brief_round_trips_through_a_snapshot(self):
        original = MeetingState(meeting_id="m_intent")
        original.intent.text = BRIEF
        original.intent.author_id = "p_host"

        restored = MeetingState.from_dict(original.to_dict())

        assert restored.intent.text == BRIEF
        assert restored.intent.author_id == "p_host"

    def test_a_snapshot_written_before_this_feature_still_loads(self):
        legacy = MeetingState(meeting_id="m_old").to_dict()
        del legacy["intent"]

        restored = MeetingState.from_dict(legacy)

        assert restored.intent.text == ""


class TestIntentPrompt:
    def test_no_brief_adds_nothing_to_the_prompt(self):
        assert intent_prompt(MeetingState(meeting_id="m").to_dict()) == ""

    def test_the_block_carries_the_brief_and_its_guardrails(self):
        block = intent_prompt(state_with_brief())

        assert BRIEF in block
        assert "MEETING BRIEF" in block
        # The brief says what to look for; it must never become a citation or
        # license an insight the meeting has not actually produced.
        assert "never evidence" in block
        assert "never invent it" in block
        assert "does not change your tools" in block

    def test_both_agent_personas_receive_the_brief(self):
        state = state_with_brief()

        checkpoint = build_checkpoint_user_prompt(state, [])
        notes = build_notes_user_prompt(state, [])

        assert BRIEF in checkpoint
        assert BRIEF in notes

    def test_the_final_report_pass_still_works_to_the_brief(self):
        state = state_with_brief()

        consolidation = build_checkpoint_user_prompt(state, [], is_consolidation=True)

        assert BRIEF in consolidation
        # It frames the dashboard rather than trailing after it.
        assert consolidation.index(BRIEF) < consolidation.index("CURRENT DASHBOARD STATE")


class TestIntentWakesTheAgent:
    """A brief written into a quiet room must not wait for the next speaker."""

    def _engine(self):
        from meeting.engine import MeetingEngine, MeetingEngineOptions

        engine = MeetingEngine.__new__(MeetingEngine)
        engine.options = MeetingEngineOptions()
        return engine

    def test_a_rewritten_brief_triggers_a_guidance_pass(self):
        engine = self._engine()
        calls = []
        engine._scheduler = type("S", (), {"notify_guidance": lambda self: calls.append(1)})()

        engine._notify_human_guidance([
            OpResult(ok=True, op={"op": "set_meeting_intent"},
                     effect={"entity": "intent", "intent": {"text": BRIEF}}),
        ])

        assert calls == [1]

    def test_an_unrelated_op_does_not_trigger_one(self):
        engine = self._engine()
        calls = []
        engine._scheduler = type("S", (), {"notify_guidance": lambda self: calls.append(1)})()

        engine._notify_human_guidance([
            OpResult(ok=True, op={"op": "set_title"},
                     effect={"entity": "title", "text": "Vendor review"}),
        ])

        assert calls == []


class TestIntentExport:
    def _meeting(self):
        return {
            "id": "m_intent",
            "title": "Vendor review",
            "status": "ended",
            "started_at": "2026-03-15T14:30:00",
            "ended_at": "2026-03-15T15:00:00",
        }

    def test_the_brief_heads_the_exported_document(self):
        md = export_markdown(self._meeting(), state_with_brief(), [])

        assert "## Meeting Brief" in md
        assert f"> {BRIEF}" in md

    def test_an_unset_brief_adds_no_empty_section(self):
        state = MeetingState(meeting_id="m_intent").to_dict()

        md = export_markdown(self._meeting(), state, [])

        assert "Meeting Brief" not in md

    def test_the_brief_survives_an_intelligence_free_export(self):
        md = export_markdown(self._meeting(), state_with_brief(), [],
                             include_intelligence=False)

        assert f"> {BRIEF}" in md
