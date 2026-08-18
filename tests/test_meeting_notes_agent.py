"""Tests for the AI note taker: prompts, op gating, scheduler cadence, export."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.agent.openrouter_direct import DirectOpenRouterAgent
from meeting.agent.pi_sidecar import PiSidecarAgent
from meeting.agent.prompts import (
    build_checkpoint_user_prompt,
    build_note_taker_system_prompt,
    build_notes_user_prompt,
    build_system_prompt,
)
from meeting.agent.scheduler import CheckpointScheduler
from meeting.interfaces import AgentResult, OpResult
from meeting.state.patches import OpContext, apply_ops, filter_notes_ops, live_note_ids
from meeting.state.schema import CARD_KEYS, MeetingState


def _seg(seg_id, start_s, text="hello there", channel="mic"):
    return {
        "id": seg_id,
        "start_s": start_s,
        "end_s": start_s + 2.0,
        "text": text,
        "channel": channel,
    }


class TestNotesState:
    def test_live_notes_is_a_card_and_round_trips(self):
        assert "live_notes" in CARD_KEYS
        from meeting.state.schema import CardItem

        state = MeetingState(meeting_id="m_notes")
        state.cards["live_notes"].append(CardItem(
            id="it_n1", card="live_notes",
            text="Maria walked the funnel.",
            data={"heading": "Funnel review", "start_s": 41.0},
        ))
        restored = MeetingState.from_dict(state.to_dict())
        assert restored.cards["live_notes"][0].data["heading"] == "Funnel review"

    def test_legacy_snapshots_without_live_notes_load(self):
        state = MeetingState(meeting_id="m_old")
        doc = state.to_dict()
        doc["cards"].pop("live_notes")
        restored = MeetingState.from_dict(doc)
        assert restored.cards["live_notes"] == []


class TestNoteTakerPrompts:
    def test_note_taker_system_prompt_persona_and_constraints(self):
        prompt = build_note_taker_system_prompt()
        assert "note taker" in prompt.lower()
        assert "live_notes" in prompt
        for op in ("add_item", "update_item", "remove_item"):
            assert op in prompt
        assert "data.heading" in prompt
        assert "data.start_s" in prompt
        assert "human_edited" in prompt
        assert "search_context_files" in prompt
        assert "untrusted" in prompt

    def test_copilot_system_prompt_delegates_live_notes(self):
        prompt = build_system_prompt()
        assert "live_notes" in prompt
        assert "note-taker pass" in prompt
        assert "search_past_meetings" in prompt
        assert "search_context_files" in prompt
        assert "CONTEXT ONLY" in prompt
        assert "untrusted" in prompt

    def test_consolidation_instructions_finalize_notes(self):
        prompt = build_checkpoint_user_prompt(
            {"participants": {}, "cards": {}, "questions": []}, [],
            is_consolidation=True,
        )
        assert "live_notes" in prompt
        assert "professional minutes" in prompt
        # Reconciliation against the full recording, not just tidying: fix
        # what later context superseded, and rebuild an empty page.
        assert "COMPLETE final transcript" in prompt
        assert "superseded" in prompt
        assert "notes page from the complete transcript" in prompt
        # Explicit synthesis instruction to take meeting notes into account:
        assert "actively taking into account the\nmeeting notes" in prompt or "actively taking into account the meeting notes" in prompt
        assert "Synthesize the meeting notes and complete transcript" in prompt

    def test_compact_state_renders_note_heading_and_extended_text(self):
        from meeting.agent.prompts import render_state_compact

        long_note_text = "This is a detailed and comprehensive note block explaining architectural decisions. " * 4
        state = {
            "title": "Architecture Sync",
            "topic": {"current": "Design Review"},
            "rolling_summary": "Discussed system components.",
            "cards": {
                "live_notes": [{
                    "id": "it_n1", "revision": 1, "status": "proposed",
                    "text": long_note_text,
                    "data": {"heading": "Architecture Review", "start_s": 15.0},
                }],
                "key_points": [{
                    "id": "it_k1", "revision": 1, "status": "proposed",
                    "text": "Short key point.",
                }],
            },
            "participants": {},
        }
        rendered = render_state_compact(state)
        assert "heading=Architecture Review" in rendered
        assert "start_s=15.0" in rendered
        assert "detailed and comprehensive note block" in rendered
        # Ensure it wasn't clipped at 240 chars:
        assert len(long_note_text) > 300
        assert long_note_text[:280] in rendered

    def test_notes_user_prompt_renders_blocks_and_segments(self):
        state = {
            "topic": {"current": "Funnel review"},
            "rolling_summary": "Team reviewed onboarding.",
            "cards": {"live_notes": [{
                "id": "it_note1", "revision": 2, "status": "proposed",
                "text": "Maria walked through the funnel numbers.",
                "data": {"heading": "Funnel review", "start_s": 41.0},
            }]},
            "participants": {},
        }
        prompt = build_notes_user_prompt(
            state, [_seg("sg_new", 95.0, "We agreed to try OAuth.")],
        )
        assert "## CURRENT NOTES PAGE" in prompt
        assert "it_note1" in prompt and "rev=2" in prompt
        assert "heading=Funnel review" in prompt
        assert "start_s=41.0" in prompt
        assert "[sg_new]" in prompt and "[t=95.0s]" in prompt
        assert "NOTE-TAKER PASS" in prompt

    def test_notes_user_prompt_empty_page_hint(self):
        prompt = build_notes_user_prompt({"cards": {}, "participants": {}}, [])
        assert "page is empty" in prompt
        assert "(no new segments)" in prompt


class _Tools:
    def __init__(self) -> None:
        self.ops = []

    def apply_agent_ops(self, ops):
        self.ops.extend(ops)
        return [OpResult(ok=True, op=op) for op in ops]

    def ask_question(self, text, evidence):
        return OpResult(ok=True, op={"op": "ask_question"})

    def resolve_question(self, question_id, answer_text, confidence, evidence):
        return OpResult(ok=True, op={"op": "resolve_question"})


class TestDirectAgentNotesMode:
    def test_direct_agent_declares_notes_support(self):
        assert DirectOpenRouterAgent.supports_notes_pass is True

    def test_notes_mode_filters_to_live_notes_ops(self):
        tools = _Tools()
        agent = DirectOpenRouterAgent()
        agent._tools = tools
        agent._notes_mode = True
        agent._notes_item_ids = frozenset({"it_note1"})

        results = agent._dispatch_tool_call("patch_state", {"ops": [
            {
                "op": "add_item", "card": "live_notes",
                "text": "new block", "evidence": ["sg_1"],
            },
            {
                "op": "add_item", "card": "key_points",
                "text": "must not apply", "evidence": ["sg_1"],
            },
            {
                "op": "update_item", "id": "it_note1", "base_revision": 1,
                "set": {"text": "extend"}, "evidence": ["sg_1"],
            },
            {
                "op": "update_item", "id": "it_keypoint", "base_revision": 1,
                "set": {"text": "must not apply"}, "evidence": ["sg_1"],
            },
            {
                "op": "set_topic", "text": "must not apply", "evidence": ["sg_1"],
            },
        ]})
        question = agent._dispatch_tool_call("ask_question", {
            "text": "must not apply", "evidence": ["sg_1"],
        })

        assert [op["op"] for op in tools.ops] == ["add_item", "update_item"]
        assert tools.ops[0]["card"] == "live_notes"
        assert len(results) == 2
        assert question == []


class TestNotesPatchOps:
    def _agent_ctx(self):
        return OpContext(
            "agent", "note-taker",
            segment_exists=lambda sid: sid.startswith("sg_"),
        )

    def test_agent_writes_notes_with_protection_flow(self):
        state = MeetingState(meeting_id="m_notes")
        ctx = self._agent_ctx()

        add = apply_ops(state, [{
            "op": "add_item", "card": "live_notes",
            "text": "Maria walked the funnel: 40 percent signup to activation, down 5 points.",
            "data": {"heading": "Funnel review", "start_s": 41.0},
            "evidence": ["sg_1"],
        }], ctx)
        assert add[0].ok
        item = state.cards["live_notes"][0]
        assert item.status == "proposed"
        assert item.data["start_s"] == 41.0

        dup = apply_ops(state, [{
            "op": "add_item", "card": "live_notes",
            "text": "Maria walked the funnel: 40 percent signup to activation, down 5 points!",
            "evidence": ["sg_1"],
        }], ctx)
        assert dup[0].reason == "duplicate_item"

        update = apply_ops(state, [{
            "op": "update_item", "id": item.id, "base_revision": item.revision,
            "set": {"text": "Maria walked the funnel; activation down 5 points to 40 percent."},
            "evidence": ["sg_2"],
        }], ctx)
        assert update[0].ok

        # A human edit makes the block authoritative; the agent is locked out.
        apply_ops(state, [{
            "op": "update_item", "id": item.id, "set": {"text": "human wording"},
        }], OpContext("user", "p_me"))
        locked = apply_ops(state, [{
            "op": "update_item", "id": item.id,
            "base_revision": item.revision + 1,
            "set": {"text": "agent rewrite attempt"}, "evidence": ["sg_3"],
        }], ctx)
        assert locked[0].reason == "human_edited"

    def test_human_can_add_a_note_directly(self):
        state = MeetingState(meeting_id="m_notes")
        results = apply_ops(state, [{
            "op": "add_item", "card": "live_notes",
            "text": "Reminder: send the deck after the call.",
            "data": {"heading": "Follow-up"},
        }], OpContext("user", "p_guest"))
        assert results[0].ok
        assert state.cards["live_notes"][0].status == "edited"


# ---------------------------------------------------------------------------
# Scheduler cadence
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self._snapshot = {
            "meeting_id": "m_test",
            "seq": 1,
            "cards": {},
            "topic": {"current": "seeded topic", "history": []},
            "rolling_summary": "seeded summary",
        }

    def snapshot(self):
        return dict(self._snapshot)


class FakeEngine:
    def __init__(self, segments=None):
        self.store = FakeStore()
        self._segments = list(segments or [])

    def get_transcript(self, after_start_s=-1.0, limit=None):
        items = [
            s for s in self._segments
            if float(s.get("start_s") or 0.0) > float(after_start_s)
        ]
        if limit is not None:
            items = items[:limit]
        return items


class FakeAgent:
    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_left = fail_times

    def checkpoint(self, payload):
        self.calls.append(payload)
        if self._fail_left > 0:
            self._fail_left -= 1
            return AgentResult(ok=False, error="forced")
        return AgentResult(
            ok=True,
            op_results=[OpResult(ok=True, op={"op": "add_item"}, seq=1)],
        )

    def is_healthy(self):
        return True


class TestSchedulerNotesPass:
    def test_notes_pass_fires_only_for_supporting_cores(self):
        for supports in (True, False):
            agent = FakeAgent()
            if supports:
                agent.supports_notes_pass = True
            sched = CheckpointScheduler(FakeEngine([_seg("sg_1", 10.0)]), agent)
            sched._successful_checkpoints = 6
            sched._maybe_fire_notes()
            assert len(agent.calls) == (1 if supports else 0)

    def test_notes_payload_carries_flag_and_consumes_segments(self):
        agent = FakeAgent()
        agent.supports_notes_pass = True
        sched = CheckpointScheduler(
            FakeEngine([_seg("sg_1", 10.0), _seg("sg_2", 20.0)]), agent,
        )
        sched._successful_checkpoints = 2
        sched._maybe_fire_notes()

        assert len(agent.calls) == 1
        payload = agent.calls[0]
        assert payload.is_notes
        assert not payload.is_polish
        assert not payload.is_consolidation
        assert [s["id"] for s in payload.new_segments] == ["sg_1", "sg_2"]
        assert sched._notes_max_sent_start_s == 20.0

        # Consumed segments do not refire even when the cadence is due.
        sched._successful_checkpoints = 4
        sched._maybe_fire_notes()
        assert len(agent.calls) == 1

    def test_notes_pass_waits_for_checkpoint_progress(self):
        agent = FakeAgent()
        agent.supports_notes_pass = True
        sched = CheckpointScheduler(FakeEngine([_seg("sg_1", 10.0)]), agent)
        sched._successful_checkpoints = 1  # below the every-N threshold
        sched._maybe_fire_notes()
        assert agent.calls == []

    def test_failed_notes_pass_leaves_segments_for_retry(self):
        agent = FakeAgent(fail_times=1)
        agent.supports_notes_pass = True
        sched = CheckpointScheduler(FakeEngine([_seg("sg_1", 10.0)]), agent)
        sched._successful_checkpoints = 2

        sched._maybe_fire_notes()  # fails
        assert len(agent.calls) == 1
        assert sched._notes_max_sent_start_s < 0.0  # nothing marked consumed
        # Cadence bookkeeping advanced: no immediate hot-loop retry.
        sched._maybe_fire_notes()
        assert len(agent.calls) == 1

        # After another successful checkpoint the window is retried and wins.
        sched._successful_checkpoints = 4
        sched._maybe_fire_notes()
        assert len(agent.calls) == 2
        assert sched._notes_max_sent_start_s == 10.0

    def test_notes_pass_skipped_without_new_segments(self):
        agent = FakeAgent()
        agent.supports_notes_pass = True
        sched = CheckpointScheduler(FakeEngine([]), agent)
        sched._successful_checkpoints = 5
        sched._maybe_fire_notes()
        assert agent.calls == []


class TestNotesExport:
    def test_markdown_export_renders_meeting_notes_section(self):
        from meeting.export.markdown import export_markdown

        state = {
            "participants": {},
            "topic": {"current": "Funnel review", "history": []},
            "rolling_summary": "Team reviewed onboarding.",
            "cards": {"live_notes": [
                {
                    "status": "proposed",
                    "text": "Maria walked the funnel.",
                    "data": {"heading": "Funnel review", "start_s": 41.0},
                    "evidence": [],
                },
                {
                    "status": "proposed",
                    "text": "Team agreed to try OAuth.",
                    "data": {"heading": "Login decision"},
                    "evidence": [],
                },
            ]},
            "questions": [],
        }
        md = export_markdown(
            {"id": "m1", "title": "Weekly", "started_at": None, "status": "ended"},
            state,
            [],
        )
        assert "## Meeting Notes" in md
        assert "**00:41 Funnel review** \u2014 Maria walked the funnel." in md
        assert "**Login decision** \u2014 Team agreed to try OAuth." in md

    def test_markdown_export_skips_empty_notes(self):
        from meeting.export.markdown import export_markdown

        state = {
            "participants": {},
            "topic": {"current": "", "history": []},
            "rolling_summary": "",
            "cards": {},
            "questions": [],
        }
        md = export_markdown(
            {"id": "m1", "title": "Weekly", "started_at": None, "status": "ended"},
            state,
            [],
        )
        assert "Meeting Notes" not in md


class TestSharedNotesFilter:
    """The one filter both agent cores rely on (patches.py)."""

    def test_filter_keeps_only_live_notes_ops(self):
        ops = [
            {"op": "add_item", "card": "live_notes", "text": "keep"},
            {"op": "add_item", "card": "key_points", "text": "drop"},
            {"op": "update_item", "id": "it_note1", "set": {"text": "keep"}},
            {"op": "update_item", "id": "it_key1", "set": {"text": "drop"}},
            {"op": "remove_item", "id": "it_note2"},
            {"op": "set_topic", "text": "drop"},
            {"op": "revise_segment_text", "segment_id": "sg_1", "text": "drop"},
        ]
        kept = filter_notes_ops(ops, frozenset({"it_note1", "it_note2"}))
        assert [op["op"] for op in kept] == [
            "add_item", "update_item", "remove_item",
        ]
        assert kept[0]["card"] == "live_notes"

    def test_filter_with_no_known_ids_allows_only_adds(self):
        ops = [
            {"op": "add_item", "card": "live_notes", "text": "keep"},
            {"op": "update_item", "id": "it_note1", "set": {"text": "drop"}},
        ]
        kept = filter_notes_ops(ops, None)
        assert [op["op"] for op in kept] == ["add_item"]

    def test_live_note_ids_reads_the_snapshot(self):
        state = {"cards": {"live_notes": [
            {"id": "it_a", "status": "proposed"},
            {"id": "it_b", "status": "removed"},
            {"id": "it_c", "status": "edited"},
        ]}}
        assert live_note_ids(state) == frozenset({"it_a", "it_b", "it_c"})
        assert live_note_ids({}) == frozenset()
        assert live_note_ids({"cards": {}}) == frozenset()

    def test_both_agent_cores_declare_notes_support(self):
        assert DirectOpenRouterAgent.supports_notes_pass is True
        assert PiSidecarAgent.supports_notes_pass is True


class TestEngineNotesStrip:
    """Proposed notes are stripped only when consolidation rebuilds them."""

    def _engine(self):
        from meeting.engine import MeetingEngine, MeetingEngineOptions

        engine = MeetingEngine(MeetingEngineOptions(), repository=object())
        state = MeetingState(meeting_id="m_strip")

        def _add(card, item_id, status="proposed", pinned=False):
            from meeting.state.schema import CardItem

            state.cards[card].append(CardItem(
                id=item_id, card=card, text=f"text {item_id}",
                status=status, pinned=pinned,
            ))

        _add("live_notes", "it_note_prop")
        _add("live_notes", "it_note_edit", status="edited")
        _add("live_notes", "it_note_pin", pinned=True)
        _add("key_points", "it_key_prop")
        _add("key_points", "it_key_edit", status="confirmed")

        from meeting.state.store import MeetingStateStore

        engine.store = MeetingStateStore(state)
        return engine, state

    def _statuses(self, state, card):
        # The store applies copy-on-write, so read post-strip state through it.
        live = [
            item for item in state["cards"].get(card, [])
        ]
        return {item["id"]: item["status"] for item in live}

    def test_notes_only_strip_removes_only_unprotected_notes(self):
        engine, state = self._engine()
        engine._strip_proposed_cards(cards=("live_notes",))
        snap = engine.store.snapshot()
        assert self._statuses(snap, "live_notes") == {
            "it_note_prop": "removed",
            "it_note_edit": "edited",
            "it_note_pin": "proposed",
        }
        # Other cards untouched by a notes-only strip.
        assert self._statuses(snap, "key_points") == {
            "it_key_prop": "proposed", "it_key_edit": "confirmed",
        }

    def test_redecode_strip_keeps_live_notes(self):
        from meeting.state.schema import CARD_KEYS

        engine, state = self._engine()
        engine._strip_proposed_cards(cards=tuple(
            key for key in CARD_KEYS if key not in ("user_notes", "live_notes")
        ))
        # The notes page survives the re-decode strip entirely...
        snap = engine.store.snapshot()
        assert self._statuses(snap, "live_notes") == {
            "it_note_prop": "proposed",
            "it_note_edit": "edited",
            "it_note_pin": "proposed",
        }
        # ...while proposed items on other cards are removed.
        assert self._statuses(snap, "key_points") == {
            "it_key_prop": "removed", "it_key_edit": "confirmed",
        }

    def test_default_strip_covers_all_cards_but_user_notes(self):
        engine, state = self._engine()
        engine._strip_proposed_cards()
        snap = engine.store.snapshot()
        assert self._statuses(snap, "live_notes") == {
            "it_note_prop": "removed",
            "it_note_edit": "edited",
            "it_note_pin": "proposed",
        }
        assert self._statuses(snap, "key_points") == {
            "it_key_prop": "removed", "it_key_edit": "confirmed",
        }

    def test_redecode_strip_keeps_evidenced_proposed_items(self):
        from meeting.state.schema import CARD_KEYS, CardItem, MeetingState
        from meeting.state.store import MeetingStateStore
        from meeting.engine import MeetingEngine, MeetingEngineOptions

        state = MeetingState(meeting_id="m_redecode")
        state.cards["key_points"].extend([
            CardItem(
                id="it_key_alive", card="key_points", text="anchored",
                evidence=["sg_new1"],
            ),
            CardItem(
                id="it_key_ghost", card="key_points", text="ghost",
                evidence=[],
            ),
            CardItem(
                id="it_key_edit", card="key_points", text="edited",
                status="edited", evidence=[],
            ),
        ])
        state.cards["live_notes"].append(CardItem(
            id="it_note_ghost", card="live_notes", text="note", evidence=[],
        ))
        engine = MeetingEngine(
            MeetingEngineOptions(), repository=object(),
        )
        engine.store = MeetingStateStore(state)
        engine._strip_proposed_cards(
            cards=tuple(
                key for key in CARD_KEYS
                if key not in ("user_notes", "live_notes")
            ),
            keep_evidenced=True,
        )
        assert self._statuses(engine.store.snapshot(), "key_points") == {
            "it_key_alive": "proposed",
            "it_key_ghost": "removed",
            "it_key_edit": "edited",
        }
        assert self._statuses(engine.store.snapshot(), "live_notes") == {
            "it_note_ghost": "proposed",
        }
