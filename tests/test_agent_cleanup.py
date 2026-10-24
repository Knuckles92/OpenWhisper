"""Installed agents use the shared cleanup path without HTTP credentials or tools."""
import threading
from unittest.mock import Mock

import pytest

from meeting.agent.installed.drivers import PassOutcome, tool_contract
from services.installed_agents import InstalledAgent
from services.settings import (
    SettingsKey, TranscriptCleanupProvider, resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
)
from services.transcript_cleanup import CANCELED_REASON, TranscriptCleanup


@pytest.fixture
def agent_runner(monkeypatch):
    monkeypatch.setattr("services.installed_agents.resolve_agent", lambda agent_id:
                        InstalledAgent(agent_id, "agent", "9.0.0", signed_in=True))
    runner = Mock(return_value=PassOutcome(ok=True, text="Hello world."))
    monkeypatch.setattr("meeting.agent.installed.core.run_agent_task", runner)
    return runner


@pytest.mark.parametrize("agent_id", TranscriptCleanupProvider.INSTALLED)
@pytest.mark.parametrize("model", ["", "chosen-model"])
def test_agent_selection_and_cleanup(agent_runner, agent_id, model):
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: agent_id,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: model}
    assert resolve_transcript_cleanup_provider(settings) == agent_id
    assert resolve_transcript_cleanup_model(settings) == model
    cleaner = TranscriptCleanup(provider=agent_id, model=model)
    assert cleaner.client is None and cleaner.api_key is None
    assert cleaner.is_available()
    assert cleaner.cleanup("hello um world", system_prompt="Use the profile's rules.") == "Hello world."
    assert cleaner.last_error is None
    args, kwargs = agent_runner.call_args
    assert args == (agent_id,)
    assert kwargs["model"] == model
    assert kwargs["system_prompt"] == "Use the profile's rules."
    assert kwargs["user_prompt"] == "hello um world"
    assert kwargs["tools"] == []
    assert kwargs["effort"] == "low"


@pytest.mark.parametrize("outcome", [
    PassOutcome(ok=False, error="Sign in needed"),
    PassOutcome(ok=True, text="  "),
    PassOutcome(ok=False, canceled=True),
])
def test_failed_agent_preserves_raw_text(agent_runner, outcome):
    agent_runner.return_value = outcome
    cleaner = TranscriptCleanup(provider="claude_code")
    assert cleaner.cleanup("raw transcript") == "raw transcript"
    assert cleaner.last_error


def test_missing_agent_does_not_fall_back_to_api(agent_runner, monkeypatch):
    monkeypatch.setattr("services.installed_agents.resolve_agent", lambda _: None)
    cleaner = TranscriptCleanup(provider="codex")
    assert not cleaner.is_available()
    assert cleaner.cleanup("raw") == "raw"
    agent_runner.assert_not_called()


@pytest.mark.parametrize("cancel_request", [False, True])
def test_agent_is_stopped_when_cleanup_wait_ends(agent_runner, cancel_request):
    stopped = threading.Event()
    caller_cancel = threading.Event()

    def wait_for_cancel(*args, cancel_event, **kwargs):
        if cancel_request:
            caller_cancel.set()
        if cancel_event.wait(2):
            stopped.set()
        return PassOutcome(ok=False, canceled=True)

    agent_runner.side_effect = wait_for_cancel
    cleaner = TranscriptCleanup(provider="codex", cancel_event=caller_cancel)
    assert cleaner.cleanup("raw", deadline_s=0.15) == "raw"
    assert stopped.wait(1)
    if cancel_request:
        assert cleaner.last_error == CANCELED_REASON
    else:
        assert "timed out" in cleaner.last_error


def test_switching_from_api_closes_client_and_uses_agent(agent_runner):
    cleaner = TranscriptCleanup(provider="openai", defer_client=True)
    client = Mock()
    cleaner.client = client
    cleaner.configure("opencode_cli", "", "high")
    client.close.assert_called_once()
    assert cleaner.cleanup("raw") == "Hello world."
    assert agent_runner.call_args.kwargs["effort"] == "high"


def test_agent_is_ready_for_cleanup_based_rewrites(agent_runner):
    from services.text_rewrite import provider_ready

    assert provider_ready({SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "codex",
                           SettingsKey.TRANSCRIPT_CLEANUP_MODEL: ""})


def test_tool_free_contract_returns_text_instead_of_requesting_meeting_tools():
    contract = tool_contract([], "Claude Code", closing="Return only cleaned text.")
    assert "no tools" in contract
    assert "patch_state" not in contract
    assert "only by calling" not in contract
    assert contract.endswith("Return only cleaned text.")
