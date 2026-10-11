"""Settings → API & MCP: the live cards say plainly what each switch exposes."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.agent_mcp import events_ext
from services.automation import events
from services.settings import SettingsKey
from tests.test_mcp_host_tabs import make_view


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def bus():
    replacement = events.EventBus()
    previous = events.set_bus(replacement)
    yield replacement
    replacement.close()
    events.set_bus(previous)


@pytest.fixture(autouse=True)
def no_subscriptions(monkeypatch):
    monkeypatch.setattr(events_ext, "subscription_count", lambda: 0)


@pytest.fixture
def live(tmp_path):
    view = make_view(tmp_path)
    yield view.live
    view.close()
    view.deleteLater()
    QApplication.processEvents()


def test_mcp_events_says_the_agent_picks_the_address_and_gets_what_you_dictate(live):
    line = live.mcp_events_switch.accessibleDescription()
    assert "chooses the web address" in line
    assert "what you dictate" in line and "subscribes to" in line
    # One or two lines on the card, not a paragraph.
    assert len(line) <= 140



def test_follow_live_events_names_what_it_shares_while_on_by_default(live):
    check = live.api_permission_checks[SettingsKey.API_LIVE_EVENTS]
    assert check.isChecked()
    line = check.accessibleDescription()
    for shared in ("finished dictations", "actions", "meeting lines"):
        assert shared in line

def test_the_channel_card_says_words_a_program_sends_are_marked_and_need_permission(live):
    note = live.channel_programs.text()
    assert "Send words to my coding agents" in note
    assert "program" in note and "Claude" in note
    assert live.channel_card.isAncestorOf(live.channel_programs)