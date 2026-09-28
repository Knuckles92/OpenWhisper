"""Tests for transcript-polish prompting and polish-pass tool isolation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from meeting.agent.openrouter_direct import DirectOpenRouterAgent
from meeting.agent.prompts import build_checkpoint_user_prompt
from meeting.agent.tool_policy import (
    PASS_POLISH,
    ToolScope,
    run_tool,
    tool_result_text,
)
from meeting.interfaces import OpResult

class _Tools:
    def __init__(self) -> None:
        self.ops = []
        self.question_calls = 0

    def apply_agent_ops(self, ops):
        self.ops.extend(ops)
        return [OpResult(ok=True, op=op) for op in ops]

    def ask_question(self, text, evidence):
        self.question_calls += 1
        return OpResult(ok=True, op={"op": "ask_question"})

    def resolve_question(self, question_id, answer_text, confidence, evidence):
        self.question_calls += 1
        return OpResult(ok=True, op={"op": "resolve_question"})

def test_polish_prompt_limits_the_agent_to_transcript_text():
    prompt = build_checkpoint_user_prompt(
        {"participants": {}, "cards": {}, "questions": []},
        [{
            "id": "sg_one",
            "start_s": 1.0,
            "end_s": 2.0,
            "text": "helo world",
        }],
        is_polish=True,
    )

    assert "TRANSCRIPT POLISH PASS" in prompt
    assert "ONLY revise_segment_text" in prompt
    assert "## TRANSCRIPT BLOCK FOR CLEANUP" in prompt
    assert "## FULL MEETING TRANSCRIPT" not in prompt
    assert "Review only this block" in prompt
    assert "search_past_meetings" in prompt
    assert "search_context_files" in prompt

def test_polish_scope_rejects_state_and_question_tools():
    tools = _Tools()
    scope = ToolScope(pass_kind=PASS_POLISH)

    payload, results = run_tool(tools, "patch_state", {"ops": [
        {
            "op": "add_item",
            "card": "key_points",
            "text": "must not apply",
            "evidence": ["sg_one"],
        },
        {
            "op": "revise_segment_text",
            "segment_id": "sg_one",
            "text": "hello world",
            "evidence": ["sg_one"],
        },
    ]}, scope)
    question_payload, question_results = run_tool(tools, "ask_question", {
        "text": "must not apply",
        "evidence": ["sg_one"],
    }, scope)

    assert [op["op"] for op in tools.ops] == ["revise_segment_text"]
    assert [(r.ok, r.reason) for r in results] == [
        (False, "polish_only"), (True, None),
    ]
    assert [r["reason"] for r in payload["results"]] == ["polish_only", None]
    assert question_payload["reason"] == "polish_only"
    assert [r.ok for r in question_results] == [False]
    assert tools.question_calls == 0


def test_direct_tool_mode_tells_the_model_about_pass_rejections(monkeypatch):
    from types import SimpleNamespace

    tools = _Tools()
    agent = DirectOpenRouterAgent()
    agent._tools = tools
    ops = [
        {"op": "set_topic", "text": "must not apply", "evidence": ["sg_one"]},
        {"op": "revise_segment_text", "segment_id": "sg_one",
         "text": "hello world", "evidence": ["sg_one"]},
    ]
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
        name="patch_state", arguments=json.dumps({"ops": ops}),
    ))
    replies = iter([
        SimpleNamespace(tool_calls=[call], text="", usage=None,
                        assistant_message={"role": "assistant"}),
        SimpleNamespace(tool_calls=[], text="Done.", usage=None,
                        assistant_message={"role": "assistant"}),
    ])
    sent = []

    def generate(client, profile, **kwargs):
        sent.append([dict(m) for m in kwargs["messages"]])
        return next(replies)

    monkeypatch.setattr("meeting.agent.openrouter_direct.generate", generate)
    result = agent._run_tool_mode(
        MagicMock(), "system", "user", 10.0,
        scope=ToolScope(pass_kind=PASS_POLISH),
    )

    assert result.ok
    assert [op["op"] for op in tools.ops] == ["revise_segment_text"]
    assert [r.reason for r in result.op_results] == ["polish_only", None]
    tool_message = json.loads(sent[1][-1]["content"])
    assert tool_message["results"][0]["reason"] == "polish_only"


def test_direct_read_tool_returns_text_without_ops():
    tools = _Tools()
    tools.searches = []

    def search_past_meetings(query="", meeting_id=None, limit=10):
        tools.searches.append(query)
        return {"ok": True, "text": "past:m_old:1 excerpt", "hits": []}

    tools.search_past_meetings = search_past_meetings

    def search_context_files(query="", relative_path=None, limit=10):
        tools.folder_searches.append((query, relative_path, limit))
        return {"ok": True, "text": "file:plan.md:1 excerpt", "hits": []}

    tools.folder_searches = []
    tools.search_context_files = search_context_files
    scope = ToolScope(pass_kind=PASS_POLISH)
    content, content_results = run_tool(
        tools, "search_past_meetings", {"query": "budget"}, scope,
    )
    folder, folder_results = run_tool(
        tools, "search_context_files",
        {"query": "roadmap", "relative_path": "plan.md"}, scope,
    )
    assert tool_result_text("search_past_meetings", content) == "past:m_old:1 excerpt"
    assert tool_result_text("search_context_files", folder) == "file:plan.md:1 excerpt"
    assert content_results == folder_results == []
    assert tools.ops == []
    assert tools.searches == ["budget"]
    assert tools.folder_searches == [("roadmap", "plan.md", 10)]


def test_missing_read_tool_says_so_in_plain_text():
    payload, results = run_tool(
        _Tools(), "search_past_meetings", {"query": "budget"}, ToolScope(),
    )
    assert payload["disabled"] is True
    assert results == []
    assert tool_result_text("search_past_meetings", payload) == (
        "Past-meeting recall is not available."
    )


def test_polish_uses_longer_budget_in_both_backends(monkeypatch, tmp_path):
    from meeting.agent.pi_sidecar import PiSidecarAgent
    from meeting.interfaces import AgentResult, CheckpointPayload

    for agent, method in [
        (PiSidecarAgent(str(tmp_path)), "_run_checkpoint"),
        (DirectOpenRouterAgent(), "_run_pass"),
    ]:
        calls = []
        def run(payload, timeout, **kwargs):
            calls.append((timeout, kwargs))
            return AgentResult(ok=True)
        monkeypatch.setattr(agent, method, run)
        for polish in (False, True):
            agent.checkpoint(CheckpointPayload(
                request_id="test", state_snapshot={}, new_segments=[],
                is_polish=polish,
            ))
        assert calls[0][0] == 60.0
        assert calls[1][0] == 180.0
        if method == "_run_checkpoint":
            assert calls[1][1]["stall_s"] == 45.0
