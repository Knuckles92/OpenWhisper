"""Common Meeting Mode start seams must share Linux readiness decisions."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _controller():
    from ui_qt import ui_controller as mod

    ctrl = SimpleNamespace(
        main_window=SimpleNamespace(
            tabbed_content=SimpleNamespace(unlock_meeting_tab=MagicMock())
        ),
        on_meeting_start=MagicMock(),
        on_meeting_start_demo=MagicMock(),
        on_meeting_start_new=MagicMock(),
        on_meeting_end=None,
        _meeting_active=False,
        ensure_meeting_platform_ack=MagicMock(return_value=True),
        ensure_meeting_start_readiness=MagicMock(return_value="required"),
    )
    # Bind unbound methods from UIController onto the namespace.
    ctrl._on_meeting_start_requested = mod.UIController._on_meeting_start_requested.__get__(
        ctrl, type(ctrl)
    )
    ctrl._on_meeting_demo_requested = mod.UIController._on_meeting_demo_requested.__get__(
        ctrl, type(ctrl)
    )
    ctrl._on_meeting_start_new_requested = (
        mod.UIController._on_meeting_start_new_requested.__get__(ctrl, type(ctrl))
    )
    ctrl._on_tray_meeting_toggle = mod.UIController._on_tray_meeting_toggle.__get__(
        ctrl, type(ctrl)
    )
    ctrl._on_meeting_end_requested = (
        mod.UIController._on_meeting_end_requested.__get__(ctrl, type(ctrl))
    )
    return ctrl


@pytest.mark.parametrize(
    "method_name,args,target_attr",
    [
        ("_on_meeting_start_requested", (True,), "on_meeting_start"),
        ("_on_meeting_start_new_requested", (True,), "on_meeting_start_new"),
        ("_on_tray_meeting_toggle", (), "on_meeting_start"),
    ],
)
def test_start_seams_forward_ready_policy_once(qapp, method_name, args, target_attr):
    ctrl = _controller()
    ctrl.ensure_meeting_start_readiness.return_value = "required"
    getattr(ctrl, method_name)(*args)
    ctrl.ensure_meeting_start_readiness.assert_called_once()
    target = getattr(ctrl, target_attr)
    target.assert_called_once()
    assert target.call_args.kwargs.get("system_audio_policy") == "required"


@pytest.mark.parametrize(
    "method_name,args,target_attr",
    [
        ("_on_meeting_start_requested", (True,), "on_meeting_start"),
        ("_on_meeting_start_new_requested", (True,), "on_meeting_start_new"),
        ("_on_tray_meeting_toggle", (), "on_meeting_start"),
    ],
)
def test_start_seams_cancel_starts_nothing(qapp, method_name, args, target_attr):
    ctrl = _controller()
    ctrl.ensure_meeting_start_readiness.return_value = None
    getattr(ctrl, method_name)(*args)
    getattr(ctrl, target_attr).assert_not_called()


@pytest.mark.parametrize(
    "method_name,args,target_attr",
    [
        ("_on_meeting_start_requested", (True,), "on_meeting_start"),
        ("_on_tray_meeting_toggle", (), "on_meeting_start"),
    ],
)
def test_start_seams_forward_mic_only_disabled(qapp, method_name, args, target_attr):
    ctrl = _controller()
    ctrl.ensure_meeting_start_readiness.return_value = "disabled"
    getattr(ctrl, method_name)(*args)
    target = getattr(ctrl, target_attr)
    target.assert_called_once()
    assert target.call_args.kwargs.get("system_audio_policy") == "disabled"


def test_demo_uses_platform_ack_without_audio_readiness(qapp):
    ctrl = _controller()

    ctrl._on_meeting_demo_requested(False)

    ctrl.ensure_meeting_platform_ack.assert_called_once()
    ctrl.ensure_meeting_start_readiness.assert_not_called()
    ctrl.on_meeting_start_demo.assert_called_once_with(
        False,
        system_audio_policy="disabled",
    )


@pytest.mark.parametrize(
    "answer, expected_calls",
    [
        ("Yes", 1),
        ("No", 0),
    ],
)
def test_active_tray_end_requires_confirmation(qapp, answer, expected_calls):
    from ui_qt.ui_controller import QMessageBox

    ctrl = _controller()
    ctrl._meeting_active = True
    ctrl.on_meeting_end = MagicMock()
    button = getattr(QMessageBox.StandardButton, answer)

    with patch("ui_qt.ui_controller.QMessageBox.question", return_value=button):
        ctrl._on_tray_meeting_toggle()

    assert ctrl.on_meeting_end.call_count == expected_calls
    ctrl.ensure_meeting_start_readiness.assert_not_called()


def test_demo_cancelled_by_platform_ack_starts_nothing(qapp):
    ctrl = _controller()
    ctrl.ensure_meeting_platform_ack.return_value = False

    ctrl._on_meeting_demo_requested(False)

    ctrl.ensure_meeting_start_readiness.assert_not_called()
    ctrl.on_meeting_start_demo.assert_not_called()


def test_hotkey_controller_uses_readiness_once(qapp):
    from services import application_controller as mod
    from tests.test_application_controller import DummyUIController

    ui = DummyUIController()
    ui.platform_ack_result = True
    ctrl = SimpleNamespace(
        ui_controller=ui,
        meeting_runtime=SimpleNamespace(start_meeting=MagicMock()),
    )
    mod.ApplicationController._on_meeting_platform_ack_requested(ctrl)
    assert ui.platform_ack_requests == 1
    ctrl.meeting_runtime.start_meeting.assert_called_once_with(
        system_audio_policy="auto"
    )



def test_ensure_meeting_start_readiness_linux_required(qapp):
    from ui_qt.ui_controller import UIController

    ctrl = SimpleNamespace(
        main_window=SimpleNamespace(
            tabbed_content=SimpleNamespace(unlock_meeting_tab=MagicMock())
        )
    )
    method = UIController.ensure_meeting_start_readiness.__get__(ctrl, UIController)
    with patch("sys.platform", "linux"), patch(
        "ui_qt.dialogs.meeting_unsupported_dialog.acknowledge_unsupported_meeting_mode",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_system_audio_dialog.ensure_meeting_system_audio_permission",
        return_value=True,
    ), patch(
        "meeting.platform.linux_meeting_implementation_ready",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_linux_audio_dialog.ensure_meeting_linux_system_audio",
        return_value="ready",
    ):
        assert method() == "required"


def test_ensure_meeting_start_readiness_linux_mic_only(qapp):
    from ui_qt.ui_controller import UIController

    ctrl = SimpleNamespace(
        main_window=SimpleNamespace(
            tabbed_content=SimpleNamespace(unlock_meeting_tab=MagicMock())
        )
    )
    method = UIController.ensure_meeting_start_readiness.__get__(ctrl, UIController)
    with patch("sys.platform", "linux"), patch(
        "ui_qt.dialogs.meeting_unsupported_dialog.acknowledge_unsupported_meeting_mode",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_system_audio_dialog.ensure_meeting_system_audio_permission",
        return_value=True,
    ), patch(
        "meeting.platform.linux_meeting_implementation_ready",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_linux_audio_dialog.ensure_meeting_linux_system_audio",
        return_value="microphone_only",
    ):
        assert method() == "disabled"


def test_ensure_meeting_start_readiness_linux_cancel(qapp):
    from ui_qt.ui_controller import UIController

    ctrl = SimpleNamespace(
        main_window=SimpleNamespace(
            tabbed_content=SimpleNamespace(unlock_meeting_tab=MagicMock())
        )
    )
    method = UIController.ensure_meeting_start_readiness.__get__(ctrl, UIController)
    with patch("sys.platform", "linux"), patch(
        "ui_qt.dialogs.meeting_unsupported_dialog.acknowledge_unsupported_meeting_mode",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_system_audio_dialog.ensure_meeting_system_audio_permission",
        return_value=True,
    ), patch(
        "meeting.platform.linux_meeting_implementation_ready",
        return_value=True,
    ), patch(
        "ui_qt.dialogs.meeting_linux_audio_dialog.ensure_meeting_linux_system_audio",
        return_value="cancel",
    ):
        assert method() is None
