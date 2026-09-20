"""Takeaways share the existing pulse consent, evidence, and publication flow."""
import json

import pytest

from meeting.live_signals import LiveSignals, window_request
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from services.typesafe import TypeSafeJudge
from tests.test_meeting_fast_features import ManualExecutor, Repo, row


def test_all_five_pulse_types_publish_in_one_validated_request(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled",
                        lambda feature: feature == "highlights")
    passages = {
        "decision": "We agree to ship the beta.",
        "disagreement": "Sales and engineering disagree about the scope.",
        "commitment": "I will send the release notes by Friday.",
        "number": "The budget is five thousand dollars.",
        "takeaway": "We learned that early customer feedback prevents rework.",
    }
    rows = [row(f"sg_{kind}", text, 5 + index * 10, 8 + index * 10)
            for index, (kind, text) in enumerate(passages.items())]
    repo = Repo(rows)
    store = MeetingStateStore(MeetingState("m_test", cloud_enabled=True),
                              segment_exists=lambda sid: repo.get_segment("m_test", sid) is not None)
    requests = []

    def transport(payload, timeout_s):
        requests.append(payload)
        assert set(payload["questions"]) == set(passages) | {kind + "_anchor" for kind in passages}
        assert payload["questions"]["takeaway"]["type"] == "noul"
        assert payload["questions"]["takeaway_anchor"]["criteria"]["none"]
        answers = {}
        for kind in passages:
            answers[kind] = {"type": "noul", "noul": .93}
            answers[kind + "_anchor"] = {
                "type": "choice", "choice": f"sg_{kind}", "confidence": .9,
                "probabilities": {sid: float(sid == f"sg_{kind}")
                                  for sid in [*payload["state"]["passages"], "none"]},
            }
        return 200, json.dumps({"answers": answers})

    queue = ManualExecutor()
    worker = LiveSignals(store, repo, TypeSafeJudge("test-key", transport=transport),
                         lambda: True, executor=queue)
    worker.observe([], frontier=60)
    queue.run()
    pulses = store.snapshot()["live_highlights"]
    assert len(requests) == 1
    assert {pulse["kind"] for pulse in pulses} == set(passages)
    for pulse, source in zip(pulses, rows):
        assert pulse["segment_id"] == source["id"]
        assert pulse["text"] == source["text"]
        assert pulse["start_s"] == source["start_s"]
        assert pulse["id"] == f"pulse_0_{pulse['kind']}"
        assert pulse["assessment"]["scores"] == dict.fromkeys(passages, .93)
        assert pulse["assessment"]["source_probability"] == 1
    worker.observe(rows)
    queue.run()
    assert len(requests) == 1
    assert store.snapshot()["live_highlights"] == pulses


def test_takeaway_is_not_requested_when_highlights_are_disabled():
    _, questions = window_request([row(text="What did we learn from the launch?")], {},
                                  highlights=False, radar=True)
    assert set(questions) == {"candidate_0"}


@pytest.mark.parametrize("actor,actor_id", [("host", "me"), ("agent", "notes"), ("system", "other")])
def test_takeaway_publication_remains_system_owned(actor, actor_id):
    store = MeetingStateStore(MeetingState("m_test", cloud_enabled=True))
    result, = store.apply(actor, actor_id, [{"op": "publish_highlights", "pulses": [{
        "id": "pulse_0_takeaway", "kind": "takeaway", "start_s": 10,
        "segment_id": "sg_one", "probability": .93, "text": "Early feedback prevents rework.",
    }]}])
    assert not result.ok
    assert not store.snapshot()["live_highlights"]


def test_takeaway_replacement_does_not_leave_old_sources_or_clear_other_minutes():
    store = MeetingStateStore(MeetingState("m_test", cloud_enabled=True),
                              segment_exists=lambda sid: sid in {"old", "new"})
    pulse = {"id": "pulse_0_takeaway", "kind": "takeaway", "start_s": 10,
             "segment_id": "old", "probability": .93, "text": "Early feedback prevents rework."}

    def publish(minute, pulses):
        return store.apply("system", "live-signals", [{
            "op": "publish_highlights", "minute": minute, "pulses": pulses,
        }])[0]

    assert publish(0, [pulse]).ok
    later = {**pulse, "id": "pulse_1_takeaway", "start_s": 70}
    assert publish(1, [later]).ok
    replacement = {**pulse, "segment_id": "new", "start_s": 20, "text": "Smaller launches reduce risk."}
    assert publish(0, [replacement]).ok
    assert store.snapshot()["live_highlights"] == [replacement, later]
    assert not publish(0, [{**replacement, "segment_id": "missing"}]).ok
    assert not publish(0, [{**replacement, "kind": "unknown"}]).ok
    assert not publish(0, [replacement] * 6).ok
    assert store.snapshot()["live_highlights"] == [replacement, later]
    assert publish(0, []).ok
    assert store.snapshot()["live_highlights"] == [later]
