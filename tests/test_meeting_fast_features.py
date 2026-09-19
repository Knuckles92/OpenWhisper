import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from meeting.citation_verifier import CitationVerifier
from meeting.corrections import term_rules, correct_text
from meeting.interfaces import AgentResult, OpResult, TranscriptSegment
from meeting.live_signals import LiveSignals, window_request, window_ops
from meeting.semantic_search import search_history
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from meeting.voice_actions import correction_op
from services.settings import resolve_typesafe_feature_enabled
from services.typesafe import ChoiceAnswer, MAX_STATE_CHARS


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **kwargs):
        self.jobs.clear()


def row(sid="sg_one", text="We agree to ship the beta on Friday.", start=10, end=15):
    return {"id": sid, "text": text, "start_s": start, "end_s": end}


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get_segment(self, mid, sid):
        return next((dict(r) for r in self.rows if r["id"] == sid), None)

    def get_segments(self, mid, **kwargs):
        return self.rows


def store():
    return MeetingStateStore(MeetingState("m_test", cloud_enabled=True))


def add_claim(target):
    result, = target.apply("agent", "notes", [{"op": "add_item", "card": "decisions",
        "text": "The beta ships on Friday.", "evidence": ["sg_one"]}])
    return result.effect["item"]


@pytest.mark.parametrize("feature", ["citations", "semantic_search", "question_radar", "highlights"])
def test_new_features_require_explicit_opt_in(feature):
    assert not resolve_typesafe_feature_enabled(feature, {"typesafe_enabled": True})
    assert not resolve_typesafe_feature_enabled(feature, {f"typesafe_{feature}_enabled": True})
    assert resolve_typesafe_feature_enabled(feature, {"typesafe_enabled": True, f"typesafe_{feature}_enabled": True})


def test_pulse_anchors_and_question_sources_are_copied_not_generated():
    state, questions = window_request([row(), row("sg_q", "Who will own the rollout?", 30, 34)], {"questions": []})
    answers = {"decision": {"noul": .95}, "decision_anchor": {"choice": "sg_one"},
               "candidate_0": {"noul": .98}}
    ops = window_ops(0, state, answers, {"questions": []})
    assert ops[0]["pulses"][0]["start_s"] == 10
    assert ops[0]["pulses"][0]["segment_id"] == "sg_one"
    assert ops[1] == {"op": "ask_question", "text": "Who will own the rollout?", "evidence": ["sg_q"]}
    assert questions["decision"]["type"] == "noul"
    assert window_ops(0, state, {}, {"questions": []}) == []


def test_question_radar_keeps_answers_as_suggestions_and_respects_dismissal():
    q = {"id": "q_a", "text": "Who owns the rollout?", "status": "open"}
    state, _ = window_request([row(text="Maya owns the rollout.")], {"questions": [q]})
    answers = {"answer_q_a": {"choice": "sg_one", "confidence": .98}}
    op, = window_ops(0, state, answers, {"questions": [q]})
    assert op["confidence"] < .8 and op["answer_text"] == "Maya owns the rollout."
    assert window_ops(0, state, answers, {"questions": [{**q, "status": "dismissed"}]}) == []


def test_request_budget_with_dense_minute_and_full_inbox():
    qs = [{"id": f"q_{i}", "text": "Who owns the product launch and when will it happen?", "status": "open"} for i in range(7)]
    rows = [row(f"sg_{i:012}", "Who owns the next release? " + "A useful sentence. " * 5, i * 3, i * 3 + 2) for i in range(30)]
    state, questions = window_request(rows, {"questions": qs})
    assert len(json.dumps({"state": state, "questions": questions, "model": "jev-1.13.0"})) < MAX_STATE_CHARS


def test_minute_worker_is_nonblocking_bounded_and_deduplicates(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    target, pool, calls = store(), ManualExecutor(), []
    judge = SimpleNamespace(ask=lambda s, q: calls.append(s) or {})
    worker = LiveSignals(target, Repo([row()]), judge, lambda: True, executor=pool)
    worker.observe([row(end=59)])
    assert not pool.jobs
    worker.observe([row(end=60)])
    worker.observe([row(end=120)])
    assert len(pool.jobs) == 1 and not calls
    pool.run()
    worker.observe([row(end=60)])
    assert len(calls) == 1 and not pool.jobs
    worker.shutdown()
    worker.observe([row(end=180)])
    assert not pool.jobs


def test_minute_consent_revoked_in_flight_discards_answers(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    allowed = [True]
    target = store()
    def ask(s, q):
        allowed[0] = False
        return {"decision": {"noul": .99}, "decision_anchor": {"choice": "sg_one"}}
    worker = LiveSignals(target, Repo([row()]), SimpleNamespace(ask=ask), lambda: allowed[0], executor=ManualExecutor())
    worker._run(0)
    assert not target.snapshot()["live_highlights"]


@pytest.mark.parametrize("enabled,allowed", [(False, True), (True, False)])
def test_minute_disabled_never_calls_remote(monkeypatch, enabled, allowed):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: enabled)
    worker = LiveSignals(store(), Repo([row()]), SimpleNamespace(ask=lambda *_: pytest.fail("remote call")), lambda: allowed, executor=ManualExecutor())
    worker._run(0)


def test_verifier_annotations_do_not_change_claim_status_or_revision(monkeypatch):
    monkeypatch.setattr("meeting.citation_verifier.resolve_typesafe_feature_enabled", lambda _: True)
    target, pool = store(), ManualExecutor()
    verifier = CitationVerifier(target, Repo([row()]), SimpleNamespace(choice=lambda *_: ChoiceAnswer("unsupported", .96)), lambda: True, executor=pool)
    item = add_claim(target)
    assert target.snapshot()["cards"]["decisions"][0]["citation_check"] == {}
    pool.run()
    checked = target.snapshot()["cards"]["decisions"][0]
    assert checked["status"] == "proposed" and checked["text"] == item["text"] and checked["revision"] == 1
    assert checked["citation_check"]["status"] == "unsupported"
    verifier.shutdown()


def test_verifier_failure_and_missing_evidence_are_advisory(monkeypatch):
    monkeypatch.setattr("meeting.citation_verifier.resolve_typesafe_feature_enabled", lambda _: True)
    target, pool = store(), ManualExecutor()
    verifier = CitationVerifier(target, Repo([row()]), SimpleNamespace(choice=lambda *_: None), lambda: True, executor=pool)
    add_claim(target)
    pool.run()
    assert target.snapshot()["cards"]["decisions"][0]["citation_check"]["status"] == "unavailable"
    verifier.repository.rows = []
    verifier.invalidate()
    pool.run()
    assert target.snapshot()["cards"]["decisions"][0]["citation_check"]["status"] == "missing"


def test_verifier_rejects_stale_revision_and_changed_source(monkeypatch):
    monkeypatch.setattr("meeting.citation_verifier.resolve_typesafe_feature_enabled", lambda _: True)
    target, repo = store(), Repo([row()])
    item = add_claim(target)
    def choice(*args):
        repo.rows[0]["text"] = "We did not agree."
        return ChoiceAnswer("supported", .99)
    verifier = CitationVerifier(target, repo, SimpleNamespace(choice=choice), lambda: True, executor=ManualExecutor())
    verifier.check(item)
    assert not target.snapshot()["cards"]["decisions"][0]["citation_check"]
    target.apply("host", "me", [{"op": "update_item", "id": item["id"], "set": {"text": "A corrected claim"}}])
    result, = target.apply("system", "citation-verifier", [{"op": "citation_check", "id": item["id"], "revision": 1, "check": {"status": "supported"}}])
    assert not result.ok


def test_annotations_cannot_be_forged_by_guest():
    target = store()
    item = add_claim(target)
    for op in [{"op": "citation_check", "id": item["id"], "revision": 1}, {"op": "publish_highlights", "pulses": []}, {"op": "voice_feedback", "message": "fake"}]:
        assert not target.apply("user", "guest", [op])[0].ok


@pytest.mark.parametrize("spoken", ['Note taker, replace Myra with Maya.', "Assistant, I said Maya not Myra.", 'Note taker, change "Myra" to "Maya" please.'])
def test_spoken_correction_roundtrips_and_removal_undoes(spoken):
    target = store()
    op = correction_op(row("sg_cmd", spoken, 20, 25), [row(text="Myra owns release notes.")])
    assert op is not None
    result, = target.apply("system", "voice_command", [op])
    assert correct_text("Myra owns release notes.", term_rules(target.snapshot())) == "Maya owns release notes."
    assert target.snapshot()["cards"]["user_notes"][0]["author_type"] == "system"
    target.apply("host", "me", [{"op": "remove_item", "id": result.target_id}])
    assert not term_rules(target.snapshot())


def test_spoken_correction_without_source_or_replacement_is_rejected():
    assert correction_op(row(text="Note taker, fix that name."), [row()]) is None
    assert correction_op(row(text="Note taker, replace Myra with Maya."), [row()]) is None


def test_recap_routes_to_note_agent_and_reports_completion():
    from meeting.engine import MeetingEngine
    target, requests, future = store(), [], Future()
    engine = MeetingEngine.__new__(MeetingEngine)
    engine.store = target
    engine.request_note_adjustment = lambda text: requests.append(text) or future
    engine._apply_spoken_action("recap", row(text="Note taker, recap the decisions."), [])
    assert "recap the decisions" in requests[0]
    assert "requested" in target.snapshot()["voice_feedback"]["message"]
    future.set_result(AgentResult(True, [OpResult(True, {"op": "add_item"})]))
    assert "added" in target.snapshot()["voice_feedback"]["message"]


def test_semantic_candidates_include_non_keyword_passages_and_exclude_offline(repo):
    for mid, cloud, text in [("m_online", True, "We agreed to postpone the launch until November."), ("m_private", False, "The release was delayed for a private reason.")]:
        repo.create_meeting(id=mid, title="Launch", status="ended", started_at="2026-09-18T10:00:00", host_token=mid, guest_token=mid+"g", cloud_enabled=cloud, spool_dir="/tmp/spool")
        repo.add_segments([TranscriptSegment("sg_"+mid, mid, None, "mic", 10, 15, text)])
    rows = repo.search_candidates("shipping delay")
    assert [r["meeting_id"] for r in rows] == ["m_online"]
    assert repo.search_candidates("shipping delay", exclude_meeting_id="m_online") == []


def test_semantic_ranking_and_failure_fallback(monkeypatch):
    monkeypatch.setattr("meeting.semantic_search.resolve_typesafe_feature_enabled", lambda _: True)
    candidates = [{"segment_id": "sg_a", "meeting_id": "m_a", "text": "Launch postponed"}, {"segment_id": "sg_b", "meeting_id": "m_b", "text": "Coffee break"}]
    repo = SimpleNamespace(search_transcripts=lambda *a, **k: [{"text": "keyword"}], search_candidates=lambda *a, **k: candidates, get_meeting=lambda mid: {"cloud_enabled": True})
    judge = SimpleNamespace(ask=lambda s, q: {"p0": {"noul": .96}, "p1": {"noul": .08}})
    result = search_history(repo, "shipping delay", semantic=True, judge=judge)
    assert result["mode"] == "semantic" and len(result["results"]) == 1
    assert result["results"][0]["semantic_score"] == .96
    judge.ask = lambda *a: None
    result = search_history(repo, "shipping delay", semantic=True, judge=judge)
    assert result["mode"] == "keyword" and result["results"] == [{"text": "keyword"}]


def test_persisted_signals_round_trip():
    target = store()
    result, = target.apply("system", "live-signals", [{"op": "publish_highlights", "pulses": [{"id": "pulse_0_decision", "kind": "decision", "start_s": 10, "segment_id": "sg_one", "probability": .9, "text": "Ship"}]}])
    assert result.ok
    snapshot = target.snapshot()
    assert MeetingState.from_dict(snapshot).to_dict() == snapshot


def test_semantic_disabled_never_reads_remote_candidates(monkeypatch):
    monkeypatch.setattr("meeting.semantic_search.resolve_typesafe_feature_enabled", lambda _: False)
    repo = SimpleNamespace(search_transcripts=lambda *a, **k: [], search_candidates=lambda *a, **k: pytest.fail("remote candidates"))
    result = search_history(repo, "shipping delay", semantic=True, judge=SimpleNamespace(ask=lambda *a: pytest.fail("remote call")))
    assert result["mode"] == "keyword" and "Enable" in result["message"]


def test_semantic_rechecks_meeting_consent_before_sending(monkeypatch):
    monkeypatch.setattr("meeting.semantic_search.resolve_typesafe_feature_enabled", lambda _: True)
    repo = SimpleNamespace(search_transcripts=lambda *a, **k: [], search_candidates=lambda *a, **k: [{"meeting_id": "private", "text": "Do not send"}], get_meeting=lambda mid: {"cloud_enabled": False})
    result = search_history(repo, "shipping delay", semantic=True, judge=SimpleNamespace(ask=lambda *a: pytest.fail("remote call")))
    assert result["results"] == []


def test_minute_discards_source_changed_during_request(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    target, repository = store(), Repo([row()])
    def ask(*args):
        repository.rows[0]["text"] = "No decision was made."
        return {"decision": {"noul": .99}, "decision_anchor": {"choice": "sg_one"}}
    worker = LiveSignals(target, repository, SimpleNamespace(ask=ask), lambda: True, executor=ManualExecutor())
    worker._run(0)
    assert target.snapshot()["live_highlights"] == []


def test_no_advisory_writes_when_no_checks_exist():
    target, pool = store(), ManualExecutor()
    verifier = CitationVerifier(target, Repo([row()]), None, lambda: False, executor=pool)
    add_claim(target)
    seq = target.seq
    verifier.invalidate()
    pool.run()
    assert target.seq == seq
