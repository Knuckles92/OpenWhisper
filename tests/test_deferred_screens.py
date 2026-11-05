"""Inactive desktop views retain updates and connect only when opened."""
from PyQt6.QtTest import QSignalSpy

from ui_qt.ui_controller import UIController
from ui_qt.widgets.tabbed_content import TabbedContentWidget


def test_controller_startup_keeps_inactive_views_unbuilt():
    controller = UIController()
    try:
        window = controller.main_window
        assert window._screen_widgets == {}
        controller.set_engine_busy(True)
        window.set_device_info("CPU", True)
        controller.on_meeting_state_changed({"active": False, "status": "idle"})
        window.update_screen("upload", "set_model_downloading", "tiny", True)
        window.update_screen("upload", "set_model_downloading", "base", True)
        window.update_screen("upload", "set_model_downloading", "tiny", False)
        window.update_screen("meeting", "set_dashboard_available", True)
        controller.on_meeting_state_changed({"active": False, "dashboard_available": False})
        assert window._screen_widgets == {}

        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_UPLOAD_FILE)
        upload = window.upload_file_tab
        assert upload._engine_busy
        assert upload._device_info == "CPU"
        assert upload._model_downloads == {"base"}
        assert set(window._screen_widgets) == {"upload"}
        forwarded = QSignalSpy(window.upload_file_requested)
        upload.upload_requested.emit("clip.wav", 2.0)
        assert list(forwarded) == [["clip.wav", 2.0]]
        assert window.upload_file_tab is upload
        controller.set_engine_busy(False)

        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_MEETING_MODE)
        meeting = window.meeting_mode_tab
        assert not meeting.is_meeting_active
        assert set(window._screen_widgets) == {"upload", "meeting"}
        started = []
        # The established signal connection is checked by a callback that the
        # original handler reads at invocation, not by reconnecting the view.
        controller.on_meeting_pause = lambda: started.append("pause")
        meeting.pause_requested.emit()
        assert started == ["pause"]
        assert window.meeting_mode_tab is meeting
    finally:
        controller.cleanup()


def test_host_dashboard_materializes_once_and_receives_queued_values():
    controller = UIController()
    try:
        window = controller.main_window
        window.set_device_info("CPU", True)
        controller.set_engine_busy(True)
        assert "host" not in window._screen_widgets
        window.set_host_mode(True, persist=False)
        host = window.host_dashboard
        assert "host" in window._screen_widgets
        window.set_host_mode(False, persist=False)
        window.set_host_mode(True, persist=False)
        assert window.host_dashboard is host
        assert "upload" not in window._screen_widgets
        assert "meeting" not in window._screen_widgets
    finally:
        controller.cleanup()


def test_meeting_timer_survives_later_state_updates():
    controller = UIController()
    try:
        window = controller.main_window
        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_MEETING_MODE)
        meeting = window.meeting_mode_tab
        controller.on_meeting_state_changed(
            {"active": True, "paused": False, "status": "active", "elapsed_s": 0.0}
        )
        # Pretend the meeting has run for 45 seconds.
        meeting._running_since -= 45
        meeting._refresh_elapsed()
        assert meeting.elapsed_label.text() == "00:45"

        # Status notes, pause mirroring, and background readiness are all
        # partial updates; none of them may restart the clock.
        controller.on_meeting_state_changed({"status": "active", "paused": False})
        controller.on_meeting_state_changed({"background_available": True})
        meeting._refresh_elapsed()
        assert meeting.elapsed_label.text() == "00:45"
    finally:
        controller.cleanup()


def test_meeting_tab_built_mid_meeting_shows_elapsed_time():
    controller = UIController()
    try:
        window = controller.main_window
        # Straight to the window: the controller would open the tab now.
        window.update_screen("meeting", "set_meeting_state", {
            "active": True, "paused": False, "status": "active", "elapsed_s": 0.0,
        })
        window.update_screen("meeting", "set_meeting_state", {"status": "active"})
        assert "meeting" not in window._screen_widgets
        banked, since = window._meeting_clock
        window._meeting_clock = (banked, since - 90)

        meeting = window._ensure_screen("meeting")
        assert meeting.is_meeting_active
        assert meeting.elapsed_label.text() == "01:30"
    finally:
        controller.cleanup()
