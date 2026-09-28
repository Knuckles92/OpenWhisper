"""What the Meeting Mode tab and the consent dialog say when an agent runs AI insights."""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from services import installed_agents
from services.installed_agents import InstalledAgent
from services.settings import MeetingAgentCore, SettingsKey, settings_manager
from ui_qt.dialogs.meeting_consent_dialog import MeetingConsentDialog
from ui_qt.widgets.meeting_mode_tab import ai_insights_destination

CLAUDE = InstalledAgent("claude_code", "C:/bin/claude.exe", "2.1.281", "Claude Team", True)
SETTINGS = "Settings → Meeting Mode → Intelligence"


@pytest.fixture(autouse=True)
def _no_agent_config():
    """The user's own agent config files stay out of these tests."""
    with patch.object(installed_agents, "configured_default_model", return_value=""), \
            patch.object(installed_agents, "scan_agents",
                         side_effect=AssertionError("the tab must not scan")), \
            patch.object(installed_agents, "find_agent",
                         side_effect=AssertionError("the tab must not probe")):
        yield


def _claude(**settings):
    return {SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.CLAUDE_CODE, **settings}


class TestAiInsightsDestination:
    def test_installed_agent_names_agent_and_model(self):
        with patch.object(installed_agents, "cached_scan",
                          return_value={"claude_code": CLAUDE}):
            where, privacy = ai_insights_destination(_claude(
                **{SettingsKey.MEETING_AGENT_MODELS: {"claude_code": "haiku"}}
            ))
        assert where == "Claude Code · Haiku"
        assert privacy == "your agent receives transcript text, never audio"

    def test_before_any_scan_it_names_the_choice(self):
        with patch.object(installed_agents, "cached_scan", return_value=None):
            where, privacy = ai_insights_destination(_claude())
        assert where == "Claude Code · default model"
        assert privacy == "your agent receives transcript text, never audio"

    def test_missing_agent_points_to_settings(self):
        with patch.object(installed_agents, "cached_scan",
                          return_value={"claude_code": None}):
            where, privacy = ai_insights_destination(_claude())
        assert where == "Claude Code not found"
        assert privacy == f"choose another agent in {SETTINGS}"

    def test_unusable_agent_says_what_to_fix(self):
        old = InstalledAgent("codex", "codex", "0.100.0", problem="Too old.")
        signed_out = InstalledAgent("codex", "codex", "0.158.0", signed_in=False)
        codex = {SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.CODEX}
        with patch.object(installed_agents, "cached_scan", return_value={"codex": old}):
            where, privacy = ai_insights_destination(codex)
        assert where == "Codex needs an update"
        assert privacy.startswith("update it, or choose another agent")
        with patch.object(installed_agents, "cached_scan",
                          return_value={"codex": signed_out}):
            where, privacy = ai_insights_destination(codex)
        assert where == "Codex isn't signed in"
        assert SETTINGS in privacy

    def test_built_in_engine_is_unchanged(self):
        where, privacy = ai_insights_destination({
            SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.DIRECT,
            "meeting_llm_provider": "openrouter",
            "meeting_llm_model": "deepseek/deepseek-v4.1-flash",
        })
        assert where == "OpenRouter · deepseek-v4.1-flash"
        assert privacy == "sends transcript text, never audio"


class TestConsentForAnAgent:
    @pytest.fixture(autouse=True)
    def _qapp(self):
        return QApplication.instance() or QApplication([])

    @staticmethod
    def _body(dialog):
        return dialog.findChild(QLabel, "consentBodyLabel").text()

    def test_agent_copy_names_the_agent_not_the_endpoint(self):
        dialog = MeetingConsentDialog(
            destination="OpenRouter (openrouter.ai)", remote=True,
            agent_id=MeetingAgentCore.CLAUDE_CODE,
        )
        body = self._body(dialog)
        assert "sent to Claude Code on this computer" in body
        assert "own sign-in" in body
        assert "no files, no shell" in body
        assert "OpenRouter" not in body
        assert "AI insights do not upload audio" in body
        assert dialog.remote is True
        assert "Claude Code" in dialog.accessibleDescription()

    def test_saved_agent_is_read_from_settings(self):
        settings_manager.save_setting(SettingsKey.MEETING_AGENT_CORE, MeetingAgentCore.CODEX)
        dialog = MeetingConsentDialog(destination="OpenRouter", remote=True)
        assert dialog.agent_id == MeetingAgentCore.CODEX
        assert "sent to Codex on this computer" in self._body(dialog)

    def test_built_in_engine_keeps_the_endpoint_copy(self):
        settings_manager.save_setting(SettingsKey.MEETING_AGENT_CORE, MeetingAgentCore.DIRECT)
        dialog = MeetingConsentDialog(destination="Work gateway", remote=True)
        assert dialog.agent_id == ""
        body = self._body(dialog)
        assert "sent to Work gateway" in body
        assert "leaves this computer" in body
