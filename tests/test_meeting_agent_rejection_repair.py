"""JSON fallback must repair rejected operations, not silently consume speech."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from meeting.agent.openrouter_direct import DirectOpenRouterAgent
from meeting.interfaces import OpResult


def test_json_fallback_repairs_rejected_revision_without_replaying_success(monkeypatch):
    agent = DirectOpenRouterAgent()
    agent._tools = MagicMock()
    add = dict(
        op="add_item", card="key_points", text="Budget is $500.", evidence=["sg_1"]
    )
    update = dict(
        op="update_item",
        id="it_old",
        base_revision=1,
        set={"text": "Anthropic"},
        evidence=["sg_1"],
    )
    corrected = dict(update, base_revision=2)
    agent._tools.apply_agent_ops.side_effect = [
        [
            OpResult(ok=True, op=add),
            OpResult(
                ok=False, op=update, reason="revision_mismatch", current_revision=2
            ),
        ],
        [OpResult(ok=True, op=corrected)],
    ]
    calls = []

    def generate(*args, **kwargs):
        calls.append(list(kwargs["messages"]))
        text = json.dumps({"ops": [add, update] if len(calls) == 1 else [corrected]})
        return SimpleNamespace(
            text=text,
            usage=None,
            assistant_message={"role": "assistant", "content": text},
        )

    monkeypatch.setattr("meeting.agent.openrouter_direct.generate", generate)
    result = agent._run_json_mode(MagicMock(), "system", "user", 10)
    assert result.ok
    assert len(calls) == 2
    assert "revision_mismatch" in calls[1][-1]["content"]
    assert "Do not repeat successful" in calls[1][-1]["content"]
    assert agent._tools.apply_agent_ops.call_args_list[1].args[0] == [corrected]
    assert sum(r.ok for r in result.op_results) == 2


def test_json_fallback_reports_repeated_failure(monkeypatch):
    agent = DirectOpenRouterAgent()
    agent._tools = MagicMock()
    op = dict(op="update_item", id="missing", evidence=["sg_1"])
    agent._tools.apply_agent_ops.return_value = [
        OpResult(ok=False, op=op, reason="unknown_item")
    ]
    text = json.dumps({"ops": [op]})
    generate = MagicMock(
        return_value=SimpleNamespace(
            text=text,
            usage=None,
            assistant_message={"role": "assistant", "content": text},
        )
    )
    monkeypatch.setattr("meeting.agent.openrouter_direct.generate", generate)
    result = agent._run_json_mode(MagicMock(), "system", "user", 10)
    assert not result.ok
    assert "unknown_item" in result.error
    assert generate.call_count == 3


def test_json_fallback_does_not_retry_human_protection(monkeypatch):
    agent = DirectOpenRouterAgent()
    agent._tools = MagicMock()
    op = dict(op="update_item", id="protected", evidence=["sg_1"])
    agent._tools.apply_agent_ops.return_value = [
        OpResult(ok=False, op=op, reason="human_edited")
    ]
    text = json.dumps({"ops": [op]})
    generate = MagicMock(
        return_value=SimpleNamespace(
            text=text,
            usage=None,
            assistant_message={"role": "assistant", "content": text},
        )
    )
    monkeypatch.setattr("meeting.agent.openrouter_direct.generate", generate)
    result = agent._run_json_mode(MagicMock(), "system", "user", 10)
    assert not result.ok
    assert generate.call_count == 1
