"""The Omarchy bar widget uses every action and status field the app gives it."""
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from services.omarchy_controls import OmarchyControls

ROOT = Path(__file__).resolve().parents[1]
QML = (ROOT / "integrations" / "omarchy" / "OpenWhisper.qml").read_text(encoding="utf-8")


def _controls():
    ui = Mock()
    ui.overlay = SimpleNamespace(hands_free=False)
    ui.main_window.isActiveWindow.return_value = False
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=False),
        is_transcribing=lambda: False,
        is_meeting_active=lambda: False,
        hotkey_manager=SimpleNamespace(program_enabled=True),
    )
    return OmarchyControls(controller)


def _button_actions() -> set[str]:
    source = inspect.getsource(OmarchyControls.Button)
    return {"show", *re.findall(r'"(\w+)": "\w+"', source)}


def _bar_actions() -> set[str]:
    calls = re.findall(r"\btrigger\(([^)]*)\)", QML)
    return {action for call in calls for action in re.findall(r'"(\w+)"', call)}


def test_the_bar_sends_every_action_the_app_accepts():
    native = _controls()
    sent = _bar_actions()
    assert sent == _button_actions()
    assert all(native.Button(action) for action in sent)


def test_the_bar_reads_every_status_field_it_was_given():
    status = json.loads(_controls().Status())
    for field in ("recording", "transcribing", "meeting", "enabled", "window_active",
                  "hands_free", "language", "language_name"):
        assert field in status
        assert f"status.{field}" in QML, field


def test_the_widget_source_is_balanced():
    for opening, closing in ("{}", "()", "[]"):
        assert QML.count(opening) == QML.count(closing)
