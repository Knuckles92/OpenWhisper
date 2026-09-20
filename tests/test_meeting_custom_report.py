"""Tailored reports: state ops, the corpus the agent reads, and the REST path.

Covers the three guarantees the feature rests on: only the host can ask and
only the worker can publish, the corpus is reachable without pasting a whole
meeting into one prompt, and a pass that fails leaves a readable failure
rather than a record stuck on ``running``.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting.custom_report import (
    ReportUnavailable,
    TranscriptCorpus,
    build_corpus_prompt,
    build_digest,
    generate_report,
    report_title,
    run_custom_report,
    start_custom_report,
)
from meeting.export.markdown import demote_headings, export_markdown
from meeting.state.custom_reports import MAX_REQUEST_CHARS, WORKER_ACTOR
from meeting.state.schema import MAX_CUSTOM_REPORTS, FinalizationState, MeetingState
from meeting.state.store import MeetingStateStore
from meeting.web.ws import WsHub
from tests.test_meeting_web_auth import GUEST_TOKEN, HOST_TOKEN, FakeRepo

PAST_ID = "m_past"


def _segment(index, start_s, text, channel="mic"):
    return {
        "id": f"sg_{index}", "meeting_id": PAST_ID, "chunk_id": 1,
        "channel": channel, "start_s": float(start_s),
        "end_s": float(start_s) + 4.0, "text": text,
        "speaker_participant_id": None, "speaker_source": "channel",
        "speaker_pinned": False,
    }


SEGMENTS = [
    _segment(1, 0, "Lets talk about the vendor renewal and pricing."),
    _segment(2, 6, "We cannot go below a twelve percent discount.", "loopback"),
    _segment(3, 14, "Priya will draft the renewal note by Friday."),
    _segment(4, 24, "Agreed, we hold at twelve and revisit the terms in Q3."),
]

MEETING = {
    "id": PAST_ID, "title": "Vendor renewal",
    "started_at": "2026-09-19T10:00:00Z", "cloud_enabled": True,
}


def _ended_state(**fields):
    state = MeetingState(meeting_id=PAST_ID, title="Vendor renewal",
                         status="ended", cloud_enabled=True, **fields)
    state.finalization = FinalizationState.coerce(
        {"status": "completed"}, cloud_enabled=True, meeting_status="ended",
    )
    return state


def _store(**fields):
    return MeetingStateStore(_ended_state(**fields))


def _request(store, actor="host", **overrides):
    op = {"op": "request_custom_report", "request": "the commitments",
          "run_id": "run_1", **overrides}
    return store.apply(actor, None, [op])[0]


class TestOps:
    def test_host_asks_and_only_the_worker_publishes(self):
        store = _store()
        claim = _request(store)
        assert claim.ok and claim.effect["entity"] == "custom_report"
        report_id = claim.target_id
        assert store.snapshot()["custom_reports"][0]["status"] == "running"

        finish = {"op": "finish_custom_report", "report_id": report_id,
                  "run_id": "run_1", "status": "ready", "markdown": "# R\n\nbody"}
        # Neither a guest nor the agent may write report text.
        assert store.apply("user", "p_1", [finish])[0].reason == "system_only"
        assert store.apply("agent", "agent", [finish])[0].reason == "agent_forbidden"
        assert store.apply("system", "other", [finish])[0].reason == "system_only"

        published = store.apply("system", WORKER_ACTOR, [finish])[0]
        assert published.ok
        report = store.snapshot()["custom_reports"][0]
        assert report["status"] == "ready" and report["markdown"].startswith("# R")

    def test_guests_cannot_ask_and_the_agent_cannot_either(self):
        store = _store()
        assert _request(store, actor="user").reason == "host_only"
        assert _request(store, actor="agent").reason == "agent_forbidden"
        assert store.snapshot()["custom_reports"] == []

    @pytest.mark.parametrize("overrides,reason", [
        ({"request": ""}, "invalid_request"),
        ({"request": "   "}, "invalid_request"),
        ({"request": 42}, "invalid_request"),
        ({"request": "x" * (MAX_REQUEST_CHARS + 1)}, "request_too_long"),
        ({"run_id": ""}, "invalid_run_id"),
    ])
    def test_request_validation(self, overrides, reason):
        assert _request(_store(), **overrides).reason == reason

    def test_reports_wait_for_a_finished_meeting(self):
        live = MeetingStateStore(MeetingState(meeting_id=PAST_ID, status="active"))
        assert _request(live).reason == "meeting_not_ready"

        state = _ended_state()
        state.finalization = FinalizationState.coerce(
            {"status": "running"}, cloud_enabled=True, meeting_status="ended",
        )
        assert _request(MeetingStateStore(state)).reason == "meeting_not_ready"

    def test_only_one_report_runs_at_a_time(self):
        store = _store()
        assert _request(store).ok
        assert _request(store, run_id="run_2").reason == "report_running"

    def test_the_cap_bounds_what_one_meeting_stores(self):
        store = _store()
        for index in range(MAX_CUSTOM_REPORTS):
            claim = _request(store, run_id=f"run_{index}")
            assert claim.ok
            store.apply("system", WORKER_ACTOR, [{
                "op": "finish_custom_report", "report_id": claim.target_id,
                "run_id": f"run_{index}", "status": "ready", "markdown": "# x",
            }])
        assert _request(store, run_id="run_last").reason == "report_limit_reached"

    def test_a_stale_run_cannot_overwrite_a_finished_report(self):
        store = _store()
        report_id = _request(store).target_id
        finish = {"op": "finish_custom_report", "report_id": report_id,
                  "run_id": "run_1", "status": "ready", "markdown": "# first"}
        assert store.apply("system", WORKER_ACTOR, [finish])[0].ok
        # Same id, same run, already finished.
        assert store.apply("system", WORKER_ACTOR, [finish])[0].reason == "stale_report"
        # Same id, a different run that lost the race.
        assert store.apply("system", WORKER_ACTOR, [
            {**finish, "run_id": "run_2", "markdown": "# second"},
        ])[0].reason == "stale_report"
        assert store.snapshot()["custom_reports"][0]["markdown"] == "# first"

    @pytest.mark.parametrize("overrides,reason", [
        ({"status": "running"}, "invalid_report_status"),
        ({"status": "bogus"}, "invalid_report_status"),
        ({"status": "ready", "markdown": "   "}, "empty_report"),
        ({"report_id": "rep_missing"}, "unknown_report"),
    ])
    def test_finish_validation(self, overrides, reason):
        store = _store()
        report_id = _request(store).target_id
        result = store.apply("system", WORKER_ACTOR, [{
            "op": "finish_custom_report", "report_id": report_id,
            "run_id": "run_1", "status": "ready", "markdown": "# x", **overrides,
        }])[0]
        assert result.reason == reason

    def test_a_failed_report_keeps_its_message_and_frees_the_slot(self):
        store = _store()
        report_id = _request(store).target_id
        assert store.apply("system", WORKER_ACTOR, [{
            "op": "finish_custom_report", "report_id": report_id,
            "run_id": "run_1", "status": "failed", "message": "No API key.",
        }])[0].ok
        report = store.snapshot()["custom_reports"][0]
        assert report["status"] == "failed" and report["message"] == "No API key."
        assert _request(store, run_id="run_2").ok

    def test_discarding_a_running_report_unblocks_the_meeting(self):
        """The escape hatch for a run stranded by a crash."""
        store = _store()
        report_id = _request(store).target_id
        removal = store.apply("host", None, [
            {"op": "remove_custom_report", "report_id": report_id},
        ])[0]
        assert removal.ok and removal.effect["removed"] is True
        assert store.snapshot()["custom_reports"] == []
        # The abandoned worker's finish lands on nothing, and a retry works.
        assert store.apply("system", WORKER_ACTOR, [{
            "op": "finish_custom_report", "report_id": report_id,
            "run_id": "run_1", "status": "ready", "markdown": "# late",
        }])[0].reason == "unknown_report"
        assert _request(store, run_id="run_2").ok

    def test_guests_cannot_delete_a_report(self):
        store = _store()
        report_id = _request(store).target_id
        assert store.apply("user", "p_1", [
            {"op": "remove_custom_report", "report_id": report_id},
        ])[0].reason == "host_only"

    def test_reports_round_trip_through_the_serialization_contract(self):
        store = _store()
        report_id = _request(store).target_id
        store.apply("system", WORKER_ACTOR, [{
            "op": "finish_custom_report", "report_id": report_id,
            "run_id": "run_1", "status": "ready", "markdown": "# R",
            "title": "R", "sources": {"transcript_lines": 4},
        }])
        snapshot = store.snapshot()
        assert MeetingState.from_dict(snapshot).to_dict() == snapshot
        # A legacy snapshot without the key restores as an empty list.
        legacy = {k: v for k, v in snapshot.items() if k != "custom_reports"}
        assert MeetingState.from_dict(legacy).to_dict()["custom_reports"] == []


class TestCorpus:
    def test_lines_carry_the_clock_and_the_speaker(self):
        corpus = TranscriptCorpus(SEGMENTS, {})
        assert corpus.lines[0].startswith("[0:00] Me: Lets talk")
        assert corpus.lines[1].startswith("[0:06] Others: We cannot")
        assert corpus.duration_s == 28.0
        assert len(corpus) == 4

    def test_named_participants_replace_the_channel_label(self):
        participants = {"p_1": {"display_name": "Priya"}}
        rows = [dict(SEGMENTS[0], speaker_participant_id="p_1")]
        assert "Priya:" in TranscriptCorpus(rows, participants).lines[0]

    def test_empty_lines_are_dropped(self):
        rows = SEGMENTS + [_segment(9, 40, "   ")]
        assert len(TranscriptCorpus(rows, {})) == len(SEGMENTS)

    def test_search_ranks_by_overlap_and_returns_time_order(self):
        corpus = TranscriptCorpus(SEGMENTS, {})
        hits = corpus.search("renewal terms")
        assert "[0:00]" in hits and "[0:24]" in hits
        assert hits.index("[0:00]") < hits.index("[0:24]")
        assert "No transcript lines match" in corpus.search("submarine")
        assert corpus.search("  ") == "Provide search terms."

    def test_a_query_of_only_stop_words_asks_for_real_terms(self):
        """Better than "no matches": it tells the model to re-query."""
        assert TranscriptCorpus(SEGMENTS, {}).search("the and") == "Provide search terms."

    def test_window_reads_a_stretch_and_reports_an_empty_one(self):
        corpus = TranscriptCorpus(SEGMENTS, {})
        window = corpus.window(6, 15)
        assert "twelve percent" in window and "Q3" not in window
        # Reversed bounds are a typo, not an error.
        assert corpus.window(15, 6) == window
        assert "No transcript between" in corpus.window(500, 600)

    def test_a_long_transcript_keeps_its_edges_and_marks_the_gap(self):
        rows = [_segment(i, i * 5, f"line number {i} about budget") for i in range(600)]
        corpus = TranscriptCorpus(rows, {})
        edges = corpus.render_edges(edge_chars=400)
        assert "line number 0 " in edges
        assert "line number 599 " in edges
        assert "lines from" in edges and "search_transcript" in edges
        assert len(edges) < corpus.total_chars

    def test_the_digest_carries_qualifiers_the_report_must_not_drop(self):
        state = _ended_state()
        state.rolling_summary = "The team held pricing at twelve percent."
        state.topic.current = "Vendor renewal"
        snapshot = state.to_dict()
        snapshot["participants"] = {"p_1": {"id": "p_1", "display_name": "Priya"}}
        snapshot["cards"]["action_items"] = [{
            "id": "it_1", "card": "action_items", "text": "Draft the renewal note",
            "status": "proposed", "evidence": [],
            "data": {"owner_participant_id": "p_1", "deadline": "Friday"},
            "review": {"state": "provisional"},
        }]
        snapshot["cards"]["decisions"] = [{
            "id": "it_2", "card": "decisions", "text": "Removed decision",
            "status": "removed", "data": {}, "evidence": [],
        }]
        snapshot["questions"] = [
            {"id": "q_1", "text": "What is the term length?", "status": "open"},
            {"id": "q_2", "text": "Who signs?", "status": "resolved",
             "answer": "Legal does."},
        ]
        digest = build_digest(MEETING, snapshot, TranscriptCorpus(SEGMENTS, {}))
        assert "Vendor renewal" in digest
        assert "Priya" in digest
        assert "owner: Priya" in digest and "due: Friday" in digest
        assert "provisional" in digest
        assert "What is the term length?" in digest
        assert "Legal does." in digest
        assert "Removed decision" not in digest

    def test_the_prompt_pastes_a_short_transcript_whole(self):
        prompt = build_corpus_prompt(
            MEETING, _ended_state().to_dict(), TranscriptCorpus(SEGMENTS, {}),
            "every commitment we made",
        )
        assert "every commitment we made" in prompt
        assert "FULL TRANSCRIPT (complete)" in prompt
        assert "twelve percent discount" in prompt
        assert "The whole transcript is above." in prompt

    def test_the_prompt_points_a_long_meeting_at_the_read_tools(self):
        rows = [_segment(i, i * 5, f"line number {i} about the budget review") for i in range(4000)]
        prompt = build_corpus_prompt(
            MEETING, _ended_state().to_dict(), TranscriptCorpus(rows, {}), "a brief",
        )
        assert "use search_transcript and read_transcript" in prompt.lower()
        assert "FULL TRANSCRIPT (complete)" not in prompt

    def test_an_empty_meeting_says_so_instead_of_inviting_invention(self):
        prompt = build_corpus_prompt(
            MEETING, _ended_state().to_dict(), TranscriptCorpus([], {}), "a brief",
        )
        assert "no transcript text" in prompt.lower()

    @pytest.mark.parametrize("markdown,expected", [
        ("# Pricing brief\n\nbody", "Pricing brief"),
        ("\n\n#  Spaced  \n", "Spaced"),
        ("no heading here", "summarize the pricing"),
    ])
    def test_the_title_comes_from_the_document_then_the_request(self, markdown, expected):
        assert report_title(markdown, "summarize the pricing") == expected


class FakeFunction(SimpleNamespace):
    pass


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=FakeFunction(name=name, arguments=json.dumps(arguments)),
    )


class FakeClient:
    def with_options(self, **_):
        return self


def _install_generate(monkeypatch, turns):
    """Replay scripted model turns, recording the messages each one saw."""
    seen = []
    import services.text_generation as text_generation

    def fake_generate(client, profile, *, model, messages, tools=None, **kwargs):
        seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        return turns[min(len(seen) - 1, len(turns) - 1)]

    monkeypatch.setattr(text_generation, "generate", fake_generate)
    return seen


def _turn(text="", calls=()):
    return SimpleNamespace(
        text=text, tool_calls=list(calls),
        assistant_message={"role": "assistant", "content": text},
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="stop",
    )


class TestGeneration:
    def _run(self, monkeypatch, turns, request="every commitment", **kwargs):
        seen = _install_generate(monkeypatch, turns)
        result = generate_report(
            MEETING, _ended_state().to_dict(), SEGMENTS, request,
            provider="openrouter", model="test-model",
            client=FakeClient(), profile=SimpleNamespace(id="openrouter", kind="openrouter"),
            **kwargs,
        )
        return result, seen

    def test_one_turn_becomes_the_report(self, monkeypatch):
        result, seen = self._run(monkeypatch, [_turn("# Commitments\n\n- Hold at 12%")])
        assert result["markdown"] == "# Commitments\n\n- Hold at 12%"
        assert result["title"] == "Commitments"
        assert result["usage"]["total_tokens"] == 15
        assert result["sources"]["transcript_lines"] == 4
        assert result["sources"]["transcript_complete"] is True
        assert len(seen) == 1
        assert "every commitment" in seen[0]["messages"][1]["content"]

    def test_read_tools_run_and_their_results_come_back(self, monkeypatch):
        turns = [
            _turn("", [_call("c1", "search_transcript", {"query": "discount"}),
                       _call("c2", "read_transcript", {"start_s": 0, "end_s": 10})]),
            _turn("# Brief\n\nHeld at twelve."),
        ]
        result, seen = self._run(monkeypatch, turns)
        assert result["markdown"].startswith("# Brief")
        tool_messages = [m for m in seen[1]["messages"] if m.get("role") == "tool"]
        assert len(tool_messages) == 2
        assert "twelve percent discount" in tool_messages[0]["content"]
        assert "[0:06]" in tool_messages[1]["content"]
        assert result["sources"]["tools_used"] == {
            "read_transcript": 1, "search_transcript": 1,
        }

    def test_a_failing_tool_does_not_sink_the_report(self, monkeypatch):
        turns = [
            _turn("", [_call("c1", "search_transcript", {"query": "x"}),
                       _call("c2", "nonexistent_tool", {}),
                       SimpleNamespace(id="c3", function=FakeFunction(
                           name="read_transcript", arguments="{not json"))]),
            _turn("# Brief\n\nbody"),
        ]
        result, seen = self._run(monkeypatch, turns)
        assert result["markdown"].startswith("# Brief")
        contents = [m["content"] for m in seen[1]["messages"] if m.get("role") == "tool"]
        assert any("Unknown tool" in text for text in contents)
        assert any("Could not parse" in text for text in contents)

    def test_the_last_round_takes_the_tools_away_and_asks_for_the_report(self, monkeypatch):
        turns = [_turn("# Partial\n\nbody", [_call("c1", "search_transcript", {"query": "x"})])]
        result, seen = self._run(monkeypatch, turns)
        assert result["markdown"].startswith("# Partial")
        assert seen[-1]["tools"] is None
        assert "Stop reading and write the report now" in seen[-1]["messages"][-1]["content"]

    def test_a_wrapping_code_fence_is_stripped(self, monkeypatch):
        result, _ = self._run(monkeypatch, [_turn("```markdown\n# Brief\n\nbody\n```")])
        assert result["markdown"] == "# Brief\n\nbody"

    def test_an_empty_answer_is_reported_not_stored(self, monkeypatch):
        with pytest.raises(ReportUnavailable, match="came back empty"):
            self._run(monkeypatch, [_turn("   ")])

    def test_a_transport_failure_becomes_a_readable_message(self, monkeypatch):
        import services.text_generation as text_generation

        def boom(*_args, **_kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(text_generation, "generate", boom)
        with pytest.raises(ReportUnavailable, match="could not be reached"):
            generate_report(
                MEETING, _ended_state().to_dict(), SEGMENTS, "a brief",
                provider="openrouter", model="m", client=FakeClient(),
                profile=SimpleNamespace(id="openrouter", kind="openrouter"),
            )

    @pytest.mark.parametrize("request_text", ["", "   ", "x" * (MAX_REQUEST_CHARS + 1)])
    def test_the_request_is_validated_before_any_model_call(self, request_text):
        with pytest.raises(ReportUnavailable):
            generate_report(MEETING, _ended_state().to_dict(), SEGMENTS,
                            request_text, provider="openrouter", model="m",
                            client=FakeClient(), profile=SimpleNamespace(kind="openrouter"))

    def test_past_meeting_recall_is_offered_only_when_it_is_enabled(self, monkeypatch):
        import meeting.custom_report as module

        monkeypatch.setattr(module, "_past_recall_enabled", lambda: False)
        monkeypatch.setattr(module, "_context_files_enabled", lambda: False)
        result, seen = self._run(monkeypatch, [_turn("# Brief\n\nbody")], repository=object())
        names = {tool["function"]["name"] for tool in seen[0]["tools"]}
        assert names == {"search_transcript", "read_transcript"}
        assert result["sources"]["past_meetings"] is False

        monkeypatch.setattr(module, "_past_recall_enabled", lambda: True)
        monkeypatch.setattr(module, "_context_files_enabled", lambda: True)
        result, seen = self._run(monkeypatch, [_turn("# Brief\n\nbody")], repository=object())
        names = {tool["function"]["name"] for tool in seen[0]["tools"]}
        assert "search_past_meetings" in names and "search_context_files" in names
        assert result["sources"]["past_meetings"] is True


class TestLifecycle:
    class Repo:
        def get_meeting(self, meeting_id):
            return dict(MEETING, id=meeting_id)

        def get_segments(self, meeting_id, **_):
            return list(SEGMENTS)

    def test_a_successful_run_publishes_through_the_store(self):
        store = _store()
        captured = {}

        def generator(meeting, snapshot, segments, request, **kwargs):
            captured.update(request=request, segments=len(segments),
                            meeting=meeting["id"], provider=kwargs["provider"])
            return {"markdown": "# Brief\n\nbody", "title": "Brief",
                    "sources": {"transcript_lines": len(segments)}}

        result = start_custom_report(
            store, self.Repo(), "every commitment", provider="openrouter",
            model="m", generator=generator, run_in_thread=False,
        )
        assert result["ok"]
        assert captured == {"request": "every commitment", "segments": 4,
                            "meeting": PAST_ID, "provider": "openrouter"}
        report = store.snapshot()["custom_reports"][0]
        assert report["status"] == "ready" and report["title"] == "Brief"

    def test_a_rejected_claim_never_reaches_the_model(self):
        store = MeetingStateStore(MeetingState(meeting_id=PAST_ID, status="active"))
        called = []
        result = start_custom_report(
            store, self.Repo(), "a brief", provider="openrouter", model="m",
            generator=lambda *a, **k: called.append(1), run_in_thread=False,
        )
        assert result == {"ok": False, "report_id": None, "error": "meeting_not_ready"}
        assert called == []

    @pytest.mark.parametrize("error,expected", [
        (ReportUnavailable("No API key is configured."), "No API key is configured."),
        (RuntimeError("boom"), "The report could not be written."),
    ])
    def test_a_failing_pass_leaves_a_readable_failure_not_a_stuck_record(
        self, error, expected,
    ):
        store = _store()

        def generator(*_args, **_kwargs):
            raise error

        start_custom_report(store, self.Repo(), "a brief", provider="openrouter",
                            model="m", generator=generator, run_in_thread=False)
        report = store.snapshot()["custom_reports"][0]
        assert report["status"] == "failed"
        assert expected in report["message"]
        # The slot is free again.
        assert _request(store, run_id="run_after").ok

    def test_a_run_whose_report_was_discarded_publishes_nothing(self):
        store = _store()
        report_id = _request(store).target_id
        store.apply("host", None, [{"op": "remove_custom_report", "report_id": report_id}])
        run_custom_report(store, self.Repo(), report_id, "run_1", "a brief",
                          provider="openrouter", model="m",
                          generator=lambda *a, **k: {"markdown": "# late"})
        assert store.snapshot()["custom_reports"] == []

    def test_the_worker_runs_off_the_caller_thread(self):
        import threading

        store = _store()
        done = threading.Event()
        worker = {}

        def generator(*_args, **_kwargs):
            worker["thread"] = threading.current_thread().name
            done.set()
            return {"markdown": "# Brief", "title": "Brief", "sources": {}}

        assert start_custom_report(store, self.Repo(), "a brief",
                                   provider="openrouter", model="m",
                                   generator=generator)["ok"]
        assert done.wait(timeout=5)
        assert worker["thread"] == "meeting-custom-report"


class TestExport:
    def test_finished_reports_join_the_markdown_export(self):
        store = _store()
        for index, (status, markdown) in enumerate([
            ("ready", "# Commitments\n\n## Owners\n\n- Priya by Friday"),
            ("failed", ""),
        ]):
            report_id = _request(store, run_id=f"run_{index}").target_id
            store.apply("system", WORKER_ACTOR, [{
                "op": "finish_custom_report", "report_id": report_id,
                "run_id": f"run_{index}", "status": status,
                "markdown": markdown, "title": "Commitments" if markdown else "",
                "message": "" if markdown else "failed",
            }])
        document = export_markdown(MEETING, store.snapshot(), SEGMENTS)
        assert "## Requested Reports" in document
        assert "### Commitments" in document
        assert "> Requested: the commitments" in document
        # The report's own headings nest under the section instead of colliding.
        assert "##### Owners" in document
        # Its opening `# Commitments` is dropped: the section heading says it.
        assert document.count("Commitments") == 1
        assert "#### Commitments" not in document

    def test_a_meeting_without_reports_gains_no_section(self):
        document = export_markdown(MEETING, _ended_state().to_dict(), SEGMENTS)
        assert "Requested Reports" not in document

    def test_heading_demotion_skips_fenced_code_and_caps_at_six(self):
        source = "# a\n\n```\n# not a heading\n```\n\n###### deep"
        demoted = demote_headings(source)
        assert demoted.startswith("#### a")
        assert "# not a heading" in demoted
        assert demoted.endswith("###### deep")


class ReportRepo(FakeRepo):
    """A repository holding one ended meeting alongside the active one."""

    leave_running = False

    def __init__(self):
        super().__init__()
        self.past = {
            **MEETING, "status": "ended", "ended_at": "2026-09-19T11:00:00Z",
            "paused_total_s": 0.0, "asr_model": "base",
            "host_token": HOST_TOKEN, "guest_token": GUEST_TOKEN,
            "agent_provider": "openrouter", "agent_model": "test-model",
            "state_json": json.dumps(_ended_state().to_dict()),
        }
        self._meetings = [self._meeting, self.past]

    def get_meeting(self, meeting_id):
        if meeting_id == PAST_ID:
            return dict(self.past)
        return super().get_meeting(meeting_id)

    def segment_exists(self, meeting_id, segment_id):
        return any(row["id"] == segment_id for row in SEGMENTS)

    def on_ops_applied(self, meeting_id, state, applied, actor_type, actor_id):
        self.past["state_json"] = json.dumps(state)

    def persist_state(self, meeting_id, state):
        self.past["state_json"] = json.dumps(state)


@pytest.fixture
def api_client(monkeypatch):
    from meeting.web import api as api_module
    from meeting.web.api import create_app
    from tests.test_meeting_web_auth import FakeEngine

    started = []

    def fake_start(store, repository, text, **kwargs):
        started.append({"text": text, **kwargs})
        if repository.leave_running:
            # Claim the slot and leave it held, standing in for a worker
            # that is still reading the meeting.
            return store.apply("host", None, [{
                "op": "request_custom_report", "request": text,
                "run_id": "run_held",
            }]) and {"ok": True, "report_id": "held", "error": None}
        return store and start_custom_report(
            store, repository, text, provider=kwargs["provider"],
            model=kwargs["model"], run_in_thread=False,
            generator=lambda *a, **k: {
                "markdown": "# Brief\n\nHeld at twelve.", "title": "Brief",
                "sources": {"transcript_lines": 4},
            },
        )

    monkeypatch.setattr(api_module, "start_custom_report", fake_start)
    engine, repo = FakeEngine(), ReportRepo()
    with TestClient(create_app(engine, repo, WsHub(engine, repo))) as client:
        yield client, repo, started, engine


class TestApi:
    URL = f"/api/meetings/{PAST_ID}/reports"

    def test_a_host_request_writes_a_report_and_returns_the_state(self, api_client):
        client, _repo, started, _engine = api_client
        response = client.post(self.URL, params={"token": HOST_TOKEN},
                               json={"request": "every commitment"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] and body["report_id"]
        report = body["state"]["custom_reports"][0]
        assert report["status"] == "ready"
        assert report["markdown"].startswith("# Brief")
        # The meeting's own recorded endpoint is what the report runs against.
        assert started[0]["provider"] == "openrouter"
        assert started[0]["model"] == "test-model"

    def test_only_the_host_may_ask(self, api_client):
        client, _repo, started, _engine = api_client
        assert client.post(self.URL, json={"request": "x"}).status_code == 401
        assert client.post(self.URL, params={"token": GUEST_TOKEN},
                           json={"request": "x"}).status_code == 403
        assert started == []

    @pytest.mark.parametrize("payload", [
        {"request": ""}, {"request": "   "}, {"request": 42},
        {"request": "x" * (MAX_REQUEST_CHARS + 1)}, {},
    ])
    def test_bad_requests_are_refused_before_any_work(self, api_client, payload):
        client, _repo, started, _engine = api_client
        response = client.post(self.URL, params={"token": HOST_TOKEN}, json=payload)
        assert response.status_code == 400
        assert started == []

    def test_an_unknown_meeting_is_a_404(self, api_client):
        client, _repo, _started, _engine = api_client
        response = client.post("/api/meetings/m_nope/reports",
                               params={"token": HOST_TOKEN}, json={"request": "x"})
        assert response.status_code == 404

    def test_a_second_request_while_one_runs_is_refused_in_plain_words(self, api_client):
        client, repo, _started, _engine = api_client
        state = json.loads(repo.past["state_json"])
        state["custom_reports"] = [{
            "id": "rep_live", "request": "x", "status": "running",
            "run_id": "run_1", "markdown": "", "title": "", "message": "",
            "sources": {}, "requested_by": None,
            "created_at": "2026-09-19T11:00:00Z",
            "updated_at": "2026-09-19T11:00:00Z",
        }]
        repo.past["state_json"] = json.dumps(state)
        response = client.post(self.URL, params={"token": HOST_TOKEN},
                               json={"request": "another"})
        # The stored snapshot surfaces the interrupted run as failed, so the
        # slot is free; what is refused is a genuinely concurrent request.
        assert response.status_code in (200, 409)
        if response.status_code == 409:
            assert "already being written" in response.json()["detail"]

    def test_an_interrupted_run_is_shown_as_failed_not_still_running(self, api_client):
        client, repo, _started, _engine = api_client
        state = json.loads(repo.past["state_json"])
        state["custom_reports"] = [{
            "id": "rep_dead", "request": "x", "status": "running",
            "run_id": "run_1", "markdown": "", "title": "", "message": "",
            "sources": {}, "requested_by": None,
            "created_at": "2026-09-19T11:00:00Z",
            "updated_at": "2026-09-19T11:00:00Z",
        }]
        repo.past["state_json"] = json.dumps(state)
        detail = client.get(f"/api/meetings/{PAST_ID}", params={"token": HOST_TOKEN})
        report = detail.json()["state"]["custom_reports"][0]
        assert report["status"] == "failed"
        assert "interrupted" in report["message"]

    def test_delete_removes_a_report_and_reports_unknown_ids(self, api_client):
        client, _repo, _started, _engine = api_client
        created = client.post(self.URL, params={"token": HOST_TOKEN},
                              json={"request": "every commitment"}).json()
        report_id = created["report_id"]
        url = f"{self.URL}/{report_id}"
        assert client.delete(url).status_code == 401
        assert client.delete(url, params={"token": GUEST_TOKEN}).status_code == 403
        response = client.delete(url, params={"token": HOST_TOKEN})
        assert response.status_code == 200
        assert response.json()["state"]["custom_reports"] == []
        assert client.delete(url, params={"token": HOST_TOKEN}).status_code == 404

    def test_download_serves_the_markdown_as_a_file(self, api_client):
        client, _repo, _started, _engine = api_client
        created = client.post(self.URL, params={"token": HOST_TOKEN},
                              json={"request": "every commitment"}).json()
        url = f"{self.URL}/{created['report_id']}/download"
        assert client.get(url).status_code == 401
        response = client.get(url, params={"token": HOST_TOKEN})
        assert response.status_code == 200
        assert response.text.startswith("# Brief")
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert client.get(f"{self.URL}/rep_nope/download",
                          params={"token": HOST_TOKEN}).status_code == 404


class TestGuestScoping:
    """Reports may draw on past meetings, so a guest of *this* meeting never
    receives them — over REST, in the hello snapshot, or in the fan-out."""

    def _report_result(self):
        store = _store()
        return store.apply("host", None, [{
            "op": "request_custom_report", "request": "a brief",
            "run_id": "run_1",
        }])[0]

    def test_the_fan_out_splits_host_only_effects_onto_their_own_patch(self):
        from meeting.web.ws import WsHub

        hub = WsHub.__new__(WsHub)
        sent = []
        hub.schedule_broadcast = lambda message, host_only=False: sent.append(
            (host_only, message),
        )
        store = _store()
        item = store.apply("host", None, [{
            "op": "add_item", "card": "user_notes", "text": "a note",
        }])[0]
        report = self._report_result()
        hub._on_store_batch(2, [item, report])

        shared = [m for host_only, m in sent if not host_only]
        host_only = [m for flag, m in sent if flag]
        assert len(shared) == 1 and len(host_only) == 1
        assert [r["op"]["op"] for r in shared[0]["results"]] == ["add_item"]
        assert {r["op"]["op"] for r in host_only[0]["results"]} == {
            "add_item", "request_custom_report",
        }

    def test_a_batch_of_only_shared_effects_sends_one_patch(self):
        from meeting.web.ws import WsHub

        hub = WsHub.__new__(WsHub)
        sent = []
        hub.schedule_broadcast = lambda message, host_only=False: sent.append(
            (host_only, message),
        )
        store = _store()
        item = store.apply("host", None, [{
            "op": "add_item", "card": "user_notes", "text": "a note",
        }])[0]
        hub._on_store_batch(1, [item])
        assert [flag for flag, _ in sent] == [False]

    def test_a_batch_of_only_reports_never_reaches_guests(self):
        from meeting.web.ws import WsHub

        hub = WsHub.__new__(WsHub)
        sent = []
        hub.schedule_broadcast = lambda message, host_only=False: sent.append(
            (host_only, message),
        )
        hub._on_store_batch(1, [self._report_result()])
        assert [flag for flag, _ in sent] == [True]

    def test_the_session_endpoint_hides_reports_from_guests(self, api_client):
        client, _repo, _started, engine = api_client
        engine.store._state["custom_reports"] = [{"id": "rep_1", "markdown": "# secret"}]
        for token, visible in ((HOST_TOKEN, True), (GUEST_TOKEN, False)):
            state = client.get("/api/session", params={"token": token}).json()["state"]
            assert ("custom_reports" in state) is visible
            assert ("secret" in json.dumps(state)) is visible

    def test_the_meeting_detail_endpoint_is_host_only(self, api_client):
        client, _repo, _started, _engine = api_client
        response = client.get(f"/api/meetings/{PAST_ID}",
                              params={"token": GUEST_TOKEN})
        assert response.status_code == 403


class TestConcurrency:
    """A finalization re-run replaces the whole state document, so it must
    not run while a report worker is about to write into it."""

    # Only the insights re-run is reachable here: the speaker re-run
    # returns 400 first for missing audio consent / OpenAI key.
    @pytest.mark.parametrize("path", ["reinsights"])
    def test_a_rerun_waits_for_a_report_in_flight(self, api_client, path):
        client, repo, _started, _engine = api_client
        repo.leave_running = True
        client.post(f"/api/meetings/{PAST_ID}/reports", params={"token": HOST_TOKEN},
                    json={"request": "every commitment"})
        response = client.post(f"/api/meetings/{PAST_ID}/{path}",
                               params={"token": HOST_TOKEN})
        assert response.status_code == 409
        assert "report being written" in response.json()["detail"]

    @pytest.mark.parametrize("path", ["reinsights", "respeakers"])
    def test_a_finished_report_does_not_block_a_rerun(self, api_client, path):
        client, _repo, _started, _engine = api_client
        client.post(f"/api/meetings/{PAST_ID}/reports", params={"token": HOST_TOKEN},
                    json={"request": "every commitment"})
        response = client.post(f"/api/meetings/{PAST_ID}/{path}",
                               params={"token": HOST_TOKEN})
        assert response.status_code != 409
