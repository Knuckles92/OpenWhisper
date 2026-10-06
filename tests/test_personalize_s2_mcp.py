"""Agents can read and change the cleanup level, and the desktop follows live."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject

from services.agent_mcp.controls import (
    CONTROLS_BY_KEY,
    LIVE,
    AgentControls,
    ControlError,
)
from services.database import DatabaseManager
from services.settings import SettingsKey, SettingsManager, settings_manager
from ui_qt.ui_controller import UIController

LEVEL = SettingsKey.TRANSCRIPT_CLEANUP_LEVEL
PROMPT = SettingsKey.TRANSCRIPT_CLEANUP_PROMPT


@pytest.fixture
def agent(tmp_path):
    db = DatabaseManager(str(tmp_path / "history.db"))
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.update_settings({
        SettingsKey.MCP_SETTINGS_ACCESS: True,
        SettingsKey.MCP_WRITABLE_SETTINGS: {LEVEL: True},
    })
    yield AgentControls(tmp_path / "history.db", settings), settings
    db.close()


def _described(agent, key):
    return next(item for item in agent.get_settings()["settings"] if item["key"] == key)


def test_the_level_is_a_live_choice_in_the_cleanup_group(agent):
    controls, _settings = agent
    control = CONTROLS_BY_KEY[LEVEL]

    assert (control.label, control.group, control.effect) == ("Cleanup level", "Cleanup", LIVE)
    assert not control.resettable
    item = _described(controls, LEVEL)
    assert item["choices"] == ["light", "medium", "high"]
    assert item["value"] == "medium"
    assert item["writable"] is True


def test_an_agent_changes_the_level_and_bad_values_are_refused(agent):
    controls, settings = agent

    result = controls.update_settings({LEVEL: "high"})

    assert result["updated"] == {LEVEL: "high"}
    assert result["previous"] == {LEVEL: "medium"}
    assert settings.get(LEVEL) == "high"
    for bad in ("none", "HIGH", "", 2, None):
        with pytest.raises(ControlError):
            controls.update_settings({LEVEL: bad})
    assert settings.get(LEVEL) == "high"


def test_a_corrupt_saved_level_reads_as_medium(agent):
    controls, settings = agent
    settings.save_setting(LEVEL, "extreme")

    assert _described(controls, LEVEL)["value"] == "medium"


def test_agents_see_an_empty_custom_prompt_until_one_is_saved(agent):
    controls, settings = agent

    item = _described(controls, PROMPT)

    assert item["label"] == "Custom cleanup prompt"
    assert item["value"] == ""
    settings.save_setting(PROMPT, "Bullet points only.")
    assert _described(controls, PROMPT)["value"] == "Bullet points only."


def test_a_level_change_from_an_agent_refreshes_the_cleanup_controls():
    ui = UIController.__new__(UIController)
    QObject.__init__(ui)
    ui._settings_dialog = SimpleNamespace(refresh=Mock())
    ui.refresh_cleanup_controls = Mock()
    settings_manager.save_setting(LEVEL, "light")

    ui._apply_agent_changes("settings", {LEVEL: "light"})

    ui.refresh_cleanup_controls.assert_called_once_with()
    ui._settings_dialog.refresh.assert_called_once_with()
