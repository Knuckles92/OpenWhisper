"""Tests for transcript-polish prompting and direct-agent tool isolation."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.agent.openrouter_direct import DirectOpenRouterAgent
from meeting.agent.prompts import build_checkpoint_user_prompt
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
    assert "## FULL MEETING TRANSCRIPT" in prompt


def test_direct_polish_mode_filters_state_and_question_tools():
    tools = _Tools()
    agent = DirectOpenRouterAgent()
    agent._tools = tools
    agent._polish_mode = True

    results = agent._dispatch_tool_call("patch_state", {"ops": [
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
    ]})
    question_results = agent._dispatch_tool_call("ask_question", {
        "text": "must not apply",
        "evidence": ["sg_one"],
    })

    assert [op["op"] for op in tools.ops] == ["revise_segment_text"]
    assert len(results) == 1
    assert question_results == []
    assert tools.question_calls == 0
