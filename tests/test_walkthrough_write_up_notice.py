"""A failed AI write-up says why where it can be read, and Settings shows which agents are missing."""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QComboBox

from services.settings import MeetingAgentCore
from services.walkthrough.write_up import AGENTS, agent_name

pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _result(error=""):
    return SimpleNamespace(name="Walkthrough", shots=1, duration_s=11, first_shot=None,
                           pdf=Path("/nonexistent/walkthrough.pdf"),
                           docx=Path("/nonexistent/walkthrough.docx"), write_up_error=error)


def test_the_ready_card_shows_the_whole_failure_reason(app):
    from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard

    card = WalkthroughReadyCard()
    reason = ("Claude Code is not installed on this computer. Choose another agent "
              "in Settings → Dictation → Walkthroughs.")
    card.present(_result(reason), copied=True)
    assert card.reason.isVisibleTo(card)
    assert card.reason.text() == f"Write-up failed: {reason}"
    assert card.reason.wordWrap()

    card.present(_result(), copied=True)
    assert not card.reason.isVisibleTo(card)
    card.hide()
    card.deleteLater()


def _page(found):
    from ui_qt.dialogs.settings_dialog import SettingsDialog

    combo = QComboBox()
    for agent in AGENTS:
        combo.addItem(agent_name(agent), agent)
    page = SimpleNamespace(walkthrough_write_up_agent_combo=combo, _write_up_scan_token=0,
                           _on_write_up_scan_done=lambda token: None)
    page.label = lambda: SettingsDialog._label_write_up_agents(page)
    return page, combo


def test_agents_the_scan_did_not_find_are_marked(app, monkeypatch):
    from ui_qt.widgets import agent_picker

    found = dict.fromkeys(AGENTS)
    found[MeetingAgentCore.CODEX] = object()
    monkeypatch.setattr(agent_picker, "cached_agents", lambda: found)
    page, combo = _page(found)
    page.label()
    labels = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert labels[MeetingAgentCore.CODEX] == "Codex"
    assert labels[MeetingAgentCore.CLAUDE_CODE] == "Claude Code (not installed)"


def test_without_a_scan_one_runs_and_labels_stay_plain(app, monkeypatch):
    from ui_qt.widgets import action_relay, agent_picker

    monkeypatch.setattr(agent_picker, "cached_agents", lambda: None)
    started = []
    monkeypatch.setattr(action_relay, "start_agent_scan", started.append)
    page, combo = _page(None)
    page.label()
    page.label()
    assert len(started) == 1
    assert combo.itemText(0) == agent_name(combo.itemData(0))
