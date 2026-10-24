"""The Walkthrough and Screenshot shortcuts (docs/walkthrough.md); Qt thread.

A walkthrough is a recording that collects screenshots: each one the user
keeps closes a section of the transcript. TranscriptionRuntime records and
transcribes it like a dictation and owns its WalkthroughSession; this
runtime decides what each shortcut press means and turns kept screenshots
into the session's shots.

Outside a walkthrough the Screenshot shortcut is a plain annotate-and-copy
screenshot tool.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QTimer

from services.walkthrough.session import ScreenshotNotSaved, WalkthroughSession

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: What the snap overlay says while a walkthrough keeps recording.
RECORDING_HINT = "Recording continues — talk while you draw"
#: Lets the tray menu close before the screen is grabbed.
TRAY_SNAP_DELAY_MS = 300
FINISH_RECORDING_FIRST = "Finish the recording in progress first"


def shots_label(count: int) -> str:
    """What the recording overlay says once ``count`` screenshots are kept."""
    from services.runtime.transcription import WALKTHROUGH_LABEL

    if count == 0:
        return WALKTHROUGH_LABEL
    return f"{WALKTHROUGH_LABEL} · {count} shot{'' if count == 1 else 's'}"


class WalkthroughRuntime:
    """Starts and finishes walkthroughs and takes their screenshots; Qt thread."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        # The open snap overlay (a SnapSession), and the walkthrough it adds
        # to (None for a plain screenshot).
        self._snap = None
        self._snap_for: Optional[WalkthroughSession] = None
        # The Walkthrough shortcut came while a screenshot was being drawn:
        # finish once that screenshot is kept or dropped.
        self._finish_after_snap = False
        # A walkthrough this runtime started is recording.
        self._active = False
        self._out_of_screenshots = False
        self._tray_timer: Optional[QTimer] = None

    @property
    def _ui(self):
        return self.controller.ui_controller

    # ---- the Walkthrough shortcut ----

    def toggle(self, source: str = "hotkey") -> None:
        """Start a walkthrough, or finish the one being recorded."""
        session = self.controller.transcription_runtime.recording_walkthrough
        if session is not None:
            self._finish()
        elif self.controller.recorder.is_recording:
            self._tell(FINISH_RECORDING_FIRST)
        else:
            self._begin(source)

    def _begin(self, source: str) -> None:
        if self.controller._refuse_dictation_during_meeting():
            return
        unavailable = self.controller.transcription_readiness_message()
        if unavailable:
            self._tell(unavailable)
            return
        session = WalkthroughSession(started_at=datetime.now())
        # The writer thread can only reach the status line, a Qt signal.
        session.on_save_failed = lambda number: self.controller.status_update.emit(
            f"Screenshot {number} couldn't be saved — the walkthrough keeps recording"
        )
        if not self.controller.transcription_runtime.start_walkthrough(session, source=source):
            return
        self._active = True
        self._ui.set_walkthrough_active(True)
        # The pill keeps showing what is being recorded, but never in the
        # screenshots themselves.
        self._out_of_screenshots = self._ui.keep_overlay_out_of_screenshots(True)
        logger.info("Walkthrough started")

    def _finish(self) -> None:
        if self._snap is not None and self._snap.active:
            # Keep the screenshot being drawn, then stop.
            self._finish_after_snap = True
            self._snap.keep()
            return
        self.controller.stop_recording()

    # ---- the Screenshot shortcut ----

    def snap(self, source: str = "hotkey") -> None:
        """Take a screenshot: into the walkthrough being recorded, else onto the clipboard."""
        if self._snap is not None and self._snap.active:
            # Pressed again while drawing: keep it.
            self._snap.keep()
            return
        if source == "tray":
            self._snap_after_tray_closes()
            return
        self._snap_now()

    def _snap_after_tray_closes(self) -> None:
        if self._tray_timer is None:
            self._tray_timer = QTimer()
            self._tray_timer.setSingleShot(True)
            self._tray_timer.timeout.connect(self._snap_now)
        self._tray_timer.start(TRAY_SNAP_DELAY_MS)

    def _snap_now(self) -> None:
        supported, reason = self._ui.snap_support()
        if not supported:
            self._tell(reason)
            self._ui.request_snap_access()
            return
        session = self.controller.transcription_runtime.recording_walkthrough
        snap = self._ui.start_snap(hint=RECORDING_HINT if session is not None else "")
        self._snap, self._snap_for = snap, session
        snap.kept.connect(self._on_kept)
        snap.dropped.connect(self._on_dropped)
        snap.failed.connect(self._on_failed)

    def _on_kept(self, image) -> None:
        session, self._snap, self._snap_for = self._snap_for, None, None
        shot = None
        if session is not None:
            try:
                shot = session.add_shot(image, self.controller.recorder.get_recording_duration())
            except ScreenshotNotSaved as exc:
                # The recording goes on: its words are worth keeping even
                # where its screenshots can't be.
                self._tell(f"Couldn't save the screenshot ({exc.strerror or exc}) — "
                           "the walkthrough keeps recording")
                self._finish_if_asked()
                return
        if shot is not None:
            self.controller.recording_destination_changed.emit(shots_label(shot.number))
            self._ui.add_walkthrough_shot(shot.number, image)
            self._ui.show_overlay_caption(f"Screenshot {shot.number} added")
        else:
            # A plain screenshot, or the walkthrough stopped while this one
            # was being drawn: either way it goes to the clipboard.
            self._copy_screenshot(image, finished=session is not None)
        self._finish_if_asked()

    def _on_dropped(self) -> None:
        self._snap = self._snap_for = None
        self._finish_if_asked()

    def _on_failed(self, reason: str) -> None:
        self._snap = self._snap_for = None
        self._tell(reason or "Couldn't take a screenshot")
        self._finish_if_asked()

    def _finish_if_asked(self) -> None:
        if self._finish_after_snap:
            self._finish_after_snap = False
            if self.controller.transcription_runtime.recording_walkthrough is not None:
                self.controller.stop_recording()

    def _copy_screenshot(self, image, *, finished: bool) -> None:
        from services.walkthrough.storage import new_screenshot_path

        try:
            path = new_screenshot_path(datetime.now())
            saved = image.save(str(path), "PNG")
        except OSError:
            logger.exception("Could not create the screenshots folder")
            saved = False
        if not saved:
            # The image still reaches the clipboard; only the file is missing.
            logger.error("Could not save the screenshot file")
        copied = self._ui.copy_files_to_clipboard(
            [path] if saved else [], text=str(path) if saved else "", image=image,
        )
        unsaved = "" if saved else " (its file couldn't be saved)"
        if not copied:
            self._tell("Couldn't copy the screenshot" + unsaved)
        elif finished:
            self._tell(f"Screenshot copied{unsaved} — the walkthrough had already stopped",
                       done=saved)
        else:
            self._tell("Screenshot copied" + unsaved, done=saved)

    # ---- the recording ending ----

    def on_recording_state_changed(self, recording: bool) -> None:
        """Keep, or drop, a screenshot still being drawn when its walkthrough ends."""
        if recording or not self._active:
            return
        self._active = False
        self._ui.set_walkthrough_active(False)
        if self._out_of_screenshots:
            self._out_of_screenshots = False
            self._ui.keep_overlay_out_of_screenshots(False)
        if self._snap is not None and self._snap.active and self._snap_for is not None:
            if self.controller.recorder.capture_canceled is True:
                self._snap.drop()
            else:
                self._snap.keep()

    def _tell(self, text: str, *, done: bool = False) -> None:
        self._ui.set_status(text)
        self._ui.show_notice(text, done=done)
