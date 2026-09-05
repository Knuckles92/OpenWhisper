import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.engine_field import EngineStatus
from ui_qt.widgets.quick_record_tab import QuickRecordTab


@pytest.fixture
def tab():
    app = QApplication.instance() or QApplication([])
    widget = QuickRecordTab()
    widget.resize(680, 500)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_ready_details_and_runtime_messages_share_the_card(tab):
    tab.set_device_info("Parakeet TDT 0.6B v3 | cuda", True)
    assert tab.findChild(QLabel, "statusLabel") is None
    assert tab.engine_card.isAncestorOf(tab.resolved_label)
    assert tab.resolved_label.text() == "Parakeet TDT 0.6B v3 | cuda"
    tab.set_status("Copied to clipboard")
    assert tab.resolved_label.text() == "Copied to clipboard"
    tab.set_status("Ready to record")
    assert tab.resolved_label.text() == "Parakeet TDT 0.6B v3 | cuda"


def test_loading_animates_and_returns_to_resolved_engine(tab):
    tab.set_engine_busy(True)
    assert tab.status_dot._timer.isActive()
    first_frame = tab.status_dot.grab().toImage()
    QTest.qWait(120)
    assert tab.status_dot.grab().toImage() != first_frame
    tab.set_device_info("base | cpu (int8)", True)
    tab.set_status("Whisper engine ready")
    assert tab.status_dot._timer.isActive()
    tab.set_engine_busy(False)
    assert not tab.status_dot._timer.isActive()
    assert tab.status_dot.status() is EngineStatus.READY
    assert tab.resolved_label.text() == "base | cpu (int8)"


def test_loading_failure_keeps_the_error_after_animation_stops(tab):
    tab.set_engine_busy(True)
    tab.set_status("Engine load failed: not enough memory")
    tab.set_engine_busy(False)
    assert not tab.status_dot._timer.isActive()
    assert tab.resolved_label.text() == "Engine load failed: not enough memory"


@pytest.mark.parametrize("state", [
    OverlayState.PROCESSING, OverlayState.TRANSCRIBING,
    OverlayState.CLEANING, OverlayState.CANCELING,
])
def test_activity_animates_until_finished_without_erasing_outcome(tab, state):
    tab.set_activity_state(state)
    assert tab.status_dot._timer.isActive()
    tab.set_status("Transcription canceled" if state is OverlayState.CANCELING
                   else "Error: audio could not be processed")
    message = tab.resolved_label.text()
    tab.set_activity_state(OverlayState.NONE)
    assert not tab.status_dot._timer.isActive()
    assert tab.resolved_label.text() == message


def test_animation_suspends_when_hidden_and_resumes_when_shown(tab):
    tab.set_engine_busy(True)
    tab.hide()
    assert not tab.status_dot._timer.isActive()
    tab.show()
    assert tab.status_dot._timer.isActive()


def test_cloud_processing_has_an_indicator_without_claiming_local_readiness(tab):
    tab.set_backend("API")
    tab.set_device_info("")
    assert not tab.status_dot.isVisible()
    tab.set_activity_state(OverlayState.TRANSCRIBING)
    assert tab.status_dot.isVisible()
    assert tab.status_dot._timer.isActive()
    tab.set_activity_state(OverlayState.NONE)
    assert not tab.status_dot.isVisible()


def test_long_messages_keep_details_and_download_action(tab):
    tab.set_status("Install model in Downloads.")
    QApplication.processEvents()
    original_width = tab.engine_card.minimumSizeHint().width()
    message = "Install " + "a very long speech model name " * 8 + "in Downloads."
    opened = []
    tab.engine_downloads_requested.connect(lambda: opened.append(True))
    tab.set_status(message)
    QApplication.processEvents()
    assert tab.resolved_label.toolTip() == message
    assert 'href="downloads"' in QLabel.text(tab.resolved_label)
    tab.resolved_label.linkActivated.emit("downloads")
    assert opened == [True]
    assert tab.engine_card.minimumSizeHint().width() <= original_width


def test_engine_messages_after_an_upload_reach_the_visible_status(tab):
    from ui_qt.ui_controller import UIController
    from ui_qt.widgets import TabbedContentWidget
    upload = MagicMock()
    window = SimpleNamespace(quick_record_tab=tab, upload_file_tab=upload,
                             set_status=tab.set_status)
    controller = SimpleNamespace(main_window=window, _transcription_source_tab=TabbedContentWidget.TAB_UPLOAD_FILE)
    tab.set_engine_busy(True)
    UIController._apply_status_to_main_window(controller, "Downloading model...")
    assert tab.resolved_label.text() == "Downloading model..."
    upload.set_engine_message.assert_called_once_with("Downloading model...")


def test_upload_job_messages_stay_in_the_upload_panel(tab):
    from ui_qt.ui_controller import UIController
    from ui_qt.widgets import TabbedContentWidget
    upload = MagicMock()
    window = SimpleNamespace(quick_record_tab=tab, upload_file_tab=upload,
                             set_status=MagicMock())
    controller = SimpleNamespace(main_window=window, _transcription_source_tab=TabbedContentWidget.TAB_UPLOAD_FILE)
    UIController._apply_status_to_main_window(controller, "Transcribing chunk 2/3...")
    upload.set_status.assert_called_once_with("Transcribing chunk 2/3...")
    window.set_status.assert_not_called()


def test_download_and_reload_activity_end_independently(tab):
    tab.set_model_downloading("tiny.en", True)
    tab.set_model_downloading("base", True)
    tab.set_engine_busy(True)
    tab.set_model_downloading("tiny.en", False)
    tab.set_model_downloading("base", False)
    assert tab.status_dot._timer.isActive()
    tab.set_device_info("base | cpu (int8)", True)
    tab.set_engine_busy(False)
    assert not tab.status_dot._timer.isActive()


def test_download_failure_stops_animation_and_preserves_reason(tab):
    tab.set_model_downloading("base", True)
    tab.set_status("Model download failed: connection interrupted")
    tab.set_model_downloading("base", False)
    assert not tab.status_dot._timer.isActive()
    assert tab.resolved_label.text() == "Model download failed: connection interrupted"


def test_cached_download_completion_does_not_leave_a_loading_message(tab):
    tab.set_device_info("base | cpu (int8)", True)
    tab.set_model_downloading("tiny.en", True)
    tab.set_model_downloading("tiny.en", False)
    assert tab.resolved_label.text() == "base | cpu (int8)"
    assert not tab.status_dot._timer.isActive()
