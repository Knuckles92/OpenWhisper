"""The Walkthrough and Screenshot shortcuts (docs/walkthrough.md); Qt thread.

A walkthrough is a recording that collects screenshots: each one the user
keeps closes a section of the transcript. Its screenshots are of the kind
last picked from the column's Snap menu (a region, the window under the
pointer, the whole screen, or a region in 3 seconds). TranscriptionRuntime records and
transcribes it like a dictation and owns its WalkthroughSession; this
runtime decides what each shortcut press means, turns kept screenshots
into the session's shots, and answers the buttons on the walkthrough's
column (ui_qt/widgets/walkthrough_panel.py), which shows the session's
board as it changes.

Outside a walkthrough the Screenshot shortcut is a plain annotate-and-copy
screenshot tool.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from services._mouse_hook_win import MouseButtonHook
from services._mouse_monitor_mac import MouseClickMonitor
from services.settings import (
    SettingsKey,
    WalkthroughSnapMode,
    resolve_walkthrough_auto_mode,
    resolve_walkthrough_snap_mode,
    resolve_walkthrough_write_up,
    settings_manager,
)
from services.walkthrough.session import ScreenshotNotSaved, WalkthroughSession

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: What the snap overlay says while a walkthrough keeps recording.
RECORDING_HINT = "Recording continues — talk while you draw"
#: Lets the tray menu close before the screen is grabbed.
TRAY_SNAP_DELAY_MS = 300
FINISH_RECORDING_FIRST = "Finish the recording in progress first"
WAIT_FOR_WRITE_UP = "Wait for the last walkthrough's write-up, or cancel it"
#: How long "In 3 seconds" waits before it freezes the screen.
COUNTDOWN_S = 3
# Let the clicked app update and combine rapid clicks into one screenshot.
AUTO_SETTLE_MS = 350
_AUTO_MARK_HOTKEYS = {"keep_marks": "f8", "clear": "f9"}
_MARK_SHORTCUT_FAMILY = "walkthrough_marks"


def shots_label(count: int) -> str:
    """What the recording overlay says once ``count`` screenshots are kept."""
    from services.runtime.transcription import WALKTHROUGH_LABEL

    if count == 0:
        return WALKTHROUGH_LABEL
    return f"{WALKTHROUGH_LABEL} · {count} shot{'' if count == 1 else 's'}"


class WalkthroughRuntime(QObject):
    """Starts and finishes walkthroughs and takes their screenshots; Qt thread."""

    _click_received = pyqtSignal(object, int, int, float)
    _mark_shortcut_received = pyqtSignal(object, str)

    def __init__(self, controller: "ApplicationController"):
        super().__init__()
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
        # "In 3 seconds": ticks once a second until the snap.
        self._countdown = QTimer()
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._count_down)
        self._countdown_left = 0
        # The walkthrough's column, once made (none on Wayland); its buttons
        # are connected here once.
        self._panel = None
        # Who asked for the snap being taken (see snap's ``source``).
        self._source = "hotkey"
        self._auto_hook: MouseButtonHook | MouseClickMonitor | None = None
        self._live_drawing = None
        self._auto_snap = None
        self._auto_drawing_capture = False
        self._snap_after_drawing: str | None = None
        self._auto_position: tuple[int, int] | None = None
        # Ignore click signals queued before a pause or a manual screenshot.
        self._auto_after = 0.0
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._auto_timer.setInterval(AUTO_SETTLE_MS)
        self._auto_timer.timeout.connect(self._capture_click)
        self._click_received.connect(self._on_click, Qt.ConnectionType.QueuedConnection)
        self._mark_shortcut_received.connect(
            self._on_mark_shortcut, Qt.ConnectionType.QueuedConnection
        )

    @property
    def _ui(self):
        return self.controller.ui_controller

    @property
    def _session(self) -> Optional[WalkthroughSession]:
        return self.controller.transcription_runtime.recording_walkthrough

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
        if self.controller.transcription_runtime.writing_up:
            # The column is still showing the last one being written up.
            self._tell(WAIT_FOR_WRITE_UP)
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
        self._attach(self._ui.walkthrough_panel)
        settings = settings_manager.load_all_settings()
        if self._panel is not None:
            self._panel.set_snap_mode(resolve_walkthrough_snap_mode(settings),
                                      self._snap_shortcut())
            # Only an AI write-up reads notes.
            self._panel.set_notes_on(resolve_walkthrough_write_up(settings))
        # The pill keeps showing what is being recorded, but never in the
        # screenshots themselves.
        self._out_of_screenshots = self._ui.keep_overlay_out_of_screenshots(True)
        if sys.platform in ("win32", "darwin") and resolve_walkthrough_auto_mode(settings):
            self._start_auto(session, settings)
        logger.info("Walkthrough started")

    def _attach(self, panel) -> None:
        """Answer the column's buttons; it is made once and kept for every walkthrough."""
        if panel is None or panel is self._panel:
            return
        self._panel = panel
        # "ui" is the window and tray's source (catalog.SOURCES); the
        # column is the same user at the same desk.
        panel.snap_requested.connect(self._panel_snap)
        panel.snap_mode_chosen.connect(self.choose_snap_mode)
        panel.finish_requested.connect(self.finish)
        panel.cancel_requested.connect(self.cancel)
        panel.undo_requested.connect(self.undo_last_shot)
        panel.move_requested.connect(self.move_shot)
        panel.remove_requested.connect(self.remove_shot)
        panel.note_held.connect(self.hold_note)
        panel.pause_requested.connect(self.set_paused)

    def cancel(self) -> None:
        """The column's Cancel: its write-up, while one runs, else its walkthrough.

        A dictation made during the write-up is the user's Cancel to stop.
        """
        if not self.controller.transcription_runtime.cancel_write_up():
            self.controller.cancel()

    def finish(self) -> None:
        """Done: finish the walkthrough being recorded, if one still is."""
        if self._session is not None:
            self._finish()

    def _finish(self) -> None:
        if self._snap is not None and self._snap.active:
            # Keep the screenshot being drawn, then stop.
            self._finish_after_snap = True
            self._snap.keep()
            return
        if self._auto_position is not None or self._auto_snap is not None:
            self._finish_after_snap = True
            self._live_drawing.set_busy(True)
            return
        self.controller.stop_recording()

    # ---- Auto Mode ----

    def _start_auto(self, session: WalkthroughSession, settings: dict) -> None:
        supported, reason = self._ui.snap_support()
        if not supported:
            self._tell(reason)
            self._ui.request_snap_access()
            return

        def on_click(x, y, at):
            self._click_received.emit(session, x, y, at)

        if sys.platform == "darwin":
            hook = MouseClickMonitor(on_click=on_click)
        else:
            hook = MouseButtonHook(lambda *_: False, lambda *_: None, on_click=on_click)
        try:
            hook.start()
            if hook.wait_until_installed():
                self._auto_hook = hook
                self._start_auto_drawing(session, settings)
                self._live_drawing.arming.connect(self._discard_auto_capture)
                self._live_drawing.drawn.connect(self._capture_drawing)
                if self._panel is not None:
                    self._panel.set_auto_mode(True)
                return
        except Exception:
            logger.exception("Could not start walkthrough Auto Mode")
        if self._auto_hook is hook:
            self._stop_auto()
        else:
            hook.stop()
        self._tell("Auto Mode couldn't watch for clicks. You can still use Snap.")

    def _start_auto_drawing(self, session: WalkthroughSession, settings: dict) -> None:
        from services.hotkey_conflicts import hotkey_conflict
        from services.hotkey_manager import format_hotkey_display

        manager = self.controller.hotkey_manager
        shortcuts = {
            action: hotkey for action, hotkey in _AUTO_MARK_HOTKEYS.items()
            if manager is not None and not hotkey_conflict(
                hotkey, settings, standard_hotkeys=manager.hotkeys
            )
        }
        drawing = self._live_drawing = self._ui.start_walkthrough_drawing(
            mark_shortcuts={action: format_hotkey_display(key) for action, key in shortcuts.items()}
        )
        if manager is None or not shortcuts:
            return

        def on_shortcut(action: str) -> None:
            self._mark_shortcut_received.emit(session, action)

        def register(enabled: bool) -> None:
            manager.set_dynamic_hotkeys(
                _MARK_SHORTCUT_FAMILY,
                shortcuts if enabled else {},
                on_shortcut if enabled else lambda _: None,
            )

        drawing.enabled_changed.connect(register)
        register(True)

    def _on_mark_shortcut(self, session: WalkthroughSession, action: str) -> None:
        if self._live_drawing is not None and session is self._session:
            self._live_drawing.handle_mark_shortcut(action)

    def _auto_can_capture(self, *, drawing: bool = False) -> bool:
        return (self._auto_hook is not None and self._session is not None
                and not self.controller.recorder.paused and self._snap is None
                and self._auto_snap is None and not self._countdown_left
                and (drawing or not self._live_drawing.armed)
                and not (self._tray_timer is not None and self._tray_timer.isActive()))

    def _on_click(self, session: WalkthroughSession, x: int, y: int, at: float) -> None:
        if (not self._finish_after_snap and at >= self._auto_after
                and session is self._session and self._auto_can_capture()):
            self._auto_position = (x, y)
            self._auto_timer.start()

    def _capture_click(self) -> None:
        position, self._auto_position = self._auto_position, None
        self._auto_timer.stop()
        if position is None:
            return
        if not self._auto_can_capture():
            self._finish_if_asked()
            return
        self._capture_automatic(self._live_drawing.pinned_at(position), click_at=position)

    def _capture_drawing(self, screen, marks) -> None:
        if not self._finish_after_snap and self._auto_can_capture(drawing=True):
            self._auto_drawing_capture = True
            self._live_drawing.set_busy(True)
            self._capture_automatic(marks, screen=screen,
                                    kept_at_s=self.controller.recorder.get_recording_duration())

    def _capture_automatic(self, marks, *, kept_at_s: float | None = None, **location) -> None:
        session = self._session
        snap = self._ui.start_snap(take=WalkthroughSnapMode.SCREEN, **location)
        self._auto_snap = snap
        snap.kept.connect(lambda image: self._on_auto_outcome(snap, session, image=image,
                                                             marks=marks, kept_at_s=kept_at_s))
        snap.failed.connect(lambda reason: self._on_auto_outcome(snap, session, reason=reason))

    def _on_auto_outcome(self, snap, session: WalkthroughSession, *, image=None, marks=(),
                         kept_at_s: float | None = None, reason="") -> None:
        snap.deleteLater()
        if snap is not self._auto_snap:
            return
        self._auto_snap = None
        self._auto_drawing_capture = False
        next_snap, self._snap_after_drawing = self._snap_after_drawing, None
        if session is self._session:
            if image is not None:
                if marks:
                    from ui_qt.snap import export

                    image = export.render(image, export.logical_rect(image), marks)
                self._keep_walkthrough_shot(session, image, automatic=True, kept_at_s=kept_at_s)
            elif reason:
                self._tell(reason)
        self._finish_if_asked()
        if self._live_drawing is not None:
            self._live_drawing.set_busy(False)
            if next_snap is not None:
                self.snap(next_snap)

    def _discard_auto_capture(self) -> None:
        self._auto_after = time.monotonic()
        self._auto_timer.stop()
        self._auto_position = None
        self._auto_drawing_capture = False
        self._snap_after_drawing = None
        snap, self._auto_snap = self._auto_snap, None
        if snap is not None:
            if snap.active:
                snap.drop()
            snap.deleteLater()
        if self._live_drawing is not None:
            self._live_drawing.set_busy(False)

    def _stop_auto(self) -> None:
        self._discard_auto_capture()
        if self._live_drawing is not None:
            self._live_drawing = None
            self._ui.stop_walkthrough_drawing()
        hook, self._auto_hook = self._auto_hook, None
        if hook is not None:
            hook.stop()
            if self._panel is not None:
                self._panel.set_auto_mode(False)

    def cleanup(self) -> None:
        self._stop_auto()

    def _panel_snap(self) -> None:
        if self._finish_after_snap:
            return
        if self._live_drawing is not None and not self._countdown_left and self._snap is None:
            self._live_drawing.arm()
        else:
            self.snap("ui")

    def _restore_live_drawing(self) -> None:
        if (self._live_drawing is not None and not self._finish_after_snap
                and self._snap is None and not self._countdown_left
                and not (self._tray_timer is not None and self._tray_timer.isActive())):
            self._live_drawing.set_enabled(not self.controller.recorder.paused)

    # ---- the Screenshot shortcut ----

    def snap(self, source: str = "hotkey") -> None:
        """Take a screenshot: into the walkthrough being recorded, else onto the clipboard.

        In a walkthrough it is of the kind last picked from the Snap menu.
        """
        if self._finish_after_snap:
            return
        if self._auto_drawing_capture:
            self._snap_after_drawing = source
            return
        self._discard_auto_capture()
        if self._live_drawing is not None:
            self._live_drawing.set_enabled(False)
        if self._snap is not None and self._snap.active:
            # Pressed again while drawing: keep it.
            self._snap.keep()
            return
        if self._countdown_left:
            # Pressed again while counting down: don't wait.
            self._stop_countdown()
            self._snap_now(WalkthroughSnapMode.REGION)
            return
        self._source = source
        if source == "tray":
            self._snap_after_tray_closes()
            return
        self._snap_as_picked()

    def choose_snap_mode(self, mode: str) -> None:
        """A pick from the Snap menu: take it now, and from now on."""
        if mode not in WalkthroughSnapMode.ALL:
            return
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_SNAP_MODE, mode)
        if self._panel is not None:
            self._panel.set_snap_mode(mode)
        self.snap("ui")

    def _snap_mode(self) -> str:
        """What a snap takes: the kind last picked in a walkthrough, a region outside one."""
        if self._session is None:
            return WalkthroughSnapMode.REGION
        return resolve_walkthrough_snap_mode(settings_manager.load_all_settings())

    @staticmethod
    def _snap_shortcut() -> str:
        from services.hotkey_manager import format_hotkey_display

        return format_hotkey_display(settings_manager.load_hotkey_settings().get("screenshot") or "")

    def _snap_after_tray_closes(self) -> None:
        if self._tray_timer is None:
            self._tray_timer = QTimer()
            self._tray_timer.setSingleShot(True)
            self._tray_timer.timeout.connect(self._snap_as_picked)
        self._tray_timer.start(TRAY_SNAP_DELAY_MS)

    def _snap_as_picked(self) -> None:
        mode = self._snap_mode()
        if mode == WalkthroughSnapMode.DELAYED:
            self._countdown_left = COUNTDOWN_S
            self._show_countdown()
            self._countdown.start()
        else:
            self._snap_now(mode)

    def _count_down(self) -> None:
        self._countdown_left -= 1
        if self._countdown_left > 0:
            self._show_countdown()
            return
        self._stop_countdown()
        self._snap_now(WalkthroughSnapMode.REGION)

    def _show_countdown(self) -> None:
        if self._panel is not None:
            self._panel.set_countdown(self._countdown_left)
        self._ui.show_overlay_caption(f"Snapping in {self._countdown_left}…")

    def _stop_countdown(self) -> None:
        self._countdown.stop()
        self._countdown_left = 0
        if self._panel is not None:
            self._panel.set_countdown(0)

    def _snap_now(self, take: str = WalkthroughSnapMode.REGION) -> None:
        supported, reason = self._ui.snap_support()
        if not supported:
            self._tell(reason)
            self._ui.request_snap_access()
            self._restore_live_drawing()
            return
        session = self.controller.transcription_runtime.recording_walkthrough
        # Asked for from our own windows, "the window" is the one in front:
        # the pointer is on the column, the tray or the overlay, not on it.
        snap = self._ui.start_snap(hint=RECORDING_HINT if session is not None else "",
                                   take=take, front=self._source != "hotkey")
        self._snap, self._snap_for = snap, session
        snap.kept.connect(self._on_kept)
        snap.dropped.connect(self._on_dropped)
        snap.failed.connect(self._on_failed)

    def _on_kept(self, image) -> None:
        session, self._snap, self._snap_for = self._snap_for, None, None
        self._discard_auto_capture()
        if session is None or not self._keep_walkthrough_shot(session, image):
            # A plain screenshot, or the walkthrough stopped while this one
            # was being drawn: either way it goes to the clipboard.
            self._copy_screenshot(image, finished=session is not None)
        self._finish_if_asked()
        self._restore_live_drawing()

    def _keep_walkthrough_shot(self, session: WalkthroughSession, image, *,
                               automatic: bool = False, kept_at_s: float | None = None) -> bool:
        """Add to the storyboard. Return False when the session is closed."""
        try:
            if kept_at_s is None:
                kept_at_s = self.controller.recorder.get_recording_duration()
            shot = session.add_shot(image, kept_at_s)
        except ScreenshotNotSaved as exc:
            self._tell(f"Couldn't save the screenshot ({exc.strerror or exc}) — "
                       "the walkthrough keeps recording")
            return True
        if shot is None:
            return False
        count = session.shot_count
        self.controller.recording_destination_changed.emit(shots_label(count))
        if self._panel is not None:
            self._panel.add_shot(shot.number, image, shot.kept_at_s)
        if not automatic:
            self._ui.show_overlay_caption(f"Screenshot {count} added")
        return True

    def _on_dropped(self) -> None:
        self._snap = self._snap_for = None
        self._discard_auto_capture()
        self._finish_if_asked()
        self._restore_live_drawing()

    def _on_failed(self, reason: str) -> None:
        self._snap = self._snap_for = None
        self._discard_auto_capture()
        self._tell(reason or "Couldn't take a screenshot")
        self._finish_if_asked()
        self._restore_live_drawing()

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

    # ---- the column's board ----

    def undo_last_shot(self) -> None:
        """Take the newest screenshot out; its words join the section being spoken."""
        session = self._session
        number = session.undo_last() if session is not None else None
        if number is not None:
            self._shot_removed(session, number)

    def remove_shot(self, number: int) -> None:
        """Take screenshot ``number`` (as kept) out; its words join the next section."""
        session = self._session
        if session is not None and session.remove_shot(number):
            self._shot_removed(session, number)

    def move_shot(self, number: int, step: int) -> None:
        """Move screenshot ``number`` (as kept) and its words up (-1) or down (1)."""
        session = self._session
        if session is not None and session.move_shot(number, step) and self._panel is not None:
            self._panel.move_shot(number, step)

    def set_paused(self, paused: bool) -> None:
        """Pause the walkthrough's recording, or resume it; a pause is not in the recording."""
        from services.runtime.transcription import WALKTHROUGH_LABEL

        if self._session is None or self._finish_after_snap:
            return
        recorder = self.controller.recorder
        if paused and not recorder.pause():
            return
        if not paused:
            recorder.resume()
        self._discard_auto_capture()
        self._restore_live_drawing()
        if self._panel is not None:
            self._panel.set_paused(paused)
        self.controller.status_update.emit(
            f"{WALKTHROUGH_LABEL} · Paused" if paused else f"{WALKTHROUGH_LABEL} · Recording...")

    def hold_note(self, on: bool) -> None:
        """Hold for note pressed (``on``) or let go: what is said meanwhile is for the write-up."""
        session = self._session
        if session is None:
            return
        at_s = self.controller.recorder.get_recording_duration()
        if on:
            session.hold_note(at_s)
        else:
            session.release_note(at_s)

    def _shot_removed(self, session: WalkthroughSession, number: int) -> None:
        self.controller.recording_destination_changed.emit(shots_label(session.shot_count))
        if self._panel is not None:
            self._panel.remove_shot(number)

    # ---- the recording ending ----

    def on_recording_state_changed(self, recording: bool) -> None:
        """Keep, or drop, a screenshot still being drawn when its walkthrough ends."""
        if recording or not self._active:
            return
        self._active = False
        self._stop_auto()
        self._finish_after_snap = False
        if self._tray_timer is not None:
            self._tray_timer.stop()
        self._stop_countdown()
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
