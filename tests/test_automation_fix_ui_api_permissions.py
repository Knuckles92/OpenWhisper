"""Settings → API & MCP: the Live API card's fifth permission, for sending a
program's words to the user's coding agents (api_agent_prompts, off)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.agent_mcp import events_ext
from services.automation import events, permissions
from services.settings import SETTING_DEFAULTS, SettingsKey
from tests.test_mcp_host_tabs import make_view

AGENT = permissions.AGENT_PROMPTS


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
def view(tmp_path):
    view = make_view(tmp_path)
    yield view
    view.close()
    view.deleteLater()
    QApplication.processEvents()


def row_of(check):
    return check.parentWidget()


def test_every_permission_has_a_row_in_order(view):
    checks = view.live.api_permission_checks
    assert list(checks) == list(permissions.ALL)
    assert AGENT == SettingsKey.API_AGENT_PROMPTS


def test_sending_words_to_agents_is_off_by_default_and_says_what_it_needs(view):
    check = view.live.api_permission_checks[AGENT]
    assert SETTING_DEFAULTS[AGENT] is False
    assert not check.isChecked()
    assert check.accessibleName() == "Send words to my coding agents"
    description = check.accessibleDescription()
    assert "Agent hotkey" in description and "Claude Code session" in description
    assert "Needs Run actions too." in description
    texts = [label.text() for label in row_of(check).findChildren(type(view.live.notice))]
    assert "Send words to my coding agents" in texts


def test_the_switch_and_the_row_save_it(view):
    check = view.live.api_permission_checks[AGENT]
    check.click()
    assert view.settings.get(AGENT) is True
    QTest.mouseClick(row_of(check), Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
    assert view.settings.get(AGENT) is False
    assert not view.live.notice.isVisible()


def test_a_failed_save_rolls_back_and_says_so(view, monkeypatch):
    def fail(_mutator):
        raise OSError("read only")

    monkeypatch.setattr(view.settings, "mutate_settings", fail)
    check = view.live.api_permission_checks[AGENT]
    check.click()
    assert not check.isChecked()
    assert view.live.notice.text() == "Couldn't save that permission. Try again."


def test_saved_choice_is_reread(view):
    view.settings.save_setting(AGENT, True)
    view.live.refresh()
    assert view.live.api_permission_checks[AGENT].isChecked()
    view.settings.save_setting(AGENT, "yes")  # malformed: its default
    view.live.refresh()
    assert not view.live.api_permission_checks[AGENT].isChecked()
