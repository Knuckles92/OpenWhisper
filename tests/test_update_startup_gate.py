"""Startup gate for frozen Windows launches during a native update."""
from unittest.mock import patch

from services.app_update_apply import maybe_exit_if_update_in_progress
from services.update_contract import SETUP_MUTEX_NAME


def test_non_frozen_launch_is_not_gated():
    with patch("services.app_update_apply.sys") as fake_sys:
        fake_sys.platform = "win32"
        fake_sys.frozen = False
        maybe_exit_if_update_in_progress(["--update-health", "x"])


def test_update_mutex_without_token_exits(monkeypatch):
    monkeypatch.setattr("services.app_update_apply.sys.platform", "win32")
    monkeypatch.setattr("services.app_update_apply.sys.frozen", True, raising=False)
    with patch("services.app_update_apply.mutex_exists", return_value=True), patch(
        "services.app_update_apply.parse_health_token", return_value=None
    ):
        try:
            maybe_exit_if_update_in_progress([])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("expected SystemExit")


def test_health_token_may_launch_during_update(monkeypatch):
    monkeypatch.setattr("services.app_update_apply.sys.platform", "win32")
    monkeypatch.setattr("services.app_update_apply.sys.frozen", True, raising=False)
    token = "b" * 32
    with patch(
        "services.app_update_apply.mutex_exists", return_value=True
    ), patch(
        "services.app_update_apply.is_valid_health_launch_token",
        return_value=True,
    ):
        maybe_exit_if_update_in_progress(["--update-health", token])


def test_setup_mutex_blocks_spoofed_health_launch(monkeypatch):
    monkeypatch.setattr("services.app_update_apply.sys.platform", "win32")
    monkeypatch.setattr("services.app_update_apply.sys.frozen", True, raising=False)
    with patch(
        "services.app_update_apply.mutex_exists",
        side_effect=lambda name: name == SETUP_MUTEX_NAME,
    ), patch(
        "services.app_update_apply.is_valid_health_launch_token",
        return_value=True,
    ):
        try:
            maybe_exit_if_update_in_progress(
                ["--update-health", "b" * 32]
            )
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("expected SystemExit")
