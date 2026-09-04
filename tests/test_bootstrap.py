"""Unit tests for the extracted Qt bootstrap flow."""

import pytest
import logging
from unittest.mock import patch

from ui_qt import bootstrap


class _FakeLoadingScreen:
    def __init__(self, order=None):
        self.destroyed = False
        self.statuses = []
        self.progress = []
        self.shown = False
        self.order = order

    def show(self):
        self.shown = True
        if self.order is not None:
            self.order.append("loading_screen_shown")

    def update_status(self, status):
        self.statuses.append(status)

    def update_progress(self, progress):
        self.progress.append(progress)

    def repaint(self):
        pass

    def destroy(self):
        self.destroyed = True


class _FakeUIController:
    def __init__(self):
        self.main_window = object()
        self.show_main_window_called = False
        self.device_info = None
        self.cleaned_up = False
        self.apply_error_checked = False

    def show_main_window(self):
        self.show_main_window_called = True

    def set_device_info(self, device_info, ready=None):
        self.device_info = device_info

    def show_apply_error_if_any(self):
        self.apply_error_checked = True

    def cleanup(self):
        self.cleaned_up = True


class _FakeQtApplication:
    def __init__(self):
        self.main_window = None
        self.raise_on_run = False

    def run(self, main_window):
        self.main_window = main_window
        if self.raise_on_run:
            raise RuntimeError("boom")
        return 123


class _FakeBackend:
    def __init__(self, device_info="cpu"):
        self.device_info = device_info


class _FakeApplicationController:
    should_raise = False
    instances = []

    def __init__(self, ui_controller, local_backend=None):
        if self.should_raise:
            raise RuntimeError("controller init failed")
        self.ui_controller = ui_controller
        self.local_backend = local_backend
        self.cleaned_up = False
        self.main_ui_ready_notified = False
        self.transcription_backends = {"local_whisper": _FakeBackend("cuda")}
        self.__class__.instances.append(self)

    def notify_main_ui_ready(self):
        self.main_ui_ready_notified = True

    def cleanup(self):
        self.cleaned_up = True


class TestBootstrap:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _FakeApplicationController.instances = []
        _FakeApplicationController.should_raise = False

    @patch("services.settings.is_hf_hub_offline_env_set", return_value=False)
    @patch.object(bootstrap, "create_deferred_local_whisper_backend", return_value=None)
    @patch.object(bootstrap, "run_with_ui_pulse", side_effect=lambda fn: fn())
    @patch.object(bootstrap, "process_qt_events")
    @patch.object(bootstrap, "setup_logging")
    def test_main_runs_startup_flow_and_cleans_up_controller(
        self,
        _mock_setup_logging,
        _mock_process_events,
        _mock_pulse,
        _mock_load_backend,
        _mock_hf_env,
    ):
        qt_app = _FakeQtApplication()
        ui_controller = _FakeUIController()
        order = []
        loading_screen = _FakeLoadingScreen(order)

        def get_early_runtime_components():
            order.append("early_imports")
            return lambda: qt_app, lambda: loading_screen

        def get_late_runtime_components():
            order.append("late_imports")
            return lambda: ui_controller, _FakeApplicationController

        _mock_process_events.side_effect = lambda: order.append("process_events")

        with patch.object(
            bootstrap,
            "get_early_runtime_components",
            side_effect=get_early_runtime_components,
        ), patch.object(
            bootstrap,
            "get_late_runtime_components",
            side_effect=get_late_runtime_components,
        ):
            result = bootstrap.main()

        assert result == 123
        assert loading_screen.destroyed
        assert ui_controller.show_main_window_called
        # Device info is filled in after the background Whisper load, not
        # before the window appears.
        assert ui_controller.device_info is None
        assert len(_FakeApplicationController.instances) == 1
        assert _FakeApplicationController.instances[0].cleaned_up
        assert _FakeApplicationController.instances[0].main_ui_ready_notified
        assert order.index("loading_screen_shown") < order.index("late_imports")
        assert order.index("process_events") < order.index("late_imports")

    @patch("services.settings.is_hf_hub_offline_env_set", return_value=False)
    @patch.object(bootstrap, "create_deferred_local_whisper_backend", return_value=None)
    @patch.object(bootstrap, "run_with_ui_pulse", side_effect=lambda fn: fn())
    @patch.object(bootstrap, "process_qt_events")
    @patch.object(bootstrap, "setup_logging")
    def test_main_cleans_up_loading_screen_and_controller_on_run_error(
        self,
        _mock_setup_logging,
        _mock_process_events,
        _mock_pulse,
        _mock_load_backend,
        _mock_hf_env,
    ):
        qt_app = _FakeQtApplication()
        qt_app.raise_on_run = True
        ui_controller = _FakeUIController()
        loading_screen = _FakeLoadingScreen()

        with patch.object(
            bootstrap,
            "get_early_runtime_components",
            return_value=(lambda: qt_app, lambda: loading_screen),
        ), patch.object(
            bootstrap,
            "get_late_runtime_components",
            return_value=(lambda: ui_controller, _FakeApplicationController),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                bootstrap.main()

        assert loading_screen.destroyed
        assert len(_FakeApplicationController.instances) == 1
        assert _FakeApplicationController.instances[0].cleaned_up


class TestCudaPreloadSummary:
    """The Linux CUDA preload log must survive being run as ``__main__``.

    ``python app_qt.py`` registers the entry module as ``__main__``, not
    ``app_qt``, so a lookup of only ``sys.modules["app_qt"]`` silently logged
    nothing in every real launch — confirmed on Linux hardware.
    """

    class _Entrypoint:
        def __init__(self, libraries):
            self.CUDA_PRELOADED_LIBRARIES = libraries

    def _capture(self, modules, caplog):
        with patch.object(bootstrap.sys, "platform", "linux"), patch.dict(
            bootstrap.sys.modules, modules, clear=False
        ):
            with caplog.at_level(logging.INFO):
                bootstrap.log_cuda_preload_summary()
        return caplog.text

    def test_logs_when_entry_module_is_main(self, caplog):
        entry = self._Entrypoint(["libcublas.so.12", "libcudart.so.12"])
        modules = {"__main__": entry}
        with patch.dict(bootstrap.sys.modules, {}, clear=False):
            bootstrap.sys.modules.pop("app_qt", None)
            output = self._capture(modules, caplog)

        assert "Preloaded 2 CUDA library/libraries" in output
        assert "libcublas.so.12" in output

    def test_logs_when_entry_module_is_app_qt(self, caplog):
        entry = self._Entrypoint(["libcublas.so.12"])
        output = self._capture({"app_qt": entry}, caplog)

        assert "Preloaded 1 CUDA library/libraries" in output

    def test_reports_when_nothing_was_preloaded(self, caplog):
        """An empty list is a real answer: wheels absent, so CPU it is."""
        output = self._capture({"app_qt": self._Entrypoint([])}, caplog)

        assert "No NVIDIA CUDA libraries preloaded" in output

    def test_silent_on_non_linux(self):
        with patch.object(bootstrap.sys, "platform", "win32"):
            with patch.object(bootstrap.logging, "info") as info:
                bootstrap.log_cuda_preload_summary()
        info.assert_not_called()




@pytest.mark.parametrize("readiness_error,acknowledged", [("database unavailable", True), ("", False), ("", True)])
def test_update_health_requires_database_and_matching_transaction(monkeypatch, readiness_error, acknowledged):
    from unittest.mock import MagicMock
    from PyQt6.QtCore import QTimer
    from services import app_update_apply

    timers = []
    qt_app = _FakeQtApplication()
    qt_app.app = MagicMock()
    ui = _FakeUIController()
    ui.main_window = MagicMock()
    loading = _FakeLoadingScreen()

    class Controller(_FakeApplicationController):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.update_readiness_error = readiness_error

    def run(window):
        for callback in timers:
            callback()
        return 0

    qt_app.run = run
    monkeypatch.setattr(bootstrap, "get_early_runtime_components", lambda: (lambda: qt_app, lambda: loading))
    monkeypatch.setattr(bootstrap, "get_late_runtime_components", lambda: (lambda: ui, Controller))
    monkeypatch.setattr(bootstrap, "create_deferred_local_whisper_backend", lambda: None)
    monkeypatch.setattr(bootstrap, "process_qt_events", lambda: None)
    monkeypatch.setattr(bootstrap, "setup_logging", lambda: None)
    monkeypatch.setattr(bootstrap, "run_with_ui_pulse", lambda fn: fn())
    monkeypatch.setattr(app_update_apply, "parse_health_token", lambda: "b" * 32)
    ack = MagicMock(return_value=acknowledged)
    monkeypatch.setattr(app_update_apply, "write_health_acknowledgement", ack)
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: timers.append(callback))
    assert bootstrap.main() == 0
    assert not ui.apply_error_checked
    assert ui.main_window.setEnabled.call_args_list[0].args == (False,)
    if readiness_error:
        ack.assert_not_called()
    else:
        ack.assert_called_once_with("b" * 32)
    if readiness_error or not acknowledged:
        qt_app.app.exit.assert_called_once_with(1)
        assert ui.main_window.setEnabled.call_count == 1
    else:
        qt_app.app.exit.assert_not_called()
        assert ui.main_window.setEnabled.call_args.args == (True,)
