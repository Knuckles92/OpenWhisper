"""Offline tests for spoken instructions to the note taker."""
from concurrent.futures import Future

from meeting.interfaces import OpResult
from meeting.voice_commands import (
    ACTOR_ID,
    VoiceCommandListener,
    build_ops,
    compile_wake_pattern,
    extract_topic,
    mentions_assistant,
    referent_rows,
)
from services.settings import DEFAULT_VOICE_COMMAND_NAMES
from services.typesafe import ChoiceAnswer

PATTERN = compile_wake_pattern(DEFAULT_VOICE_COMMAND_NAMES)


class ImmediateExecutor:
    """Runs submitted work inline so tests need no threads."""

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, **_kwargs):
        pass


class FakeStore:
    def __init__(self, cloud=True):
        self.calls = []

    def apply(self, actor_type, actor_id, ops):
        self.calls.append((actor_type, actor_id, ops))
        return [OpResult(ok=True, op=op, target_id="it_1") for op in ops]


class FakeJudge:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def choice(self, state, instructions, criteria):
        self.calls.append(state)
        return self.answer


def rows():
    return [
        {"id": "sg_1", "start_s": 100.0, "end_s": 104.0, "text": "We'll ship the beta on Tuesday."},
        {"id": "sg_2", "start_s": 104.5, "end_s": 106.0, "text": "Maya owns the release notes."},
        {"id": "sg_3", "start_s": 107.0, "end_s": 109.0, "text": "Note taker, mark that as a decision."},
    ]


class TestWakeNames:
    def test_default_names_match_as_whole_words(self):
        assert mentions_assistant("Note taker, mark that as a decision", PATTERN)
        assert mentions_assistant("hey notetaker capture that", PATTERN)
        assert mentions_assistant("OpenWhisper new topic budget", PATTERN)
        assert mentions_assistant("open-whisper new topic budget", PATTERN)
        assert mentions_assistant("Assistant, summarize.", PATTERN)

    def test_ordinary_speech_does_not_wake(self):
        assert not mentions_assistant("she had to whisper because the baby slept", PATTERN)
        assert not mentions_assistant("the assistants were helpful", PATTERN)
        assert not mentions_assistant("can you write that down for me in your notebook", PATTERN)
        assert not mentions_assistant("", PATTERN)

    def test_empty_names_disable(self):
        assert compile_wake_pattern([]) is None
        assert not mentions_assistant("note taker", None)


class TestTopicExtraction:
    def test_copies_phrase_after_lead_in(self):
        assert extract_topic("note taker new topic: hiring plan") == "hiring plan"
        assert extract_topic("assistant we're moving on to the budget now") == "the budget"
        assert extract_topic("OpenWhisper set the topic to release timeline please.") == "release timeline"

    def test_never_invents(self):
        assert extract_topic("note taker mark that as a decision") is None
        assert extract_topic("assistant new topic:") is None


class TestReferents:
    def test_takes_two_recent_segments_before_command(self):
        rs = rows()
        picked = referent_rows(rs[:2], rs[2])
        assert [r["id"] for r in picked] == ["sg_1", "sg_2"]

    def test_skips_old_and_excluded_segments(self):
        rs = rows()
        rs[0]["end_s"] = 70.0  # 37 s before the command
        picked = referent_rows(rs[:2], rs[2], exclude_ids=["sg_2"])
        assert picked == []
        picked = referent_rows(rows()[:2], rows()[2], exclude_ids=["sg_2"])
        assert [r["id"] for r in picked] == ["sg_1"]


class TestBuildOps:
    def test_decision_cites_referents_and_command(self):
        rs = rows()
        ops = build_ops("mark_decision", rs[2], rs[:2])
        assert ops == [{
            "op": "add_item", "card": "decisions",
            "text": "We'll ship the beta on Tuesday. Maya owns the release notes.",
            "evidence": ["sg_1", "sg_2", "sg_3"],
            "data": {"source": "voice_command", "command": "mark_decision", "command_segment_id": "sg_3"},
        }]

    def test_action_and_note_cards(self):
        rs = rows()
        assert build_ops("mark_action", rs[2], rs[1:2])[0]["card"] == "action_items"
        assert build_ops("note_this", rs[2], rs[1:2])[0]["card"] == "key_points"

    def test_set_topic_uses_extracted_phrase_only(self):
        cmd = {"id": "sg_9", "start_s": 1.0, "end_s": 2.0, "text": "note taker new topic: vendor review"}
        assert build_ops("set_topic", cmd, []) == [{"op": "set_topic", "text": "vendor review", "evidence": ["sg_9"]}]
        cmd["text"] = "note taker let's talk about something else"
        assert build_ops("set_topic", cmd, []) == []

    def test_nothing_to_mark_and_unsupported_commands(self):
        rs = rows()
        assert build_ops("mark_decision", rs[2], []) == []
        assert build_ops("recap", rs[2], rs[:2]) == []
        assert build_ops("fix_transcript", rs[2], rs[:2]) == []


class TestListener:
    def make(self, answer, cloud=True, store=None, judge=None):
        store = store or FakeStore()
        judge = judge or FakeJudge(answer)
        listener = VoiceCommandListener(
            store, judge, DEFAULT_VOICE_COMMAND_NAMES, cloud_enabled=lambda: cloud,
            executor=ImmediateExecutor(),
        )
        return listener, store, judge

    def test_applies_decision_as_system_actor(self):
        listener, store, judge = self.make(ChoiceAnswer("mark_decision", 0.93, {}))
        assert listener.observe(rows()) == 1
        assert len(judge.calls) == 1
        assert judge.calls[0]["segment"] == "Note taker, mark that as a decision."
        assert judge.calls[0]["previous_segments"] == [
            "We'll ship the beta on Tuesday.", "Maya owns the release notes."]
        (actor_type, actor_id, ops), = store.calls
        assert (actor_type, actor_id) == ("system", ACTOR_ID)
        assert ops[0]["card"] == "decisions" and ops[0]["evidence"] == ["sg_1", "sg_2", "sg_3"]

    def test_unnamed_segments_are_never_judged(self):
        listener, store, judge = self.make(ChoiceAnswer("mark_decision", 0.99, {}))
        assert listener.observe(rows()[:2]) == 0
        assert judge.calls == [] and store.calls == []

    def test_none_and_low_confidence_do_not_apply(self):
        for answer in (ChoiceAnswer("none", 0.99, {}), ChoiceAnswer("mark_action", 0.4, {}), None):
            listener, store, _ = self.make(answer)
            listener.observe(rows())
            assert store.calls == []

    def test_cloud_off_blocks_the_remote_call(self):
        listener, store, judge = self.make(ChoiceAnswer("mark_decision", 0.9, {}), cloud=False)
        listener.observe(rows())
        assert judge.calls == [] and store.calls == []

    def test_callback_receives_results(self):
        seen = []
        store = FakeStore()
        listener = VoiceCommandListener(
            store, FakeJudge(ChoiceAnswer("note_this", 0.8, {})), ["note taker"],
            on_applied=lambda command, results: seen.append((command, len(results))),
            executor=ImmediateExecutor(),
        )
        listener.observe(rows())
        assert seen == [("note_this", 1)]

    def test_shutdown_stops_observing(self):
        listener, store, judge = self.make(ChoiceAnswer("mark_decision", 0.9, {}))
        listener.shutdown()
        assert listener.observe(rows()) == 0
        assert judge.calls == []
