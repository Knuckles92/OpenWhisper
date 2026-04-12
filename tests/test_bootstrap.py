"""Unit tests for the extracted Qt bootstrap flow."""

import unittest
from unittest.mock import patch

from ui_qt import bootstrap


class _FakeLoadingScreen:
    def __init__(self):
        self.destroyed = False
        self.statuses = []
        self.progress = []
        self.shown = False

    def show(self):
        self.shown = True

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

    def show_main_window(self):
        self.show_main_window_called = True

    def set_device_info(self, device_info):
        self.device_info = device_info

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

    def __init__(self, ui_controller):
        if self.should_raise:
            raise RuntimeError("controller init failed")
        self.ui_controller = ui_controller
        self.cleaned_up = False
        self.transcription_backends = {"local_whisper": _FakeBackend("cuda")}
        self.__class__.instances.append(self)

    def cleanup(self):
        self.cleaned_up = True


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        _FakeApplicationController.instances = []
        _FakeApplicationController.should_raise = False

    @patch.object(bootstrap, "process_qt_events")
    @patch.object(bootstrap, "setup_logging")
    def test_main_runs_startup_flow_and_cleans_up_controller(
        self, _mock_setup_logging, _mock_process_events
    ):
        qt_app = _FakeQtApplication()
        ui_controller = _FakeUIController()
        loading_screen = _FakeLoadingScreen()

        with patch.object(
            bootstrap,
            "get_runtime_components",
            return_value=(
                lambda: qt_app,
                lambda: loading_screen,
                lambda: ui_controller,
                _FakeApplicationController,
            ),
        ):
            result = bootstrap.main()

        self.assertEqual(result, 123)
        self.assertTrue(loading_screen.destroyed)
        self.assertTrue(ui_controller.show_main_window_called)
        self.assertEqual(ui_controller.device_info, "cuda")
        self.assertEqual(len(_FakeApplicationController.instances), 1)
        self.assertTrue(_FakeApplicationController.instances[0].cleaned_up)

    @patch.object(bootstrap, "process_qt_events")
    @patch.object(bootstrap, "setup_logging")
    def test_main_cleans_up_loading_screen_and_controller_on_run_error(
        self, _mock_setup_logging, _mock_process_events
    ):
        qt_app = _FakeQtApplication()
        qt_app.raise_on_run = True
        ui_controller = _FakeUIController()
        loading_screen = _FakeLoadingScreen()

        with patch.object(
            bootstrap,
            "get_runtime_components",
            return_value=(
                lambda: qt_app,
                lambda: loading_screen,
                lambda: ui_controller,
                _FakeApplicationController,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                bootstrap.main()

        self.assertTrue(loading_screen.destroyed)
        self.assertEqual(len(_FakeApplicationController.instances), 1)
        self.assertTrue(_FakeApplicationController.instances[0].cleaned_up)


if __name__ == "__main__":
    unittest.main()
