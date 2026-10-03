"""Mutation isolation and metadata reconciliation for state write-through."""
from copy import deepcopy

import pytest

from meeting.state.schema import CardItem, CustomReport, MeetingState, Participant, Question
from meeting.state.store import MeetingStateStore


class RejectingRepository:
    def on_ops_applied(self, *args):
        raise RuntimeError("storage unavailable")

    def persist_state(self, *args):
        raise RuntimeError("storage unavailable")


def test_mutated_domains_do_not_leak_when_persistence_fails():
    state = MeetingState(meeting_id="m_isolated", cloud_enabled=True)
    state.cards["key_points"] = [CardItem(
        id="it_budget", card="key_points", text="Budget is pending",
        data={"nested": {"amount": 100}},
    )]
    state.participants["p_person"] = Participant(id="p_person", display_name="Before")
    state.questions["q_question"] = Question(id="q_question", text="Budget?")
    state.custom_reports = [CustomReport(id="r_report", request="Budget", status="running", run_id="run")]
    store = MeetingStateStore(state, repository=RejectingRepository())
    before = deepcopy(store.snapshot())
    results = store.apply("host", "p_person", [
        {"op": "update_item", "id": "it_budget", "base_revision": 1,
         "set": {"text": "Budget approved", "data": {"nested": {"amount": 200}}}},
        {"op": "rename_participant", "participant_id": "p_person", "display_name": "After"},
        {"op": "dismiss_question", "question_id": "q_question"},
        {"op": "set_topic", "text": "New topic"},
        {"op": "remove_custom_report", "report_id": "r_report"},
    ])
    assert all(not result.ok and result.reason == "persistence_error" for result in results)
    assert store.snapshot() == before
    assert not store.update_runtime_fields(capture={"mic_available": True})
    assert store.snapshot() == before


def test_runtime_finalization_invalidation_rolls_back_with_failed_write():
    state = MeetingState(meeting_id="m_review", cloud_enabled=True)
    state.cards["key_points"] = [CardItem(
        id="it_fact", card="key_points", text="Fact", citation_check={"ok": True},
        review={"state": "checked"},
    )]
    state.insight_review = {"enabled": True, "status": "ready", "questions": [
        {"id": "review_q", "status": "open"},
    ]}
    store = MeetingStateStore(state, repository=RejectingRepository())
    before = deepcopy(store.snapshot())
    assert not store.update_runtime_fields(finalization={"status": "running"})
    assert store.snapshot() == before


def test_repaired_item_provenance_flag_rolls_back_with_failed_write():
    state = MeetingState(meeting_id="m_repair", cloud_enabled=True)
    state.cards["key_points"] = [CardItem(
        id="it_repair", card="key_points", text="Original snippet",
        author_type="system", author_id="state_repair", data={"source": "audio"},
        evidence=["sg_audio"],
    )]
    store = MeetingStateStore(state, repository=RejectingRepository())
    before = store.snapshot()
    result = store.apply("agent", "core", [{
        "op": "update_item", "id": "it_repair", "base_revision": 1,
        "set": {"text": "Synthesized insight"}, "evidence": ["sg_audio"],
    }])[0]
    assert not result.ok and result.reason == "persistence_error"
    assert store.snapshot() == before


def test_runtime_values_are_owned_by_store_after_commit():
    store = MeetingStateStore(MeetingState(meeting_id="m_owned"))
    speech = {"nested": {"ready": True}}
    assert store.update_runtime_fields(speech=speech)
    speech["nested"]["ready"] = False
    assert store.snapshot()["speech"]["nested"]["ready"] is True


def test_op_values_are_owned_by_store_after_commit():
    store = MeetingStateStore(MeetingState(meeting_id="m_owned"))
    data = {"nested": {"ready": True}}
    result = store.apply("host", None, [{"op": "add_item", "card": "key_points",
                                        "text": "Ready", "data": data}])[0]
    assert result.ok
    data["nested"]["ready"] = False
    assert store.snapshot()["cards"]["key_points"][0]["data"]["nested"]["ready"] is True


@pytest.mark.parametrize("metadata_only", [False, True])
def test_repository_reconciliation_keeps_canonical_title_and_contract(metadata_only):
    class Repository:
        state_metadata_only = metadata_only

        def on_ops_applied(self, meeting_id, snapshot, *args):
            snapshot["title"] = "Renamed elsewhere"
            if not metadata_only:
                snapshot["rolling_summary"] = "Repository transformation"
            return snapshot

        def persist_state(self, meeting_id, snapshot):
            snapshot["title"] = "Renamed again"
            return snapshot

    store = MeetingStateStore(MeetingState(meeting_id="m_title"), repository=Repository())
    assert store.apply("host", None, [{"op": "set_rolling_summary", "text": "Summary"}])[0].ok
    snapshot = store.snapshot()
    assert snapshot["title"] == "Renamed elsewhere"
    assert snapshot["rolling_summary"] == ("Summary" if metadata_only else "Repository transformation")
    assert store.update_runtime_fields(status="paused")
    assert store.snapshot()["title"] == "Renamed again"


def test_all_registered_handlers_have_explicit_mutation_domains():
    from meeting.state.patches import _HANDLERS
    from meeting.state.store import _OP_DOMAINS

    assert set(_HANDLERS) == set(_OP_DOMAINS)
