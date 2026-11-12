"""A detected Claude Code or Codex is offered for AI cleanup, once, and
only becomes the runner when the user says yes."""
from unittest.mock import Mock

import pytest

from services import cleanup_agent_offer as offer
from services.installed_agents import InstalledAgent
from services.settings import (
    SettingsKey, SettingsManager, resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
)

ON = {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}


def agent(agent_id, signed_in=True, problem=""):
    return InstalledAgent(agent_id, "/bin/agent", "9.0.0", signed_in=signed_in, problem=problem)


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, True)
    return manager


@pytest.mark.parametrize("settings, due", [
    (ON, True),
    ({}, False),  # cleanup is off
    ({**ON, SettingsKey.CLEANUP_AGENT_OFFER_ANSWERED: True}, False),
    # Any runner the user chose, OpenRouter included, is left alone.
    ({**ON, SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter"}, False),
    ({**ON, SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "codex"}, False),
])
def test_pending(settings, due):
    assert offer.pending(settings) is due


def test_only_signed_in_claude_code_and_codex_are_offered_claude_first():
    scan = {
        "codex": agent("codex"),
        "claude_code": agent("claude_code"),
        "opencode_cli": agent("opencode_cli"),
    }
    assert [a.id for a in offer.offerable(scan)] == ["claude_code", "codex"]
    assert offer.offerable(None) == []
    assert offer.offerable({
        "claude_code": agent("claude_code", signed_in=False),
        "codex": agent("codex", signed_in=None),
    }) == []
    assert offer.offerable({"claude_code": agent("claude_code", problem="Too old")}) == []


def test_accept_switches_cleanup_to_the_agent(store):
    offer.accept("codex", store)
    settings = store.load_all_settings()
    assert resolve_transcript_cleanup_provider(settings) == "codex"
    assert resolve_transcript_cleanup_model(settings) == ""
    assert not offer.pending(settings)
    with pytest.raises(ValueError):
        offer.accept("opencode_cli", store)


def test_decline_keeps_the_default_and_is_not_asked_again(store):
    offer.decline(store)
    settings = store.load_all_settings()
    assert resolve_transcript_cleanup_provider(settings) == "openrouter"
    assert not offer.pending(settings)


@pytest.fixture
def ui_offer(monkeypatch):
    from ui_qt import cleanup_agent_offer as ui

    state = {"pending": True}
    monkeypatch.setattr(ui.cleanup_agent_offer, "pending", lambda: state["pending"])
    monkeypatch.setattr(ui.cleanup_agent_offer, "accept", Mock())
    monkeypatch.setattr(ui.cleanup_agent_offer, "decline", Mock())
    monkeypatch.setattr(ui.agent_picker, "cached_agents",
                        lambda: {"claude_code": agent("claude_code")})
    monkeypatch.setattr(ui, "resolve_transcript_cleanup_provider", lambda: "openrouter")
    monkeypatch.setattr(ui, "ask", Mock(return_value="claude_code"))
    instance = ui.CleanupAgentOffer(None)
    instance.on_accepted = Mock()
    return ui, instance, state


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance()


def test_yes_switches_and_refreshes(ui_offer, qapp):
    ui, instance, _state = ui_offer
    instance.maybe_offer()
    ui.ask.assert_not_called()  # never inside the toggle that caused it
    qapp.processEvents()
    assert ui.ask.call_args.args[2] == "OpenRouter"
    ui.cleanup_agent_offer.accept.assert_called_once_with("claude_code")
    instance.on_accepted.assert_called_once_with()


def test_no_declines(ui_offer, qapp):
    ui, instance, _state = ui_offer
    ui.ask.return_value = None
    instance.maybe_offer()
    qapp.processEvents()
    ui.cleanup_agent_offer.decline.assert_called_once_with()
    ui.cleanup_agent_offer.accept.assert_not_called()
    instance.on_accepted.assert_not_called()


def test_nothing_is_asked_without_an_agent_or_when_not_due(ui_offer, qapp, monkeypatch):
    ui, instance, state = ui_offer
    monkeypatch.setattr(ui.agent_picker, "cached_agents", lambda: {"claude_code": None})
    instance.maybe_offer()
    qapp.processEvents()
    state["pending"] = False
    instance.maybe_offer()
    qapp.processEvents()
    ui.ask.assert_not_called()
    # Not answered: an agent installed later is still offered.
    ui.cleanup_agent_offer.decline.assert_not_called()
