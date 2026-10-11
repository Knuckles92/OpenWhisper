"""Settings → API & MCP: "Change dictated text" says what a filter program can reach.

Review round 2: filters change the words sent to actions too, not only what is
pasted, and words for coding agents also need "Send words to my coding agents".
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.agent_mcp import events_ext
from services.automation import events, permissions
from tests.test_mcp_host_tabs import make_view


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(tmp_path, monkeypatch):
    monkeypatch.setattr(events_ext, "subscription_count", lambda: 0)
    bus = events.EventBus()
    previous = events.set_bus(bus)
    view = make_view(tmp_path)
    yield view
    view.close()
    view.deleteLater()
    QApplication.processEvents()
    bus.close()
    events.set_bus(previous)


def test_change_dictated_text_names_actions_and_the_agents_switch(view):
    description = view.live.api_permission_checks[permissions.TEXT_FILTERS].accessibleDescription()
    assert "pasted or sent to an action" in description
    assert "coding agents also need the switch below" in description
