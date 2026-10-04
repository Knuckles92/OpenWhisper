"""A human passage correction is anchored, reversible, and exported consistently."""
import json

from meeting.corrections import (correct_segment_text, guidance_prompt, occurrence_rules,
                                 segment_fingerprint, term_rules)
from meeting.export.bulk import collect_meeting_export, render_export_document
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from meeting.state.segment_ops import handle_segment_op, make_segment_handler
from meeting.interfaces import OpResult
from services.models import MeetingSegment
from tests.helpers import make_meeting, make_segment


def _op(segment_id, index, replacement, *, source="word", base="word word word"):
    return {
        "op": "add_item", "card": "user_notes", "text": f"Correct occurrence {index}",
        "evidence": [segment_id],
        "data": {"kind": "occurrence_correction", "selected_text": source,
                 "replacement": replacement, "occurrence_index": index,
                 "base_fingerprint": segment_fingerprint(base)},
    }


def test_fingerprint_is_stable_for_utf8_text():
    assert segment_fingerprint("alpha βeta 😀") == "fnv1a64:8ea367c29aed5110"


def test_scoped_corrections_are_stable_persisted_exported_and_individually_undoable(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([
        make_segment(meeting_id, "sg_one", text="word word word"),
        make_segment(meeting_id, "sg_two", start=4, end=5, text="word"),
    ])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    third = store.apply("host", "host", [_op("sg_one", 2, "third")])[0]
    first = store.apply("host", "host", [_op("sg_one", 0, "first")])[0]
    assert third.ok and first.ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["first word third", "word"]
    assert repo.get_segment(meeting_id, "sg_one")["original_text"] == "word word word"
    assert term_rules(store.snapshot()) == {}
    assert '"evidence": ["sg_one"]' in guidance_prompt(store.snapshot())
    assert "do not turn them into meeting-wide spelling rules" in guidance_prompt(store.snapshot())

    # A fresh state store reads the same persisted note state after recovery.
    recovered = MeetingState.from_dict(json.loads(repo.get_meeting(meeting_id)["state_json"]))
    assert correct_segment_text("word word word", "sg_one", term_rules(recovered.to_dict()),
                                occurrence_rules(recovered.to_dict())) == "first word third"
    entry = collect_meeting_export(repo, meeting_id)
    assert "first word third" in render_export_document([entry], "txt")
    assert "first word third" in render_export_document([entry], "markdown")
    exported = json.loads(render_export_document([entry], "json"))["meetings"][0]
    assert exported["segments"][0]["text"] == "first word third"
    assert exported["segments"][0]["original_text"] == "word word word"

    assert store.undo(first.seq, "host")[0].ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["word word third", "word"]
    assert store.undo(third.seq, "host")[0].ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["word word word", "word"]
    assert "word word word" in render_export_document([collect_meeting_export(repo, meeting_id)], "txt")


def test_invalid_anchor_rejected_and_explicit_meeting_wide_rule_remains_global(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([
        make_segment(meeting_id, "sg_one", text="word word"),
        make_segment(meeting_id, "sg_two", start=4, end=5, text="word"),
    ])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    assert not store.apply("host", "host", [_op("missing", 0, "once")])[0].ok
    assert not store.apply("host", "host", [{**_op("sg_one", 0, "once"), "evidence": []}])[0].ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["word word", "word"]
    global_note = {"op": "add_item", "card": "user_notes", "text": "Correct throughout meeting",
                   "data": {"kind": "term_correction", "selected_text": "word", "replacement": "global"}}
    added = store.apply("host", "host", [global_note])[0]
    assert added.ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["global global", "global"]
    assert store.undo(added.seq, "host")[0].ok
    assert [r["text"] for r in repo.get_segments(meeting_id)] == ["word word", "word"]


def test_new_global_rule_or_segment_revision_disables_stale_occurrence_until_base_returns(repo, db):
    meeting_id = make_meeting(repo)
    raw = "alpha beta alpha"
    repo.add_segments([make_segment(meeting_id, "sg_one", text=raw)])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    scoped = store.apply("host", "host", [_op("sg_one", 1, "fixed", source="alpha", base=raw)])[0]
    assert scoped.ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "alpha beta fixed"

    global_note = {"op": "add_item", "card": "user_notes", "text": "Correct beta everywhere",
                   "data": {"kind": "term_correction", "selected_text": "beta", "replacement": "alpha"}}
    added = store.apply("host", "host", [global_note])[0]
    assert added.ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "alpha alpha alpha"
    assert store.undo(added.seq, "host")[0].ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "alpha beta fixed"

    with db.get_session() as session:
        session.get(MeetingSegment, "sg_one").text = "alpha new beta alpha"
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "alpha new beta alpha"
    with db.get_session() as session:
        session.get(MeetingSegment, "sg_one").text = raw
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "alpha beta fixed"

    recovered = MeetingState.from_dict(json.loads(repo.get_meeting(meeting_id)["state_json"]))
    assert correct_segment_text(raw, "sg_one", term_rules(recovered.to_dict()),
                                occurrence_rules(recovered.to_dict())) == "alpha beta fixed"


def test_missing_or_invalid_base_fingerprint_is_rejected(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_one", text="word word word")])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    op = _op("sg_one", 1, "fixed")
    del op["data"]["base_fingerprint"]
    assert not store.apply("host", "host", [op])[0].ok
    op["data"]["base_fingerprint"] = "fnv1a64:wrong"
    assert not store.apply("host", "host", [op])[0].ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "word word word"


def test_segment_revision_undo_restores_raw_text_under_scoped_correction(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_one", text="word word")])
    store = MeetingStateStore(
        MeetingState(meeting_id=meeting_id), repository=repo,
        segment_exists=lambda sid: repo.segment_exists(meeting_id, sid),
        segment_handler=make_segment_handler(repo, meeting_id),
    )
    scoped = store.apply("host", "host", [_op("sg_one", 0, "fixed", base="word word")])[0]
    assert scoped.ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "fixed word"
    # A trusted system revision remains undoable while agent polish is blocked.
    revision = store.apply("system", "test", [{"op": "revise_segment_text",
        "segment_id": "sg_one", "text": "changed word", "evidence": ["sg_one"]}])[0]
    assert revision.ok
    assert repo.get_segment(meeting_id, "sg_one")["original_text"] == "changed word"
    assert store.undo(revision.seq, "host")[0].ok
    assert repo.get_segment(meeting_id, "sg_one")["original_text"] == "word word"
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "fixed word"
    assert store.undo(scoped.seq, "host")[0].ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "word word"


def test_segment_revision_preview_keeps_raw_and_displayed_text_in_sync():
    class ProjectedRepository:
        def get_segment(self, meeting_id, segment_id):
            return {"id": segment_id, "text": "fixed word", "original_text": "word word"}

    result = OpResult(ok=True, op={"op": "revise_segment_text"},
                      effect={"entity": "segment_text", "segment_id": "sg_one",
                              "text": "changed word"})
    inverse = handle_segment_op(ProjectedRepository(), "m_one", result)
    assert inverse["text"] == "word word"
    assert result.effect["segment"]["original_text"] == "changed word"


def test_agent_polish_cannot_bake_a_scoped_correction_into_raw_text(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_one", text="word word")])
    store = MeetingStateStore(
        MeetingState(meeting_id=meeting_id), repository=repo,
        segment_exists=lambda sid: repo.segment_exists(meeting_id, sid),
        segment_handler=make_segment_handler(repo, meeting_id),
    )
    scoped = store.apply("host", "host", [_op("sg_one", 0, "fixed", base="word word")])[0]
    assert scoped.ok
    polish = {"op": "revise_segment_text", "segment_id": "sg_one",
              "text": "fixed word,", "evidence": ["sg_one"]}
    blocked = store.apply("agent", "polish", [polish])[0]
    assert not blocked.ok and blocked.reason == "human_scoped_correction"
    assert repo.get_segment(meeting_id, "sg_one")["original_text"] == "word word"
    assert store.undo(scoped.seq, "host")[0].ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "word word"
    accepted = store.apply("agent", "polish", [polish])[0]
    assert accepted.ok
    assert repo.get_segment(meeting_id, "sg_one")["original_text"] == "fixed word,"


def test_selection_changed_before_save_is_rejected_without_persisting_note(repo, db):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_one", text="word word")])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    selected_before_revision = _op("sg_one", 0, "fixed", base="word word")
    with db.get_session() as session:
        session.get(MeetingSegment, "sg_one").text = "new word word"
    rejected = store.apply("host", "host", [selected_before_revision])[0]
    assert not rejected.ok and rejected.reason == "stale_selection"
    assert rejected.seq is None
    assert store.snapshot()["cards"]["user_notes"] == []
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "new word word"
    retry = store.apply("host", "host", [_op("sg_one", 0, "fixed",
        base="new word word")])[0]
    assert retry.ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "new fixed word"


def test_selection_fingerprint_uses_current_meeting_wide_corrections(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_one", text="word word")])
    store = MeetingStateStore(MeetingState(meeting_id=meeting_id), repository=repo,
                              segment_exists=lambda sid: repo.segment_exists(meeting_id, sid))
    global_note = {"op": "add_item", "card": "user_notes", "text": "Global spelling",
                   "data": {"kind": "term_correction", "selected_text": "word",
                            "replacement": "term"}}
    assert store.apply("host", "host", [global_note])[0].ok
    stale = store.apply("host", "host", [_op("sg_one", 0, "fixed",
        source="term", base="word word")])[0]
    assert stale.reason == "stale_selection"
    accepted = store.apply("host", "host", [_op("sg_one", 0, "fixed",
        source="term", base="term term")])[0]
    assert accepted.ok
    assert repo.get_segment(meeting_id, "sg_one")["text"] == "fixed term"
