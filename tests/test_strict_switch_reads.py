"""On/off settings read the same everywhere: a stored bool, else the default.

Before typed settings, some readers took any truthy value as "on" while the
resolvers and the MCP settings tools took only a real bool. A hand-edited
``"auto_paste": "false"`` pasted, ``"meeting_cloud_consent_given": "no"``
counted as consent, and the MCP tools reported both as their defaults. Every
reader now agrees with the resolvers.
"""
from __future__ import annotations

import pytest

from services import cleanup_profiles
from services.runtime.hotkeys import HotkeyRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings


@pytest.mark.parametrize("stored, expected", [
    (True, True), (False, False), ("false", False), ("no", False), (1, False),
    (0, False), (None, False),
])
def test_cleanup_reads_its_switch_strictly(stored, expected):
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: stored}
    assert cleanup_profiles.cleanup_may_run(settings) is expected


@pytest.mark.parametrize("stored, expected", [
    (True, True), (False, False), ("false", True), (0, True), (None, True),
])
def test_auto_paste_reads_its_switch_strictly(monkeypatch, stored, expected):
    from services.runtime import hotkeys

    monkeypatch.setattr(
        hotkeys, "settings_manager", InMemorySettings({SettingsKey.AUTO_PASTE: stored}),
    )
    assert HotkeyRuntime._auto_paste_enabled() is expected


def test_a_malformed_consent_is_not_consent(monkeypatch):
    from services.runtime import meeting as runtime_meeting

    monkeypatch.setattr(
        runtime_meeting, "settings_manager",
        InMemorySettings({SettingsKey.MEETING_CLOUD_CONSENT_GIVEN: "no"}),
    )
    runtime = runtime_meeting.MeetingRuntime.__new__(runtime_meeting.MeetingRuntime)
    assert runtime._cloud_consent_given() is False
