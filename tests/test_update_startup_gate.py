"""Startup gate for frozen Windows launches during a setup or native update."""
from unittest.mock import patch

import pytest

from services.app_update_apply import maybe_exit_if_update_in_progress
from services.update_contract import SETUP_MUTEX_NAME


@pytest.fixture(autouse=True)
def _no_modal_box():
    """A real MessageBoxW would block the whole suite until someone clicked it."""
    with patch("services.app_update_apply.native_message_box") as box:
        yield box


@pytest.fixture
def frozen_windows(monkeypatch):
    monkeypatch.setattr("services.app_update_apply.sys.platform", "win32")
    monkeypatch.setattr("services.app_update_apply.sys.frozen", True, raising=False)


def test_non_frozen_launch_is_not_gated():
    with patch("services.app_update_apply.sys") as fake_sys:
        fake_sys.platform = "win32"
        fake_sys.frozen = False
        maybe_exit_if_update_in_progress(["--update-health", "x"])


def test_update_mutex_without_token_exits(frozen_windows, _no_modal_box):
    with patch("services.app_update_apply.mutex_exists", return_value=True), patch(
        "services.app_update_apply.parse_health_token", return_value=None
    ):
        with pytest.raises(SystemExit) as caught:
            maybe_exit_if_update_in_progress([])

    assert caught.value.code == 0
    _no_modal_box.assert_called_once()
    assert "finishing an update" in _no_modal_box.call_args[0][0]


def test_health_token_may_launch_during_update(frozen_windows):
    with patch(
        "services.app_update_apply.mutex_exists", return_value=True
    ), patch(
        "services.app_update_apply.is_valid_health_launch_token",
        return_value=True,
    ):
        maybe_exit_if_update_in_progress(["--update-health", "b" * 32])


def test_the_launch_that_follows_setup_waits_instead_of_vanishing(frozen_windows):
    """Inno's postinstall launch always runs while setup still holds its mutex."""
    held = [True, True, False]

    def setup_mutex_then_free(name):
        if name != SETUP_MUTEX_NAME:
            return False
        return held.pop(0) if held else False

    with patch(
        "services.app_update_apply.mutex_exists", side_effect=setup_mutex_then_free
    ), patch("services.app_update_apply.time.sleep") as sleep:
        maybe_exit_if_update_in_progress([])

    assert sleep.call_count == 2


def test_a_setup_that_never_ends_refuses_the_launch(frozen_windows, _no_modal_box):
    with patch(
        "services.app_update_apply.mutex_exists",
        side_effect=lambda name: name == SETUP_MUTEX_NAME,
    ), patch("services.app_update_apply._SETUP_LAUNCH_WAIT_S", 0.0), patch(
        "services.app_update_apply.time.sleep"
    ):
        with pytest.raises(SystemExit) as caught:
            maybe_exit_if_update_in_progress([])

    assert caught.value.code == 0
    assert "Setup is still running" in _no_modal_box.call_args[0][0]
