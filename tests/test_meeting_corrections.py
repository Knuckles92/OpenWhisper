"""Human corrections offered from the dashboard: reversible transcript term
corrections, agent guidance, ASR priming, and the prompt-time agent review."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from meeting.agent.prompts import (
    build_checkpoint_user_prompt,
    build_notes_user_prompt,
)
from meeting.agent.scheduler import _GUIDANCE_MAX_SEGMENTS, CheckpointScheduler
from meeting.asr.revise import build_initial_prompt
from meeting.corrections import (
    correct_text,
    guidance_prompt,
    repository_term_rules,
    term_rules,
    term_rules_from_items,
    vocabulary_hint,
    vocabulary_terms,
)
import meeting.asr.engine as asr_engine
from tests.test_meeting_asr import FakeRepository, _chunk, _make_engine
from tests.test_meeting_engine import (  # noqa: F401  (fixtures)
    fakes,
    make_engine,
)
from tests.test_meeting_notes_agent import FakeAgent, FakeEngine, _seg
from tests.test_meeting_repository import make_meeting, make_segment


def _note(selected, replacement, *, kind="term_correction", text="", **overrides):
    item = {
        "id": f"it_{selected}", "card": "user_notes", "status": "edited",
        "author_type": "user", "text": text or f"Correction: {selected} -> {replacement}",
        "data": {"kind": kind, "selected_text": selected, "replacement": replacement},
    }
    item.update(overrides)
    return item


def _state(*notes):
    return {"cards": {"user_notes": list(notes)}}


class TestTermRules:
    def test_only_live_human_term_corrections_within_bounds_qualify(self):
        rules = term_rules(_state(
            _note("Entropic", "Anthropic"),
            _note("gone", "Gone", status="removed"),
            _note("agent", "Agent", author_type="agent"),
            _note("insight", "Insight", kind="agent_insight"),
            _note("x" * 121, "Long"),
            _note("blank", "   "),
            {"text": "plain user note", "status": "edited", "author_type": "user"},
        ))
        assert rules == {"entropic": "Anthropic"}

    def test_later_note_wins_and_items_helper_matches_state_helper(self):
        notes = [_note("Entropic", "Anthropic"), _note("entropic", "Acme")]
        assert term_rules(_state(*notes)) == {"entropic": "Acme"}
        assert term_rules_from_items(notes) == term_rules(_state(*notes))

    def test_empty_state_shapes(self):
        assert term_rules({}) == {}
        assert term_rules({"cards": None}) == {}
        assert term_rules({"cards": {"user_notes": [None, "junk"]}}) == {}


class TestCorrectText:
    def test_whole_word_case_insensitive_replacement(self):
        rules = {"entropic": "Anthropic"}
        assert correct_text("Entropic and ENTROPIC, entropic.", rules) == (
            "Anthropic and Anthropic, Anthropic."
        )
        assert correct_text("entropically unentropic", rules) == "entropically unentropic"

    def test_single_pass_prevents_chaining_and_prefers_longest_match(self):
        rules = {"a": "b", "b": "c", "open whisper": "OpenWhisper", "whisper": "Whisper"}
        assert correct_text("a b", rules) == "b c"
        assert correct_text("open whisper uses whisper", rules) == "OpenWhisper uses Whisper"

    def test_regex_syntax_in_terms_is_literal(self):
        assert correct_text("we use c++ (fast)", {"c++": "C++", "(fast)": "quick"}) == (
            "we use C++ quick"
        )

    def test_no_rules_or_empty_text_is_identity(self):
        assert correct_text("Entropic", {}) == "Entropic"
        assert correct_text("", {"entropic": "Anthropic"}) == ""


class TestVocabulary:
    def test_terms_are_distinct_ordered_and_bounded(self):
        rules = {"entropic": "Anthropic", "antropic": "anthropic", "cloud": "Claude"}
        assert vocabulary_terms(rules) == ["Anthropic", "Claude"]
        assert vocabulary_hint(rules) == "Anthropic, Claude."
        assert vocabulary_hint({}) == ""
        many = {str(i): f"Term{i}" for i in range(40)}
        assert len(vocabulary_terms(many)) == 12
        assert len(", ".join(vocabulary_terms({"a": "x" * 100, "b": "y" * 100}))) <= 160

    def test_initial_prompt_leads_with_vocabulary_after_truncation(self):
        prior = [{"text": "word " * 100}]
        prompt = build_initial_prompt(prior, vocabulary=["Anthropic", "Claude"])
        assert prompt.startswith("Anthropic, Claude. word")
        assert len(prompt) <= len("Anthropic, Claude. ") + 224
        assert build_initial_prompt(prior) == build_initial_prompt(prior, vocabulary=["", " "])


class TestGuidancePrompt:
    def test_includes_both_kinds_and_excludes_removed_or_agent_notes(self):
        prompt = guidance_prompt(_state(
            _note("Entropic", "Anthropic", text="Correction: Entropic -> Anthropic"),
            _note("plan", "", kind="agent_insight", text="We mean the hiring plan"),
            _note("secret", "Secret", status="removed", text="deleted-secret"),
            _note("agent", "Agent", author_type="agent", text="agent-authored"),
            {"text": "plain note", "status": "edited", "author_type": "user", "data": {}},
        ))
        assert prompt.startswith("## HUMAN MEETING GUIDANCE")
        assert "Correction: Entropic -> Anthropic" in prompt
        assert "We mean the hiring plan" in prompt
        assert "deleted-secret" not in prompt
        assert "agent-authored" not in prompt
        assert "plain note" not in prompt
        assert "do not invent" in prompt

    def test_empty_without_guidance_notes(self):
        assert guidance_prompt({}) == ""
        assert guidance_prompt(_state({"text": "plain", "status": "edited", "author_type": "user"})) == ""

    def test_agent_prompts_lead_with_guidance(self):
        state = {**_state(_note("Entropic", "Anthropic")), "participants": {}}
        for prompt in (
            build_checkpoint_user_prompt(state, []),
            build_notes_user_prompt(state, []),
        ):
            assert prompt.startswith("## HUMAN MEETING GUIDANCE")
            assert '"replacement": "Anthropic"' in prompt
        assert not build_checkpoint_user_prompt({"participants": {}}, []).startswith(
            "## HUMAN MEETING GUIDANCE"
        )


class TestRepositoryReadTimeCorrection:
    def test_segments_read_corrected_with_raw_text_preserved(self, repo):
        meeting_id = make_meeting(repo)
        repo.add_segments([
            make_segment(meeting_id, "sg_1", 1.0, 2.0, "Entropic ships Claude"),
            make_segment(meeting_id, "sg_2", 3.0, 4.0, "entropically speaking"),
        ])
        repo.persist_state(meeting_id, _state(_note("Entropic", "Anthropic")))

        rows = {row["id"]: row for row in repo.get_segments(meeting_id)}
        assert rows["sg_1"]["text"] == "Anthropic ships Claude"
        assert rows["sg_1"]["original_text"] == "Entropic ships Claude"
        assert rows["sg_2"]["text"] == "entropically speaking"
        assert repo.get_segment(meeting_id, "sg_1")["text"] == "Anthropic ships Claude"

        # Removing the correction restores the raw words: nothing was rewritten.
        repo.persist_state(meeting_id, _state(_note("Entropic", "Anthropic", status="removed")))
        assert repo.get_segment(meeting_id, "sg_1")["text"] == "Entropic ships Claude"

    def test_repository_provider_tracks_state_changes_for_recovery(self, repo):
        meeting_id = make_meeting(repo)
        provider = repository_term_rules(repo, meeting_id)
        assert provider() == {}
        repo.persist_state(meeting_id, {"seq": 2, **_state(_note("Entropic", "Anthropic"))})
        assert provider() == {"entropic": "Anthropic"}
        repo.persist_state(meeting_id, {"seq": 3, **_state()})
        assert provider() == {}
        assert repository_term_rules(object(), meeting_id)() == {}

    def test_other_meetings_are_unaffected(self, repo):
        first, second = make_meeting(repo, "m_a"), make_meeting(repo, "m_b")
        repo.add_segments([
            make_segment(first, "sg_a", text="Entropic"),
            make_segment(second, "sg_b", text="Entropic"),
        ])
        repo.persist_state(first, _state(_note("Entropic", "Anthropic")))
        assert repo.get_segment(first, "sg_a")["text"] == "Anthropic"
        assert repo.get_segment(second, "sg_b")["text"] == "Entropic"


class TestSchedulerGuidancePass:
    def test_guidance_fires_without_new_speech_and_resends_recent_transcript(self):
        agent = FakeAgent()
        engine = FakeEngine([_seg(f"sg_{i}", float(i)) for i in range(_GUIDANCE_MAX_SEGMENTS + 5)])
        scheduler = CheckpointScheduler(engine, agent, base_interval_s=60.0,
                                        min_interval_s=60.0, max_interval_s=60.0)
        # Everything is already known to the agent; a normal tick would not fire.
        scheduler._mark_sent(engine._segments)
        scheduler.start()
        try:
            scheduler.notify_guidance()
            deadline = time.monotonic() + 3.0
            while not agent.calls and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(agent.calls) == 1
            payload = agent.calls[0]
            assert not payload.is_notes and not payload.is_consolidation
            sent = [seg["id"] for seg in payload.new_segments]
            assert len(sent) == _GUIDANCE_MAX_SEGMENTS
            assert sent[-1] == f"sg_{_GUIDANCE_MAX_SEGMENTS + 4}"
            assert scheduler._guidance_pending is False
        finally:
            scheduler.stop()

    def test_failed_guidance_pass_is_retried(self):
        agent = FakeAgent(fail_times=1)
        engine = FakeEngine([_seg("sg_1", 1.0)])
        scheduler = CheckpointScheduler(engine, agent)
        scheduler._mark_sent(engine._segments)
        scheduler.notify_guidance()
        scheduler._fire()
        assert len(agent.calls) == 1
        assert scheduler._guidance_pending is True
        scheduler._retry_not_before = 0.0
        scheduler._fire()
        assert len(agent.calls) == 2
        assert scheduler._guidance_pending is False


class TestEngineHook:
    def test_user_note_ops_wake_the_agent_and_other_cards_do_not(self, make_engine, fakes):  # noqa: F811 (pytest fixtures)
        engine = make_engine(cloud_enabled=True)
        engine.start()
        scheduler = fakes.schedulers[-1]
        [added] = engine.apply_client_action("host", None, {
            "op": "add_item", "card": "user_notes",
            "text": "Correction: Entropic -> Anthropic",
            "data": {"kind": "term_correction", "selected_text": "Entropic",
                     "replacement": "Anthropic"},
        })
        assert added.ok
        assert scheduler.guidance_notices == 1
        assert engine._active_term_rules() == {"entropic": "Anthropic"}
        assert fakes.asr[-1].term_rules == engine._active_term_rules

        [other] = engine.apply_client_action("host", None, {
            "op": "add_item", "card": "key_points", "text": "Unrelated point",
        })
        assert other.ok
        assert scheduler.guidance_notices == 1

        engine.undo(added.seq, None)
        assert scheduler.guidance_notices == 2
        assert engine._active_term_rules() == {}


class TestAsrPriming:
    def _engine(self, rules):
        backend = SimpleNamespace(is_available=lambda: True, model=MagicMock(), cleanup=lambda: None)
        engine = _make_engine(FakeRepository(), backend)
        engine._term_rules = lambda: rules
        return engine

    def test_draft_prompt_is_corrected_and_primed(self, tmp_path):
        engine = self._engine({"entropic": "Anthropic"})
        engine._draft_context[("m_test", "mic")] = "Entropic said hello".split()
        assert engine._draft_prompt(_chunk(tmp_path)) == "Anthropic. Anthropic said hello"

    def test_vocabulary_alone_primes_an_empty_context(self, tmp_path):
        engine = self._engine({"entropic": "Anthropic"})
        assert engine._draft_prompt(_chunk(tmp_path)) == "Anthropic."
        engine._term_rules = None
        engine._draft_context.clear()
        assert engine._draft_prompt(_chunk(tmp_path)) is None

    def test_provider_errors_degrade_to_raw_context(self, tmp_path):
        engine = self._engine({})
        def boom():
            raise RuntimeError("store gone")
        engine._term_rules = boom
        engine._draft_context[("m_test", "mic")] = ["Entropic"]
        assert engine._draft_prompt(_chunk(tmp_path)) == "Entropic"

    def test_revise_window_primes_whisper_with_vocabulary(self):
        model = MagicMock()
        model.transcribe.return_value = ([], SimpleNamespace())
        backend = SimpleNamespace(is_available=lambda: True, model=model, cleanup=lambda: None)
        repo = FakeRepository()
        # The revise window trails the frontier by 45 s, so this row (ending
        # at 2 s) precedes the decode window and qualifies as prompt context.
        chunk = {"id": 1, "channel": "mic", "start_s": 0.0, "duration_s": 60.0,
                 "seq": 0, "file_path": "", "asr_status": "done"}
        repo.get_segments_in_range = lambda *args, **kwargs: []
        repo.get_audio_chunks = lambda meeting_id: [chunk]
        # Rows read from the repository already carry the correction.
        repo.get_segments = lambda meeting_id, after_start_s=-1.0: [
            {"channel": "mic", "end_s": 2.0, "text": "Anthropic said hello"},
        ]
        engine = _make_engine(repo, backend)
        engine._term_rules = lambda: {"entropic": "Anthropic"}
        original = asr_engine.stitch_window_audio
        asr_engine.stitch_window_audio = (
            lambda *args, **kwargs: (np.zeros(16000, dtype=np.float32), 0.0)
        )
        try:
            engine.revise_window("mic", frontier_s=60.0)
        finally:
            asr_engine.stitch_window_audio = original
        assert model.transcribe.call_args.kwargs["initial_prompt"] == (
            "Anthropic. Anthropic said hello"
        )
