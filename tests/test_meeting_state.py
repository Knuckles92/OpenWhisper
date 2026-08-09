"""
Tests for the Meeting Mode state layer: patch ops, protection rules, and the
single-writer store (seq numbering, audit events, undo).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore


class FakeRepository:
    """Captures write-through calls; serves events back for undo."""

    def __init__(self):
        self.events = {}
        self.segments = {"sg_known": {"id": "sg_known",
                                      "speaker_participant_id": None,
                                      "speaker_source": "channel",
                                      "speaker_pinned": False}}
        self.ops_applied_calls = []

    def on_ops_applied(self, meeting_id, state, results, actor_type, actor_id):
        self.ops_applied_calls.append((actor_type, actor_id, list(results)))
        for r in results:
            self.events[r.seq] = {
                "seq": r.seq,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "action": r.op.get("op"),
                "target_id": r.target_id,
                "payload": r.op,
                "inverse": r.inverse,
            }

    def get_event(self, meeting_id, seq):
        return self.events.get(seq)

    def segment_exists(self, segment_id):
        return segment_id in self.segments

    def get_segment(self, segment_id):
        return self.segments.get(segment_id)

    def update_segment_speaker(self, segment_id, participant_id, source, pinned):
        seg = self.segments[segment_id]
        seg["speaker_participant_id"] = participant_id
        seg["speaker_source"] = source
        seg["speaker_pinned"] = pinned


def make_store():
    repo = FakeRepository()

    def segment_handler(result):
        effect = result.effect
        prior = repo.get_segment(effect["segment_id"])
        if prior is None:
            raise ValueError("unknown segment")
        # Capture before mutating: the fake's get_segment aliases live state
        # (the real repository returns fresh dicts).
        prior_pid = prior["speaker_participant_id"]
        repo.update_segment_speaker(
            effect["segment_id"], effect["participant_id"],
            effect["source"], effect["pinned"])
        return {"op": "reassign_segment_speaker",
                "segment_id": effect["segment_id"],
                "participant_id": prior_pid}

    state = MeetingState(meeting_id="m_test")
    store = MeetingStateStore(
        state, repository=repo,
        segment_handler=segment_handler,
        segment_exists=repo.segment_exists,
    )
    return store, repo


class TestItemOps:
    def test_agent_add_item_is_proposed(self):
        store, _ = make_store()
        results = store.apply("agent", "agent", [
            {"op": "add_item", "card": "key_points", "text": "Budget approved",
             "evidence": ["sg_known"]},
        ])
        assert results[0].ok
        item = results[0].effect["item"]
        assert item["status"] == "proposed"
        assert item["author_type"] == "agent"
        assert results[0].seq == 1

    def test_human_add_item_is_edited(self):
        store, _ = make_store()
        results = store.apply("user", "p_guest1", [
            {"op": "add_item", "card": "user_notes", "text": "My note"},
        ])
        assert results[0].ok
        assert results[0].effect["item"]["status"] == "edited"
        assert results[0].effect["item"]["author_id"] == "p_guest1"

    def test_agent_update_requires_matching_revision(self):
        store, _ = make_store()
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "decisions", "text": "Ship v1"},
        ])
        item_id = added.target_id

        wrong = store.apply("agent", "agent", [
            {"op": "update_item", "id": item_id, "base_revision": 99,
             "set": {"text": "Ship v2"}},
        ])[0]
        assert not wrong.ok
        assert wrong.reason == "revision_mismatch"
        assert wrong.current_revision == 1

        right = store.apply("agent", "agent", [
            {"op": "update_item", "id": item_id, "base_revision": 1,
             "set": {"text": "Ship v2"}},
        ])[0]
        assert right.ok
        assert right.effect["item"]["revision"] == 2

    def test_human_edit_protects_item_from_agent(self):
        store, _ = make_store()
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "action_items", "text": "Send report"},
        ])
        item_id = added.target_id

        edited = store.apply("user", "p_1", [
            {"op": "update_item", "id": item_id,
             "set": {"text": "Send report by Friday"}},
        ])[0]
        assert edited.ok
        assert edited.effect["item"]["status"] == "edited"

        agent_try = store.apply("agent", "agent", [
            {"op": "update_item", "id": item_id, "base_revision": 2,
             "set": {"text": "changed"}},
        ])[0]
        assert not agent_try.ok
        assert agent_try.reason == "human_edited"

        agent_remove = store.apply("agent", "agent", [
            {"op": "remove_item", "id": item_id, "base_revision": 2},
        ])[0]
        assert not agent_remove.ok
        assert agent_remove.reason == "human_edited"

    def test_pin_protects_item(self):
        store, _ = make_store()
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "risks", "text": "Timeline slip"},
        ])
        store.apply("user", "p_1", [{"op": "pin_item", "id": added.target_id}])
        blocked = store.apply("agent", "agent", [
            {"op": "update_item", "id": added.target_id, "base_revision": 1,
             "set": {"text": "x"}},
        ])[0]
        assert not blocked.ok
        assert blocked.reason == "human_edited"

    def test_remove_is_soft_delete(self):
        store, _ = make_store()
        [added] = store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "temp"},
        ])
        removed = store.apply("user", "p_1", [
            {"op": "remove_item", "id": added.target_id},
        ])[0]
        assert removed.ok
        assert removed.effect["item"]["status"] == "removed"
        # Still findable for undo purposes
        snapshot = store.snapshot()
        texts = [i["text"] for i in snapshot["cards"]["user_notes"]]
        assert "temp" in texts

    def test_unknown_card_and_evidence_rejected(self):
        store, _ = make_store()
        bad_card = store.apply("agent", "agent", [
            {"op": "add_item", "card": "nope", "text": "x"},
        ])[0]
        assert bad_card.reason == "unknown_card"

        bad_evidence = store.apply("agent", "agent", [
            {"op": "add_item", "card": "key_points", "text": "x",
             "evidence": ["sg_missing"]},
        ])[0]
        assert bad_evidence.reason == "unknown_evidence"


class TestAgentBoundaries:
    def test_agent_cannot_use_human_ops(self):
        store, _ = make_store()
        for op in (
            {"op": "pin_item", "id": "it_x"},
            {"op": "rename_participant", "participant_id": "p_x",
             "display_name": "Sam"},
            {"op": "answer_question", "question_id": "q_x",
             "answer_text": "yes"},
            {"op": "set_cloud_enabled", "enabled": False},
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": None},
        ):
            result = store.apply("agent", "agent", [op])[0]
            assert not result.ok
            assert result.reason == "agent_forbidden"

    def test_unknown_op_rejected(self):
        store, _ = make_store()
        result = store.apply("agent", "agent", [{"op": "replace_everything"}])[0]
        assert result.reason == "unknown_op"

    def test_rejected_ops_do_not_bump_seq(self):
        store, _ = make_store()
        store.apply("agent", "agent", [{"op": "unknown_thing"}])
        assert store.seq == 0


class TestAgentAuthorityLimits:
    """Rules the agent's tool schema cannot express, enforced at the seam."""

    def test_agent_cannot_add_to_user_notes(self):
        store, _ = make_store()
        result = store.apply("agent", "agent", [
            {"op": "add_item", "card": "user_notes", "text": "Agent note"},
        ])[0]
        assert not result.ok
        assert result.reason == "human_only_card"
        assert store.snapshot()["cards"]["user_notes"] == []
        # Humans still own the card.
        human = store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "My note"},
        ])[0]
        assert human.ok

    def test_agent_participants_stay_provisional(self):
        store, _ = make_store()
        created = store.apply("agent", "agent", [
            {"op": "upsert_participant", "display_name": "Sam",
             "is_provisional": False},
        ])[0]
        assert created.ok
        assert created.effect["participant"]["is_provisional"] is True

        renamed = store.apply("agent", "agent", [
            {"op": "upsert_participant", "id": created.target_id,
             "display_name": "Samantha", "is_provisional": False},
        ])[0]
        assert renamed.ok
        assert renamed.effect["participant"]["is_provisional"] is True

    def test_agent_may_only_mint_others_clusters(self):
        store, _ = make_store()
        for kind in ("me", "guest", "nonsense"):
            result = store.apply("agent", "agent", [
                {"op": "upsert_participant", "display_name": "Impostor",
                 "kind": kind},
            ])[0]
            assert not result.ok, kind
            assert result.reason == "invalid_kind"
        assert store.snapshot()["participants"] == {}

        allowed = store.apply("agent", "agent", [
            {"op": "upsert_participant", "display_name": "Speaker 2",
             "kind": "others_cluster"},
        ])[0]
        assert allowed.ok
        assert allowed.effect["participant"]["kind"] == "others_cluster"

    def test_humans_create_non_provisional_participants(self):
        store, _ = make_store()
        result = store.apply("host", "p_me", [
            {"op": "upsert_participant", "display_name": "Sarah"},
        ])[0]
        assert result.ok
        assert result.effect["participant"]["kind"] == "others_cluster"
        assert result.effect["participant"]["is_provisional"] is False

    def test_only_the_app_may_mint_me_and_guest_identities(self):
        """A dashboard client minting ``me`` could impersonate the host.

        The WS layer resolves the host by scanning for the first ``kind ==
        "me"`` participant, so an extra one is a hazard no matter which human
        created it. The engine creates both kinds as ``system``.
        """
        store, _ = make_store()
        for actor in ("host", "user"):
            for kind in ("me", "guest"):
                blocked = store.apply(actor, "p_1", [
                    {"op": "upsert_participant", "display_name": "Imposter",
                     "kind": kind},
                ])[0]
                assert not blocked.ok
                assert blocked.reason == "invalid_kind"

        allowed = store.apply("system", None, [
            {"op": "upsert_participant", "display_name": "Me", "kind": "me"},
        ])[0]
        assert allowed.ok
        assert allowed.effect["participant"]["kind"] == "me"

    def test_blank_rolling_summary_cannot_wipe_the_summary(self):
        store, _ = make_store()
        assert store.apply("agent", "agent", [
            {"op": "set_rolling_summary", "text": "We agreed to ship on Friday."},
        ])[0].ok

        # The agent is rejected on content; a guest never reaches the handler
        # at all, since the summary is host-only meeting metadata.
        for actor, text, reason in (
            ("agent", "", "invalid_text"),
            ("agent", "   \n ", "invalid_text"),
            ("user", "", "host_only"),
            ("user", "a guest rewrite", "host_only"),
        ):
            wiped = store.apply(actor, "p_1", [
                {"op": "set_rolling_summary", "text": text},
            ])[0]
            assert not wiped.ok
            assert wiped.reason == reason
        assert store.snapshot()["rolling_summary"] == (
            "We agreed to ship on Friday."
        )


class TestEvidenceIntegrity:
    """Evidence anchors are additive; undo must restore the exact prior set."""

    @staticmethod
    def _with_segments(repo, *segment_ids):
        for sg_id in segment_ids:
            repo.segments[sg_id] = {
                "id": sg_id, "speaker_participant_id": None,
                "speaker_source": "channel", "speaker_pinned": False,
            }

    def test_update_unions_evidence_instead_of_replacing_it(self):
        store, repo = make_store()
        self._with_segments(repo, "sg_a", "sg_b", "sg_c")
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "key_points", "text": "Original",
             "evidence": ["sg_known", "sg_a"]},
        ])
        # A consolidation-style reword citing only its newest anchors.
        [updated] = store.apply("agent", "agent", [
            {"op": "update_item", "id": added.target_id, "base_revision": 1,
             "set": {"text": "Reworded and merged"},
             "evidence": ["sg_b", "sg_a", "sg_c"]},
        ])
        assert updated.ok
        assert updated.effect["item"]["evidence"] == [
            "sg_known", "sg_a", "sg_b", "sg_c",
        ]

    def test_undo_restores_the_previous_evidence(self):
        store, repo = make_store()
        self._with_segments(repo, "sg_a", "sg_b")
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "decisions", "text": "Original",
             "evidence": ["sg_known", "sg_a"]},
        ])
        [updated] = store.apply("agent", "agent", [
            {"op": "update_item", "id": added.target_id, "base_revision": 1,
             "set": {"text": "Reworded"}, "evidence": ["sg_b"]},
        ])
        undo_results = store.undo(updated.seq, "p_host")
        assert undo_results and undo_results[0].ok
        item = undo_results[0].effect["item"]
        assert item["text"] == "Original"
        assert item["evidence"] == ["sg_known", "sg_a"]


class TestParticipants:
    def test_agent_creates_provisional_participant(self):
        store, _ = make_store()
        result = store.apply("agent", "agent", [
            {"op": "upsert_participant", "display_name": "Sam"},
        ])[0]
        assert result.ok
        p = result.effect["participant"]
        assert p["name_source"] == "agent_inferred"
        assert p["is_provisional"] is True

    def test_human_rename_blocks_agent_suggestions(self):
        store, _ = make_store()
        [created] = store.apply("agent", "agent", [
            {"op": "upsert_participant", "display_name": "Speaker 1"},
        ])
        pid = created.target_id
        store.apply("user", "p_me", [
            {"op": "rename_participant", "participant_id": pid,
             "display_name": "Sarah Chen"},
        ])
        suggestion = store.apply("agent", "agent", [
            {"op": "suggest_participant_name", "participant_id": pid,
             "display_name": "Sara"},
        ])[0]
        assert not suggestion.ok
        assert suggestion.reason == "human_named"
        snapshot = store.snapshot()
        assert snapshot["participants"][pid]["display_name"] == "Sarah Chen"


class TestQuestions:
    def test_open_question_cap_applies_to_agent(self):
        store, _ = make_store()
        for i in range(7):
            r = store.apply("agent", "agent", [
                {"op": "ask_question", "text": f"Question {i}?"},
            ])[0]
            assert r.ok
        capped = store.apply("agent", "agent", [
            {"op": "ask_question", "text": "One too many?"},
        ])[0]
        assert not capped.ok
        assert capped.reason == "question_limit"

    def test_resolution_confidence_thresholds(self):
        store, _ = make_store()

        def ask():
            return store.apply("agent", "agent", [
                {"op": "ask_question", "text": "Is the deadline fixed?"},
            ])[0].target_id

        # High confidence -> resolved with audio badge
        qid = ask()
        high = store.apply("agent", "agent", [
            {"op": "resolve_question", "question_id": qid,
             "answer_text": "Yes, end of Q3", "confidence": 0.9},
        ])[0]
        assert high.ok
        q = high.effect["question"]
        assert q["status"] == "resolved"
        assert q["answer_source"] == "audio"

        # Medium confidence -> greyed suggestion, still open
        qid = ask()
        mid = store.apply("agent", "agent", [
            {"op": "resolve_question", "question_id": qid,
             "answer_text": "Probably Q3", "confidence": 0.6},
        ])[0]
        assert mid.ok
        q = mid.effect["question"]
        assert q["status"] == "open"
        assert q["suggested_answer"] == "Probably Q3"

        # Low confidence -> rejected outright
        qid = ask()
        low = store.apply("agent", "agent", [
            {"op": "resolve_question", "question_id": qid,
             "answer_text": "Maybe", "confidence": 0.2},
        ])[0]
        assert not low.ok
        assert low.reason == "low_confidence"

    def test_human_answer_beats_agent_resolution(self):
        store, _ = make_store()
        qid = store.apply("agent", "agent", [
            {"op": "ask_question", "text": "Who owns rollout?"},
        ])[0].target_id
        answered = store.apply("user", "p_me", [
            {"op": "answer_question", "question_id": qid,
             "answer_text": "I do"},
        ])[0]
        assert answered.ok
        assert answered.effect["question"]["answer_source"] == "user"

        stale = store.apply("agent", "agent", [
            {"op": "resolve_question", "question_id": qid,
             "answer_text": "Sam does", "confidence": 0.95},
        ])[0]
        assert not stale.ok
        assert stale.reason == "already_closed"


class TestSegmentOps:
    def test_reassign_speaker_flows_through_handler(self):
        store, repo = make_store()
        [created] = store.apply("user", "p_me", [
            {"op": "upsert_participant", "display_name": "Sarah"},
        ])
        pid = created.target_id
        result = store.apply("user", "p_me", [
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": pid},
        ])[0]
        assert result.ok
        assert repo.segments["sg_known"]["speaker_participant_id"] == pid
        assert repo.segments["sg_known"]["speaker_pinned"] is True
        # Handler supplied the inverse from prior state
        assert result.inverse == {"op": "reassign_segment_speaker",
                                  "segment_id": "sg_known",
                                  "participant_id": None}


class TestStoreMechanics:
    def test_set_title_is_host_only(self):
        store, _ = make_store()
        guest = store.apply("user", "p_guest", [
            {"op": "set_title", "text": "Guest rename"},
        ])[0]
        assert guest.ok is False
        assert guest.reason == "host_only"
        assert store.snapshot()["title"] == ""
        host = store.apply("host", "p_me", [
            {"op": "set_title", "text": "Host title"},
        ])[0]
        assert host.ok is True
        assert store.snapshot()["title"] == "Host title"

    def test_seq_increments_per_applied_op(self):
        store, _ = make_store()
        results = store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "a"},
            {"op": "add_item", "card": "user_notes", "text": "b"},
            {"op": "unknown_thing"},
        ])
        assert [r.seq for r in results] == [1, 2, None]
        assert store.seq == 2

    def test_subscribers_receive_applied_only(self):
        store, _ = make_store()
        received = []
        store.subscribe(lambda seq, results: received.append((seq, results)))
        store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "a"},
            {"op": "bad_op"},
        ])
        assert len(received) == 1
        seq, results = received[0]
        assert seq == 1
        assert len(results) == 1
        assert results[0].ok

    def test_undo_restores_previous_text(self):
        store, repo = make_store()
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "decisions", "text": "Original"},
        ])
        [edited] = store.apply("user", "p_1", [
            {"op": "update_item", "id": added.target_id,
             "set": {"text": "Edited by human"}},
        ])
        undo_results = store.undo(edited.seq, "p_host")
        assert undo_results and undo_results[0].ok
        snapshot = store.snapshot()
        item = next(i for i in snapshot["cards"]["decisions"]
                    if i["id"] == added.target_id)
        assert item["text"] == "Original"
        # Undo restored the pre-edit status too (proposed)
        assert item["status"] == "proposed"

    def test_undo_of_add_removes_item(self):
        store, repo = make_store()
        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "key_points", "text": "Ephemeral"},
        ])
        undo_results = store.undo(added.seq, "p_host")
        assert undo_results and undo_results[0].ok
        snapshot = store.snapshot()
        item = next(i for i in snapshot["cards"]["key_points"]
                    if i["id"] == added.target_id)
        assert item["status"] == "removed"

    def test_snapshot_round_trips(self):
        store, _ = make_store()
        store.apply("agent", "agent", [
            {"op": "set_topic", "text": "Q3 planning"},
            {"op": "add_item", "card": "key_points", "text": "Point"},
            {"op": "ask_question", "text": "Why?"},
        ])
        snapshot = store.snapshot()
        rebuilt = MeetingState.from_dict(snapshot)
        assert rebuilt.to_dict() == snapshot


class TestClientAbusePrevention:
    """Dashboard clients are untrusted: guests must not be able to forge
    provenance, impersonate the host, or grow the state document without
    bound."""

    def test_guest_cannot_forge_answered_from_audio(self):
        """``resolve_question`` stamps ``answer_source='audio'``.

        Reaching it from a browser would attribute a guest's own words to the
        recording. Humans have ``answer_question`` instead.
        """
        store, _ = make_store()
        qid = store.apply("agent", "agent", [
            {"op": "ask_question", "text": "Who owns rollout?"},
        ])[0].target_id

        for actor in ("user", "host"):
            forged = store.apply(actor, "p_guest", [
                {"op": "resolve_question", "question_id": qid,
                 "answer_text": "fabricated", "confidence": 1.0},
            ])[0]
            assert not forged.ok
            assert forged.reason == "agent_only"

        snapshot = store.snapshot()
        question = next(q for q in snapshot["questions"] if q["id"] == qid)
        assert question["status"] == "open"
        assert question["answer_source"] is None

        # The human path still works and is labeled as such.
        answered = store.apply("user", "p_guest", [
            {"op": "answer_question", "question_id": qid, "answer_text": "I do"},
        ])[0]
        assert answered.ok
        assert answered.effect["question"]["answer_source"] == "user"

    def test_guests_cannot_rewrite_meeting_metadata(self):
        store, _ = make_store()
        for op in (
            {"op": "set_topic", "text": "hijacked"},
            {"op": "set_title", "text": "hijacked"},
            {"op": "set_rolling_summary", "text": "hijacked"},
        ):
            blocked = store.apply("user", "p_guest", [op])[0]
            assert not blocked.ok
            assert blocked.reason == "host_only"
            assert store.apply("host", "p_me", [op])[0].ok

    def test_oversized_item_data_is_rejected(self):
        store, _ = make_store()
        huge = {"note": "x" * 8000}
        added = store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "note",
             "data": huge},
        ])[0]
        assert not added.ok
        assert added.reason == "data_too_large"

        [ok_item] = store.apply("user", "p_1", [
            {"op": "add_item", "card": "user_notes", "text": "note"},
        ])
        updated = store.apply("user", "p_1", [
            {"op": "update_item", "id": ok_item.target_id, "set": {"data": huge}},
        ])[0]
        assert not updated.ok
        assert updated.reason == "data_too_large"

    def test_participant_and_question_floods_are_capped(self):
        from meeting.state.patches import (
            MAX_OPEN_QUESTIONS_HUMAN, MAX_PARTICIPANTS,
        )
        store, _ = make_store()
        for i in range(MAX_PARTICIPANTS):
            assert store.apply("user", "p_1", [
                {"op": "upsert_participant", "display_name": f"P{i}"},
            ])[0].ok
        flooded = store.apply("user", "p_1", [
            {"op": "upsert_participant", "display_name": "one too many"},
        ])[0]
        assert not flooded.ok
        assert flooded.reason == "participant_limit"

        for i in range(MAX_OPEN_QUESTIONS_HUMAN):
            assert store.apply("user", "p_1", [
                {"op": "ask_question", "text": f"Q{i}?"},
            ])[0].ok
        capped = store.apply("user", "p_1", [
            {"op": "ask_question", "text": "one too many?"},
        ])[0]
        assert not capped.ok
        assert capped.reason == "question_limit"


class TestPinnedSpeakerProtection:
    """A human speaker correction outranks any later automated relabel."""

    @staticmethod
    def _pinned_store():
        store, repo = make_store()
        store._segment_pinned = lambda sg_id: bool(
            repo.segments.get(sg_id, {}).get("speaker_pinned"))
        return store, repo

    def test_diarizer_cannot_revert_a_human_pin(self):
        store, repo = self._pinned_store()
        [sarah] = store.apply("user", "p_me", [
            {"op": "upsert_participant", "display_name": "Sarah"},
        ])
        [dana] = store.apply("user", "p_me", [
            {"op": "upsert_participant", "display_name": "Dana"},
        ])

        pinned = store.apply("user", "p_me", [
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": sarah.target_id},
        ])[0]
        assert pinned.ok
        assert repo.segments["sg_known"]["speaker_pinned"] is True

        # A re-cluster batch computed before the pin landed.
        stale = store.apply("system", "diarizer", [
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": dana.target_id},
        ])[0]
        assert not stale.ok
        assert stale.reason == "segment_pinned"
        assert repo.segments["sg_known"]["speaker_participant_id"] == sarah.target_id
        assert repo.segments["sg_known"]["speaker_pinned"] is True

    def test_a_later_human_correction_still_wins(self):
        store, repo = self._pinned_store()
        [sarah] = store.apply("user", "p_me", [
            {"op": "upsert_participant", "display_name": "Sarah"},
        ])
        [dana] = store.apply("user", "p_me", [
            {"op": "upsert_participant", "display_name": "Dana"},
        ])
        store.apply("user", "p_me", [
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": sarah.target_id},
        ])
        corrected = store.apply("host", "p_host", [
            {"op": "reassign_segment_speaker", "segment_id": "sg_known",
             "participant_id": dana.target_id},
        ])[0]
        assert corrected.ok
        assert repo.segments["sg_known"]["speaker_participant_id"] == dana.target_id
