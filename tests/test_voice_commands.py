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


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))
        return Future()

    def run(self):
        fn, args = self.jobs.pop(0)
        return fn(*args)


def streaming_listener(executor=None, judge=None, allowed=lambda: True):
    events, store = [], FakeStore()
    judge = judge or FakeJudge(ChoiceAnswer("note_this", .95, {}))
    listener = VoiceCommandListener(store, judge, DEFAULT_VOICE_COMMAND_NAMES,
                                    cloud_enabled=allowed, executor=executor or ImmediateExecutor(),
                                    on_feedback=events.append)
    return listener, store, judge, events


def preview(text="Assistant, note that the launch is Friday.", start=0, end=3, channel="mic"):
    return dict(channel=channel, start_s=start, end_s=end, text=text, final=False)


def test_preview_acknowledges_without_mutating_and_commit_saves_once():
    listener, store, judge, events = streaming_listener()
    p = preview()
    listener.observe_preview(p)
    assert [e["phase"] for e in events] == ["heard", "recognized"]
    assert not store.calls
    row = dict(p, id="sg_command")
    assert listener.observe([row]) == 1
    assert store.calls[0][2][0]["text"] == "the launch is Friday."
    assert store.calls[0][2][0]["evidence"] == ["sg_command"]
    assert events[-1]["phase"] == "saved"
    listener.observe([row])
    count = len(events)
    listener.observe_preview(dict(p, end_s=4))
    assert len(store.calls) == 1 and len(events) == count


def test_changed_preview_is_coalesced_while_judge_is_busy():
    executor = ManualExecutor()
    listener, store, judge, events = streaming_listener(executor)
    for end in range(3, 80):
        listener.observe_preview(preview(f"Assistant, note that the count is {end}.", end=end))
    assert len(executor.jobs) == 1
    executor.run()
    assert len(judge.calls) == 1 and "79" in judge.calls[0]["segment"]
    assert not store.calls


def test_split_wake_and_instruction_preserve_both_evidence_ids():
    listener, store, judge, events = streaming_listener()
    listener.observe([dict(preview("Assistant.", end=1), id="sg_wake")])
    assert not judge.calls and events[-1]["phase"] == "heard"
    listener.observe([dict(preview("Note that the launch is Friday.", start=1, end=3), id="sg_body")])
    op = store.calls[0][2][0]
    assert op["text"] == "the launch is Friday."
    assert op["evidence"] == ["sg_wake", "sg_body"]


def test_split_wake_never_crosses_channel_or_long_gap():
    for continuation in (preview("Note that launch is Friday", channel="loopback", start=1),
                         preview("Note that launch is Friday", start=10, end=12)):
        listener, store, judge, _ = streaming_listener()
        listener.observe([dict(preview("Assistant.", end=1), id="sg_wake")])
        listener.observe([dict(continuation, id="sg_body")])
        assert not judge.calls and not store.calls


def test_preview_wake_stub_and_continuation_are_joined():
    listener, store, judge, events = streaming_listener()
    listener.observe_preview(preview("Assistant.", end=1))
    listener.observe_preview(preview("Note that launch is Friday.", start=1, end=3))
    assert judge.calls[0]["segment"].startswith("Assistant, Note that")
    assert events[-1]["phase"] == "recognized" and not store.calls


def test_same_segment_point_is_captured_instead_of_unrelated_previous_point():
    listener, store, judge, _ = streaming_listener()
    listener.observe([dict(preview("Older unrelated discussion.", end=1), id="sg_old")])
    listener.observe([dict(preview("The launch is Friday. Assistant, capture that.", start=1, end=4), id="sg_cmd")])
    op = store.calls[0][2][0]
    assert op["text"] == "The launch is Friday"
    assert op["evidence"] == ["sg_cmd"]


def test_preview_cannot_overwrite_committed_feedback_from_a_slow_judgment():
    executor = ManualExecutor()
    listener, store, judge, events = streaming_listener(executor)
    listener.observe_preview(preview())
    listener.observe([dict(preview(), id="sg_cmd")])
    executor.run()  # Obsolete preview response must not announce recognition.
    assert not any(e["phase"] == "recognized" for e in events)
    executor.run()
    assert events[-1]["phase"] == "saved" and len(store.calls) == 1


def test_preview_consent_and_shutdown_block_network_and_mutations():
    executor = ManualExecutor()
    allowed = [True]
    listener, store, judge, events = streaming_listener(executor, allowed=lambda: allowed[0])
    listener.observe_preview(preview())
    allowed[0] = False
    executor.run()
    assert not judge.calls and events[-1]["phase"] == "unavailable"
    listener.observe_preview(preview("Assistant, note that Tuesday is cancelled.", end=4))
    listener.shutdown()
    count = len(events)
    executor.run()
    assert not judge.calls and not store.calls and len(events) == count


def test_missing_key_and_service_failure_have_visible_feedback():
    listener, store, judge, events = streaming_listener()
    listener._judge = None
    listener.observe_preview(preview())
    assert events[-1]["phase"] == "unavailable"
    listener._judge = FakeJudge(None)
    listener.observe([dict(preview(), id="sg_cmd")])
    assert events[-1]["phase"] == "error" and not store.calls


def test_committed_queue_is_bounded_without_blocking_caller():
    executor = ManualExecutor()
    listener, store, judge, events = streaming_listener(executor)
    for i in range(30):
        listener.observe([dict(preview(start=i*4, end=i*4+3), id=f"sg_{i}")])
    assert len(executor.jobs) == 16
    assert events[-1]["phase"] == "error" and not store.calls


def test_consent_revoked_during_judgment_prevents_save():
    allowed = [True]
    class RevokingJudge:
        def choice(self, *args):
            allowed[0] = False
            return ChoiceAnswer("note_this", .99)
    listener, store, _, events = streaming_listener(judge=RevokingJudge(), allowed=lambda: allowed[0])
    listener.observe([dict(preview(), id="sg_cmd")])
    assert not store.calls and not any(e["phase"] == "saved" for e in events)


def test_budget_note_from_real_meeting_uses_dictation_not_prior_praise():
    listener, store, judge, events = streaming_listener(
        judge=FakeJudge(ChoiceAnswer("note_this", .66, {})))
    transcript = [
        dict(id="sg_praise_1", channel="mic", start_s=48.074, end_s=49.114,
             text="This is gonna work."),
        dict(id="sg_praise_2", channel="mic", start_s=49.454, end_s=50.894,
             text="Insanely good."),
        dict(id="sg_wake", channel="mic", start_s=59.174, end_s=60.214,
             text="Assistant."),
        dict(id="sg_budget", channel="mic", start_s=60.874, end_s=66.074,
             text="Add a note that we need to get a thousand dollars for budget A."),
    ]
    listener.observe(transcript)
    op = store.calls[0][2][0]
    assert op["text"] == "we need to get a thousand dollars for budget A."
    assert op["evidence"] == ["sg_wake", "sg_budget"]
    assert events[-1]["phase"] == "saved"
    listener.observe(transcript)
    assert len(store.calls) == 1


def test_polite_dictation_and_split_note_preamble_preserve_the_requested_content():
    for lead in ("Add a note that", "Please add a note that", "Can you please add a note that",
                 "Could you take a note that", "Would you make a note that"):
        listener, store, _, _ = streaming_listener()
        listener.observe([dict(preview("Unrelated earlier point.", end=1), id="sg_old")])
        listener.observe([dict(preview(f"Assistant, {lead}", start=1, end=2), id="sg_wake")])
        assert not store.calls
        listener.observe([dict(preview("budget A needs a thousand dollars.", start=2, end=5), id="sg_budget")])
        op = store.calls[0][2][0]
        assert op["text"] == "budget A needs a thousand dollars."
        assert op["evidence"] == ["sg_wake", "sg_budget"]
