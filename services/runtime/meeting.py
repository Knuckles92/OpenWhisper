"""Meeting mode helpers for the application controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pyperclip

from services.meeting_controller import MeetingController
from services.settings import settings_manager
from transcriber import LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController


class MeetingRuntime:
    """Owns meeting-mode wiring between UI and services."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def setup_meeting_mode(self) -> None:
        """Initialize meeting mode controller."""
        try:
            local_backend = self.controller.transcription_backends.get("local_whisper")
            if local_backend and isinstance(local_backend, LocalWhisperBackend):
                self.controller.meeting_controller = MeetingController(backend=local_backend)

                meeting_tab = self.controller.ui_controller.get_meeting_tab()
                if meeting_tab:
                    meeting_tab.on_start_meeting = self.on_start_meeting
                    meeting_tab.on_stop_meeting = self.on_stop_meeting

                self.controller.ui_controller.on_load_meeting = self.on_load_meeting
                self.controller.ui_controller.on_delete_meeting = self.on_delete_meeting
                self.controller.ui_controller.on_rename_meeting = self.on_rename_meeting
                self.controller.ui_controller.on_copy_meeting = self.on_copy_meeting

                self.refresh_meeting_list()
                self.controller.meeting_controller.recover_pending_chunks()
                self.refresh_meeting_list()

                logging.info("Meeting mode initialized")
            else:
                logging.warning("Meeting mode requires Local Whisper backend")
        except Exception as exc:
            logging.error(f"Failed to initialize meeting mode: {exc}")

    def on_start_meeting(self) -> None:
        """Handle start meeting from Meeting Mode tab."""
        if self.controller.meeting_controller is None:
            logging.error("Meeting controller not available")
            return

        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if meeting_tab is None:
            return

        title = meeting_tab.get_meeting_title()
        saved_device_id = settings_manager.load_audio_input_device()

        if self.controller.meeting_controller.start_meeting(title, saved_device_id):
            self.controller.meeting_controller.on_chunk_transcribed = self.on_meeting_chunk
            meeting_tab.set_status("Recording in progress...")

            from ui_qt.widgets import TabbedContentWidget

            self.controller.ui_controller.main_window.tabbed_content.set_recording_state(
                True, TabbedContentWidget.TAB_MEETING_MODE
            )
        else:
            meeting_tab.set_idle()
            meeting_tab.set_status("Failed to start meeting")

    def on_stop_meeting(self) -> None:
        """Handle stop meeting from Meeting Mode tab."""
        if self.controller.meeting_controller is None:
            return

        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if meeting_tab is None:
            return

        meeting_tab.set_processing()
        meeting_tab.set_status("Finalizing transcription...")
        meeting = self.controller.meeting_controller.stop_meeting()

        if meeting:
            meeting_tab.set_status(f"Meeting saved ({meeting.formatted_duration})")
        else:
            meeting_tab.set_status("Meeting ended")

        meeting_tab.set_idle()
        self.controller.ui_controller.main_window.tabbed_content.set_recording_state(
            False, -1
        )
        self.refresh_meeting_list()

    def on_meeting_chunk(self, text: str) -> None:
        """Handle transcribed chunk from meeting."""
        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if meeting_tab:
            meeting_tab.append_transcription(text)

    def on_load_meeting(self, meeting_id: str) -> None:
        """Handle loading a past meeting from the sidebar."""
        if self.controller.meeting_controller is None:
            return

        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if meeting_tab is None:
            return

        meeting = self.controller.meeting_controller.get_meeting(meeting_id)
        if meeting:
            meeting_tab.set_meeting_title(meeting.title)
            meeting_tab.set_transcription(meeting.transcript)
            meeting_tab.set_status(f"Loaded: {meeting.title}")

    def on_delete_meeting(self, meeting_id: str) -> None:
        """Handle meeting deletion from the sidebar."""
        if self.controller.meeting_controller is None:
            return

        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if self.controller.meeting_controller.delete_meeting(meeting_id):
            if meeting_tab:
                meeting_tab.set_status("Meeting deleted")
                meeting_tab.clear_transcription()
                meeting_tab.set_meeting_title("")
            self.refresh_meeting_list()
        elif meeting_tab:
            meeting_tab.set_status("Failed to delete meeting")

    def on_rename_meeting(self, meeting_id: str, new_title: str) -> None:
        """Handle meeting rename from the sidebar."""
        if self.controller.meeting_controller is None:
            return

        meeting_tab = self.controller.ui_controller.get_meeting_tab()
        if self.controller.meeting_controller.rename_meeting(meeting_id, new_title):
            if meeting_tab:
                meeting_tab.set_status(f"Renamed to: {new_title}")
            self.refresh_meeting_list()
        elif meeting_tab:
            meeting_tab.set_status("Failed to rename meeting")

    def on_copy_meeting(self, meeting_id: str) -> None:
        """Handle meeting transcript copy from the sidebar."""
        if self.controller.meeting_controller is None:
            return

        meeting = self.controller.meeting_controller.get_meeting(meeting_id)
        if meeting and meeting.transcript:
            pyperclip.copy(meeting.transcript)
            meeting_tab = self.controller.ui_controller.get_meeting_tab()
            if meeting_tab:
                meeting_tab.set_status("Transcript copied to clipboard")

    def on_get_meeting(self, meeting_id: str):
        """Get meeting data for context menu actions."""
        if self.controller.meeting_controller is None:
            return None
        return self.controller.meeting_controller.get_meeting(meeting_id)

    def refresh_meeting_list(self) -> None:
        """Refresh the meetings list in the sidebar."""
        if self.controller.meeting_controller is None:
            return

        meetings = self.controller.meeting_controller.get_meetings_for_display()
        self.controller.ui_controller.refresh_meetings_list(meetings)
