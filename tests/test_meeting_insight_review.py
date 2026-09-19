"""Review policy and persistence tests use synthetic meetings and mocked providers."""
from copy import deepcopy
from unittest.mock import Mock
import json
import threading

import pytest

from meeting.insight_review import (
    CONSENT, TypeSafeReviewer, ReviewUnavailable, assess, evidence_context,
    review_config, run_review, start_review,
)
from meeting.state.schema import CardItem, FinalizationState, MeetingState
from meeting.state.store import MeetingStateStore
from services.settings import SettingsKey, resolve_meeting_insight_review


@pytest.fixture(autouse=True)
def _synthetic_master_switch(monkeypatch):
    monkeypatch.setattr("meeting.insight_review.resolve_typesafe_enabled", lambda: True)


class Repo:
    def __init__(self):
        self.segments = [{"id": "sg1", "text": "I could send the paper links.", "start_s": 10,
                          "speaker_participant_id": "p1"}]
        self.saved = None
        self.fail = False

    def get_segments(self, _):
        return deepcopy(self.segments)

    def persist_state(self, meeting_id, state):
        self.saved = deepcopy(state)

    def on_ops_applied(self, meeting_id, state, *args):
        if self.fail:
            raise RuntimeError("test persistence failure")
        self.saved = deepcopy(state)


def make_store(count=1, *, consent=CONSENT, enabled=True, cloud=True):
    repo = Repo()
    state = MeetingState(meeting_id="synthetic", status="ended", cloud_enabled=cloud,
                         finalization=FinalizationState(status="completed"),
                         insight_review=review_config(enabled=enabled, consent=consent))
    for i in range(count):
        state.cards["action_items"].append(CardItem(id=f"it{i}", card="action_items",
            text=f"Send paper links {i}", evidence=["sg1"]))
    state.cards["live_notes"].append(CardItem(id="note1", card="live_notes", text="Paper links will be sent.", evidence=["sg1"]))
    return MeetingStateStore(state, repository=repo), repo


class Reviewer:
    def __init__(self, scores=None):
        self.scores = scores or {"support": .9, "acceptance": .3, "owner": .2, "contradiction": .01, "asr_uncertain": .01}
        self.calls = 0

    def evaluate(self, state, questions, *, consent):
        assert consent == CONSENT
        self.calls += 1
        return dict(self.scores)


def prepare(store, repo, reviewer=None):
    assert store.apply("system", None, [{"op": "review_begin", "run_id": "r"}])[0].ok
    run_review(store, repo, "r", reviewer or Reviewer())
    return store.snapshot()["insight_review"]["questions"]


def answer(store, q, **kwargs):
    return store.apply("host", "host1", [{"op": "review_answer", "question_id": q["id"],
                                          "answer": "offered", **kwargs}])[0]


@pytest.mark.parametrize("settings", [{}, {SettingsKey.MEETING_INSIGHT_REVIEW: True},
    {SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT: CONSENT}])
def test_explicit_consent_and_enabled_are_both_required(settings):
    assert not resolve_meeting_insight_review(settings)["enabled"]


def test_setting_defaults_ignore_legacy_question_limit():
    result = resolve_meeting_insight_review({SettingsKey.TYPESAFE_ENABLED: True, SettingsKey.MEETING_INSIGHT_REVIEW: True,
        SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT: CONSENT,
        "meeting_insight_review_limit": 1,
        SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY: "invalid"})
    assert result == {"enabled": True, "consent": CONSENT, "sensitivity": "normal"}


@pytest.mark.parametrize("kwargs", [{"consent": ""}, {"enabled": False}, {"cloud": False}])
def test_no_worker_or_network_without_consent(monkeypatch, kwargs):
    store, repo = make_store(**kwargs)
    thread = Mock()
    monkeypatch.setattr("meeting.insight_review.threading.Thread", thread)
    assert not start_review(store, repo)["ok"]
    thread.assert_not_called()
    reviewer = Reviewer()
    run_review(store, repo, "r", reviewer)
    assert reviewer.calls == 0


def test_http_client_rejects_missing_consent_before_network(monkeypatch):
    post = Mock()
    monkeypatch.setattr("services.typesafe._http_post", post)
    with pytest.raises(ReviewUnavailable):
        TypeSafeReviewer("synthetic-key").evaluate({}, {}, consent="")
    post.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 2, True, "0.9", None])
def test_malformed_probability_is_unavailable(monkeypatch, value):
    post = Mock(return_value=(200, json.dumps({
        "answers": {"support": {"type": "noul", "noul": value}}})))
    monkeypatch.setattr("services.typesafe._http_post", post)
    with pytest.raises(ReviewUnavailable):
        TypeSafeReviewer("synthetic-key").evaluate(
            {}, {"support": {"type": "noul"}}, consent=CONSENT)
    post.assert_called_once()


def test_timeout_never_exposes_provider_or_key(monkeypatch):
    post = Mock(side_effect=TimeoutError("secret provider body"))
    monkeypatch.setattr("services.typesafe._http_post", post)
    with pytest.raises(ReviewUnavailable) as error:
        TypeSafeReviewer("secret-key").evaluate(
            {}, {"support": {"type": "noul"}}, consent=CONSENT)
    post.assert_called_once()
    assert "secret" not in str(error.value)


def test_review_uses_shared_model_transport_and_tls_bundle(monkeypatch):
    from services.typesafe import ENDPOINT, MODEL

    context = object()
    tls = Mock(return_value=context)
    response = Mock(status=200)
    response.read.return_value = json.dumps({
        "answers": {"support": {"type": "noul", "noul": .95}}}).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("services.typesafe.verified_context", tls)
    monkeypatch.setattr("services.typesafe.urllib.request.urlopen", urlopen)
    state = {"insight": "The team agreed to ship."}
    questions = {"support": {"type": "noul", "instructions": "Is it supported?"}}
    assert TypeSafeReviewer("synthetic-key").evaluate(
        state, questions, consent=CONSENT) == {"support": .95}
    request = urlopen.call_args.args[0]
    assert request.full_url == ENDPOINT
    assert request.get_header("Authorization") == "Bearer synthetic-key"
    assert json.loads(request.data) == {"model": MODEL, "state": state, "questions": questions}
    assert urlopen.call_args.kwargs == {"timeout": 12.0, "context": context}
    tls.assert_called_once()


@pytest.mark.parametrize("status,body", [
    (503, "secret provider response"),
    (200, "not json"),
    (200, '{"answers": {}}'),
    (200, '{"answers": {"support": {"type": "choice", "choice": "yes"}}}'),
])
def test_shared_client_failures_are_review_unavailable(monkeypatch, status, body):
    post = Mock(return_value=(status, body))
    monkeypatch.setattr("services.typesafe._http_post", post)
    with pytest.raises(ReviewUnavailable) as error:
        TypeSafeReviewer("secret-key").evaluate(
            {}, {"support": {"type": "noul"}}, consent=CONSENT)
    post.assert_called_once()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("legacy_limit", [None, 1, 3, 5])
def test_all_ranked_questions_survive_reload_without_confirming_items(legacy_limit):
    store, repo = make_store(8)
    if legacy_limit is not None:
        state = MeetingState.from_dict(store.snapshot())
        state.insight_review["max_questions"] = legacy_limit
        store.replace_document(state)

    class RankedReviewer(Reviewer):
        def evaluate(self, state, *args, **kwargs):
            scores = super().evaluate(state, *args, **kwargs)
            scores["acceptance"] = .8 - int(state["insight"]["text"].split()[-1]) * .09
            return scores

    questions = prepare(store, repo, RankedReviewer())
    assert [q["item_id"] for q in questions] == [f"it{i}" for i in reversed(range(8))]
    reopened = MeetingStateStore(MeetingState.from_dict(repo.saved), repository=repo)
    assert reopened.snapshot()["insight_review"]["questions"] == questions
    assert answer(reopened, questions[-1]).ok
    snapshot = store.snapshot()
    assert all(i["status"] == "proposed" for i in snapshot["cards"]["action_items"])
    assert all(i["review"]["state"] == "provisional" for i in snapshot["cards"]["action_items"])
    assert {q["field"] for q in questions} == {"acceptance"}


def test_high_scores_keep_inferred_separate_from_human_confirmation():
    store, repo = make_store()
    store.apply("system", None, [{"op": "update_item", "id": "it0", "set": {"data": {"owner_participant_id": "p1"}}}])
    assert prepare(store, repo, Reviewer({"support": .99, "acceptance": .99, "owner": .99, "contradiction": .01, "asr_uncertain": .01})) == []
    item = store.snapshot()["cards"]["action_items"][0]
    assert item["status"] == "proposed" and item["review"]["state"] == "inferred"


def test_contradiction_overrides_high_support_and_unclear_audio():
    store, repo = make_store()
    item = store.snapshot()["cards"]["action_items"][0]
    for override in ({"contradiction": .99}, {"asr_uncertain": .9}):
        scores = {"support": .99, "acceptance": .99, "owner": .99, **override}
        assessment, q = assess(item, repo.segments, [], Reviewer(scores), "normal", consent=CONSENT)
        assert q["field"] == "support"
        assert assessment["review"]["state"] != "inferred"


def test_missing_evidence_does_not_invent_a_score_from_network():
    store, repo = make_store()
    item = store.snapshot()["cards"]["action_items"][0]
    item["evidence"] = ["missing"]
    reviewer = Reviewer()
    assessment, q = assess(item, repo.segments, [], reviewer, "normal", consent=CONSENT)
    assert reviewer.calls == 0
    assert assessment["review"]["state"] == "unsupported"
    assert "missing" in q["reason"]


def test_context_includes_later_resolution_in_chronological_order():
    item = {"text": "Paper links task", "evidence": ["sg0"]}
    segments = [{"id": f"sg{i}", "start_s": i, "text": "Unrelated discussion"} for i in range(100)]
    segments[0]["text"] = "Paper links may be sent"
    segments[-1]["text"] = "Paper links task is canceled"
    context = evidence_context(item, segments)
    assert context[0]["id"] == "sg0" and context[-1]["id"] == "sg99"


def test_answer_updates_linked_insight_notes_and_durable_report():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert answer(store, q).ok
    state = MeetingState.from_dict(repo.saved)
    item = state.find_item("it0")
    assert item.protected and item.status == "edited"
    assert item.data["commitment"] == "offered"
    assert item.text.startswith("Offered, not agreed:")
    assert "User clarification" in state.find_item("note1").text
    assert len(state.cards["user_notes"]) == 1
    assert repo.segments[0]["text"] == "I could send the paper links."
    q = state.insight_review["questions"][0]
    assert q["history"][0]["before"]["text"] == "Send paper links 0"
    assert q["answered_by"] == "host1"
    from meeting.export.markdown import export_markdown
    exported = export_markdown({}, state.to_dict(), repo.segments)
    assert "User Clarifications" in exported and "Only offered, not agreed" in exported
    result = store.apply("agent", "model", [{"op": "update_item", "id": item.id,
        "base_revision": item.revision, "set": {"text": "Committed to sending"}, "evidence": ["sg1"]}])[0]
    assert result.reason == "human_edited"


def test_duplicate_answer_and_stale_revision_are_rejected():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    store.apply("host", None, [{"op": "update_item", "id": "it0", "set": {"text": "New human wording"}}])
    assert answer(store, q).reason == "review_changed"
    assert not store.snapshot()["cards"]["user_notes"]
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert answer(store, q).ok
    assert answer(store, q).reason == "already_closed"


def test_skip_reopen_and_revoking_incorrect_answer():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert store.apply("host", None, [{"op": "review_skip", "question_id": q["id"]}])[0].ok
    assert store.snapshot()["cards"]["action_items"][0]["status"] == "proposed"
    assert store.apply("host", None, [{"op": "review_reopen", "question_id": q["id"]}])[0].ok
    assert answer(store, q, answer="incorrect").ok
    assert store.apply("host", None, [{"op": "review_reopen", "question_id": q["id"]}])[0].ok
    assert answer(store, q, answer="accepted").ok
    assert store.snapshot()["cards"]["action_items"][0]["status"] == "edited"


def test_persistence_failure_is_atomic_and_retryable():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    before = store.snapshot()
    repo.fail = True
    assert answer(store, q).reason == "persistence_error"
    assert store.snapshot() == before
    repo.fail = False
    assert answer(store, q).ok


@pytest.mark.parametrize("actor", ["agent", "user"])
def test_only_host_can_answer_and_only_system_can_publish(actor):
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert not store.apply(actor, None, [{"op": "review_answer", "question_id": q["id"], "answer": "accepted"}])[0].ok
    assert not store.apply("host", None, [{"op": "review_finish", "run_id": "r", "status": "ready"}])[0].ok


def test_changed_insight_is_not_scored_by_a_stale_worker():
    store, repo = make_store()
    class EditingReviewer(Reviewer):
        def evaluate(self, *args, **kwargs):
            store.apply("host", None, [{"op": "update_item", "id": "it0", "set": {"text": "Corrected during check"}}])
            return super().evaluate(*args, **kwargs)
    assert prepare(store, repo, EditingReviewer()) == []
    assert store.snapshot()["cards"]["action_items"][0]["review"] == {}


def test_changed_transcript_discards_old_assessment():
    store, repo = make_store()
    class EditingReviewer(Reviewer):
        def evaluate(self, *args, **kwargs):
            repo.segments[0]["text"] = "The paper task was canceled."
            return super().evaluate(*args, **kwargs)
    assert prepare(store, repo, EditingReviewer()) == []
    assert store.snapshot()["insight_review"]["status"] == "unavailable"


def test_background_start_is_nonblocking_and_duplicate_start_is_rejected(monkeypatch):
    store, repo = make_store()
    entered, release = threading.Event(), threading.Event()
    def work(*_):
        entered.set()
        release.wait(3)
    monkeypatch.setattr("meeting.insight_review.run_review", work)
    try:
        assert start_review(store, repo)["ok"]
        assert entered.wait(1)
        assert not start_review(store, repo)["ok"]
        assert store.snapshot()["status"] == "ended"
    finally:
        release.set()


def test_archive_api_host_action_survives_restart(tmp_path):
    from fastapi.testclient import TestClient
    from meeting.persist.repository import SqlMeetingRepository
    from meeting.web.archive import ArchivedMeetingDashboard
    from meeting.web.server import MeetingWebServer
    from services.database import DatabaseManager
    store, fake = make_store()
    q = prepare(store, fake)[0]
    database = DatabaseManager(db_path=str(tmp_path / "review.db"))
    repo = SqlMeetingRepository(db=database)
    repo.create_meeting(id="synthetic", title="Synthetic review", status="ended", cloud_enabled=True,
                        host_token="test-host", guest_token="test-guest", spool_dir=str(tmp_path),
                        started_at="2026-09-18T10:00:00Z", ended_at="2026-09-18T10:10:00Z",
                        state_json=json.dumps(store.snapshot()), state_seq=store.seq)
    archive = ArchivedMeetingDashboard(repo, repo.get_meeting("synthetic"), spool_root=str(tmp_path))
    server = MeetingWebServer(archive, repo)
    try:
        with TestClient(server.app) as client:
            op = {"op": "review_answer", "question_id": q["id"], "answer": "offered"}
            route = "/api/meetings/synthetic/review"
            assert client.post(route, params={"token": "test-guest"}, json=op).status_code == 403
            response = client.post(route, params={"token": "test-host"}, json=op)
            assert response.status_code == 200 and response.json()["ok"]
        reopened = ArchivedMeetingDashboard(repo, repo.get_meeting("synthetic"), spool_root=str(tmp_path))
        assert reopened.store.snapshot()["insight_review"]["questions"][0]["status"] == "answered"
        assert reopened.store.snapshot()["cards"]["action_items"][0]["data"]["commitment"] == "offered"
    finally:
        database.close()


def test_later_human_edit_supersedes_old_correction_everywhere():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert answer(store, q).ok
    result = store.apply("host", None, [{"op": "update_item", "id": "it0",
                                        "set": {"text": "Agreed later to send links"}}])[0]
    assert result.effect["entity"] == "review"
    assert store.snapshot()["insight_review"]["questions"][0]["superseded"]
    assert "superseded" in store.snapshot()["cards"]["user_notes"][0]["text"]
    from meeting.export.markdown import export_markdown
    assert "## User Clarifications" not in export_markdown({}, store.snapshot(), repo.segments)


def test_revising_offered_to_accepted_replaces_the_old_clarification():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    assert answer(store, q).ok
    assert store.apply("host", None, [{"op": "review_reopen", "question_id": q["id"]}])[0].ok
    assert answer(store, q, answer="accepted").ok
    state = store.snapshot()
    assert state["cards"]["action_items"][0]["text"] == "Send paper links 0"
    assert "Only offered" not in state["cards"]["live_notes"][0]["text"]
    assert state["cards"]["live_notes"][0]["text"].count("User clarification") == 1


def test_partial_failure_keeps_successful_checks():
    store, repo = make_store(2)
    class PartialReviewer(Reviewer):
        def evaluate(self, state, *args, **kwargs):
            if state["insight"]["text"].endswith("1"):
                raise ReviewUnavailable("test failure")
            return super().evaluate(state, *args, **kwargs)
    questions = prepare(store, repo, PartialReviewer())
    assert len(questions) == 1
    assert store.snapshot()["insight_review"]["status"] == "partial"


def test_new_finalization_invalidates_open_reviews_but_preserves_human_answers():
    store, repo = make_store(2)
    questions = prepare(store, repo)
    assert answer(store, questions[0]).ok
    assert store.update_runtime_fields(finalization={"status": "running"})
    assert answer(store, questions[1]).reason == "meeting_not_ready"
    state = store.snapshot()
    assert state["insight_review"]["questions"][1]["superseded"]
    assert not state["insight_review"]["questions"][0].get("superseded")
    assert state["cards"]["action_items"][0]["review"]["state"] == "human"
    assert state["cards"]["action_items"][1]["review"] == {}


def test_replaced_evidence_invalidates_a_question_even_at_same_item_revision():
    store, repo = make_store()
    q = prepare(store, repo)[0]
    state = MeetingState.from_dict(store.snapshot())
    state.find_item("it0").evidence = ["replacement"]
    store.replace_document(state)
    assert answer(store, q).reason == "review_changed"


def test_cloud_revocation_does_not_leave_a_review_pending():
    store, repo = make_store(cloud=False)
    assert not start_review(store, repo)["ok"]
    assert store.snapshot()["insight_review"]["status"] == "unavailable"


def test_owner_clarification_requires_consistent_wording_and_updates_metadata():
    from meeting.state.schema import Participant
    store, repo = make_store()
    state = MeetingState.from_dict(store.snapshot())
    state.participants["p1"] = Participant(id="p1", display_name="Alex")
    store.replace_document(state)
    q = prepare(store, repo, Reviewer({"support": .99, "acceptance": .99, "owner": .1}))[0]
    assert q["field"] == "owner"
    assert answer(store, q, answer="edit", owner_participant_id="p1").reason == "review_wording_required"
    assert answer(store, q, answer="edit", owner_participant_id="p1", text="Alex will send the paper links.").ok
    snapshot = store.snapshot()
    assert snapshot["cards"]["action_items"][0]["data"]["owner_participant_id"] == "p1"
    assert "Owner: Alex" in snapshot["insight_review"]["questions"][0]["correction"]


def test_deadline_can_be_explicitly_cleared_without_becoming_a_fact():
    store, repo = make_store()
    state = MeetingState.from_dict(store.snapshot())
    item = state.find_item("it0")
    item.text = "Send paper links by Friday."
    item.data = {"owner_participant_id": "p1", "deadline": "Friday"}
    store.replace_document(state)
    q = prepare(store, repo, Reviewer({"support": .99, "acceptance": .99, "owner": .99, "deadline": .3}))[0]
    assert q["field"] == "deadline"
    assert answer(store, q, answer="edit", text="Send paper links; no deadline was agreed.", deadline="").ok
    snapshot = store.snapshot()
    assert snapshot["cards"]["action_items"][0]["data"]["deadline"] is None
    assert "Deadline: None agreed" in snapshot["insight_review"]["questions"][0]["correction"]


def test_master_switch_off_blocks_review_even_with_review_consent():
    assert not resolve_meeting_insight_review({
        SettingsKey.MEETING_INSIGHT_REVIEW: True,
        SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT: CONSENT,
        SettingsKey.TYPESAFE_ENABLED: False,
    })["enabled"]


def test_master_switch_revocation_prevents_the_next_http_request(monkeypatch):
    monkeypatch.setattr("meeting.insight_review.resolve_typesafe_enabled", lambda: False)
    post = Mock()
    monkeypatch.setattr("services.typesafe._http_post", post)
    with pytest.raises(ReviewUnavailable, match="off"):
        TypeSafeReviewer("synthetic-key").evaluate({}, {}, consent=CONSENT)
    post.assert_not_called()
