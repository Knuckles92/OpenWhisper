"""Meeting Mode runtime for the application controller.

Thin Qt-side adapter that owns the ``MeetingEngine`` lifecycle and bridges
its listener events to ``ApplicationController`` signals (thread-safe via
``pyqtSignal.emit``). All ``meeting.*`` imports are lazy so the app starts
even when Meeting Mode dependencies are unavailable.
"""

from __future__ import annotations

import logging
import shutil
import threading
import webbrowser
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import urlsplit

from config import config
from services.components import meeting_agent_payload_dir, speaker_model_path
try:
    from services.settings import (
        MeetingAgentCore,
        SettingsKey,
        resolve_meeting_agent_core,
        resolve_meeting_llm_model,
        resolve_meeting_llm_provider,
        resolve_meeting_server_bind,
        resolve_meeting_server_port,
        resolve_meeting_whisper_model,
        settings_manager,
    )
except ImportError:  # pragma: no cover - supports lightweight test stubs
    from services.settings import SettingsKey, settings_manager

if TYPE_CHECKING:
    from meeting.engine import MeetingEngine
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: Seconds ``cleanup()`` waits for ``MeetingEngine.shutdown()`` before leaving
#: it to a daemon thread. The engine's own budget is minutes (ASR drain plus a
#: final consolidation round-trip) and cleanup runs after the window and tray
#: are gone, so a longer wait would only hang an invisible process.
SHUTDOWN_JOIN_TIMEOUT_S = 5.0


def redact_meeting_url(url: Optional[str]) -> str:
    """Return a dashboard URL with its capability token stripped.

    Dashboard URLs carry the host or guest token in the path, and both grant
    authority over the live meeting. Log files get attached to bug reports, so
    only the origin is ever written out.

    Args:
        url: Dashboard URL, or None.

    Returns:
        ``scheme://host:port/<redacted>``, or an empty string when no URL was
        given.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<redacted>"
    if not parts.scheme or not parts.netloc:
        return "<redacted>"
    return f"{parts.scheme}://{parts.netloc}/<redacted>"


class MeetingRuntime:
    """Owns Meeting Mode lifecycle and engine-to-Qt signal bridging."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._lock = threading.Lock()
        self._engine: Optional["MeetingEngine"] = None
        self._repo = None
        self._host_url: Optional[str] = None
        self._guest_url: Optional[str] = None
        # Claimed by start_meeting for the whole start attempt (including the
        # consent round-trip), before the engine exists to report itself.
        self._starting = False
        # Consent round-trip state: what to do when the consent dialog
        # resolves ("start" continues start_meeting, "toggle" applies the
        # cloud toggle). Mirrors the hf_consent_requested request/continuation
        # pattern in ApplicationController.
        self._consent_pending_kind: Optional[str] = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Kick off the startup crash-recovery scan for interrupted meetings.

        Called once the main UI is ready. The scan itself hits SQLite and the
        audio spool, so it runs on a worker thread; when interrupted meetings
        are found, ``meeting_recovery_found`` is emitted with the meeting
        dicts and the connected slot shows the recovery dialog on the Qt main
        thread.
        """
        threading.Thread(
            target=self._recovery_scan_worker,
            name="meeting-recovery-scan", daemon=True,
        ).start()

    def _recovery_scan_worker(self) -> None:
        """Scan for interrupted meetings off the Qt thread; report via signal."""
        try:
            from meeting.recovery import find_recoverable_meetings
        except Exception as exc:
            logger.debug(f"Meeting recovery module unavailable: {exc}")
            return

        try:
            try:
                meetings = find_recoverable_meetings(self._repository())
            except TypeError:
                # Tolerate a no-arg signature (module resolves its own
                # repository internally).
                meetings = find_recoverable_meetings()
        except Exception as exc:
            logger.error(f"Meeting recovery scan failed: {exc}")
            return

        if meetings:
            logger.info(f"Found {len(meetings)} interrupted meeting(s)")
            self.controller.meeting_recovery_found.emit(list(meetings))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """True while a meeting session is running (including paused)."""
        engine = self._engine
        try:
            return engine is not None and engine.is_active()
        except Exception:
            return False

    def start_meeting(self, cloud_enabled: Optional[bool] = None) -> None:
        """Start a meeting session (UI/tray callback target).

        Args:
            cloud_enabled: Explicit cloud-intelligence choice, or None to use
                the remembered per-meeting toggle.
        """
        # ``is_active`` is engine-derived and stays False for the seconds
        # ``_start_worker`` takes, so the authoritative guard is the
        # controller's exclusive-mode flag plus a claim held across the consent
        # round-trip. Without it a second launch would build a second engine —
        # second Whisper model, second web server — and leak the first.
        with self._lock:
            busy = self._starting or self.controller.meeting_active or self.is_active
            if not busy:
                self._starting = True
        if busy:
            self.controller.meeting_status_update.emit(
                "A meeting is already in progress"
            )
            return
        if self.controller.recorder.is_recording:
            with self._lock:
                self._starting = False
            self.controller.status_update.emit(
                "Stop recording before starting a meeting"
            )
            return

        if cloud_enabled is None:
            cloud = bool(
                settings_manager.get(SettingsKey.MEETING_CLOUD_LAST_ENABLED, False)
            )
        else:
            cloud = bool(cloud_enabled)

        if cloud and not self._cloud_consent_given():
            # One-time informed consent before any transcript leaves the
            # machine; the controller shows the dialog on the Qt main thread
            # and calls back into on_consent_result.
            self._consent_pending_kind = "start"
            self.controller.meeting_consent_requested.emit()
            return

        self._launch(cloud)

    def on_consent_result(self, granted: bool) -> None:
        """Continue the pending action after the consent dialog resolves.

        Args:
            granted: True when the user enabled cloud intelligence.
        """
        kind, self._consent_pending_kind = self._consent_pending_kind, None
        if kind == "start":
            # A declined consent still starts the meeting — transcript-only.
            # _launch releases the start claim taken by start_meeting.
            self._launch(cloud=granted)
            return

        # Nothing is waiting to start on this result; drop any stale claim so
        # a later Start Meeting is not blocked forever.
        with self._lock:
            self._starting = False
        if kind == "toggle":
            if granted:
                self._apply_cloud(True)
            else:
                # The panel checkbox is already ticked; put it back so it
                # cannot claim cloud is on while it is off (and re-prompt on
                # the next start). set_meeting_state blocks the checkbox's
                # signals, so this cannot loop back into toggle_cloud.
                self.controller.meeting_state_changed.emit(
                    {"cloud_enabled": False}
                )
                self.controller.meeting_status_update.emit(
                    "Cloud intelligence stays off"
                )
        else:
            logger.debug("Consent result received with no pending meeting action")

    def _launch(self, cloud: bool) -> None:
        """Kick off the meeting start on a dedicated thread.

        ``MeetingEngine.start()`` loads a Whisper model and boots the web
        server, so it must not run on the Qt main thread. ``meeting_active``
        is raised immediately so dictation is excluded during startup.
        """
        try:
            settings_manager.save_setting(
                SettingsKey.MEETING_CLOUD_LAST_ENABLED, cloud
            )
        except Exception as exc:
            logger.warning(f"Could not persist meeting cloud toggle: {exc}")

        self.controller.meeting_active = True
        with self._lock:
            # Exclusive mode now guards the start; release the claim.
            self._starting = False
        self.controller.meeting_status_update.emit("Starting meeting...")
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": False, "status": "starting",
             "cloud_enabled": cloud}
        )
        threading.Thread(
            target=self._start_worker, args=(cloud,),
            name="meeting-start", daemon=True,
        ).start()

    def _start_worker(self, cloud: bool) -> None:
        """Build and start the engine off the Qt thread; report via signals."""
        try:
            from meeting.engine import MeetingEngine

            self._shutdown_engine()  # drop a previous (ended) session's server
            options = self._build_options(cloud)
            engine = MeetingEngine(options, repository=self._repository())
            engine.add_listener(self._on_engine_event)
            # Publish before starting: start() takes seconds to bring up
            # capture, ASR, the web server and the agent, and a cleanup()
            # landing in that window would otherwise find no engine and
            # orphan all of them. MeetingEngine.shutdown() waits out an
            # in-flight start, so an early handle is safe.
            with self._lock:
                self._engine = engine
            result = engine.start()
        except Exception as exc:
            logger.error("Failed to start meeting", exc_info=True)
            with self._lock:
                if self._engine is engine:
                    self._engine = None
            self.controller.meeting_active = False
            self.controller.meeting_error.emit(f"Could not start the meeting: {exc}")
            self.controller.meeting_state_changed.emit(
                {"active": False, "status": "failed"}
            )
            return

        with self._lock:
            self._engine = engine
            # The engine's server_started event usually landed first (and
            # already announced the URLs); keep those values when the start
            # result omits them.
            self._host_url = (
                result.get("host_url") or result.get("url") or self._host_url
            )
            self._guest_url = result.get("guest_url") or self._guest_url

        # No meeting_server_started emit here: the engine's own server_started
        # event already carried the URLs to the UI, and a second emit opens a
        # second dashboard tab.
        self.controller.meeting_status_update.emit("Meeting in progress")
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": False, "status": "active",
             "cloud_enabled": cloud, "elapsed_s": 0.0}
        )
        logger.info(f"Meeting started: {result.get('meeting_id')}")

    def _build_options(self, cloud: bool):
        """Compose ``MeetingEngineOptions`` from settings, config, and components."""
        from meeting.engine import MeetingEngineOptions

        settings = settings_manager.load_all_settings()
        agent_kind = resolve_meeting_agent_core(settings)
        payload_dir = meeting_agent_payload_dir()
        if agent_kind == MeetingAgentCore.PI and payload_dir is None:
            logger.info(
                "Meeting agent component not installed; using the direct "
                "OpenRouter core"
            )
            agent_kind = MeetingAgentCore.DIRECT

        return MeetingEngineOptions(
            title="",
            cloud_enabled=cloud,
            mic_device_id=settings_manager.load_audio_input_device(),
            asr_model=resolve_meeting_whisper_model(settings),
            llm_provider=resolve_meeting_llm_provider(settings),
            llm_model=resolve_meeting_llm_model(settings),
            agent_core_kind=agent_kind,
            sidecar_payload_dir=payload_dir,
            diarization_model_path=speaker_model_path(),
            server_bind=resolve_meeting_server_bind(settings),
            server_port=resolve_meeting_server_port(settings),
            spool_root=config.MEETINGS_FOLDER,
        )

    def pause_meeting(self) -> None:
        """Pause the active meeting (UI callback target)."""
        engine = self._engine
        if engine is None or not self.is_active:
            return
        try:
            engine.pause()
        except Exception as exc:
            logger.error(f"Failed to pause meeting: {exc}")
            self.controller.meeting_error.emit(f"Could not pause the meeting: {exc}")
            return
        self.controller.meeting_status_update.emit("Meeting paused")
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": True, "status": "paused"}
        )

    def resume_meeting(self) -> None:
        """Resume a paused meeting (UI callback target)."""
        engine = self._engine
        if engine is None or not self.is_active:
            return
        try:
            engine.resume()
        except Exception as exc:
            logger.error(f"Failed to resume meeting: {exc}")
            self.controller.meeting_error.emit(f"Could not resume the meeting: {exc}")
            return
        self.controller.meeting_status_update.emit("Meeting in progress")
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": False, "status": "active"}
        )

    def end_meeting(self) -> None:
        """End the meeting and finalize it (UI/tray callback target).

        ``MeetingEngine.end()`` is asynchronous internally (stops capture,
        drains ASR, runs consolidation); the ``ended`` listener event marks
        completion and releases exclusive mode.
        """
        engine = self._engine
        if engine is None or not self.is_active:
            return
        try:
            engine.end()
        except Exception as exc:
            logger.error(f"Failed to end meeting: {exc}")
            self.controller.meeting_error.emit(f"Could not end the meeting: {exc}")
            return
        self.controller.meeting_status_update.emit(
            "Ending meeting — finishing transcription..."
        )
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": False, "status": "ending"}
        )

    def cancel_meeting(self) -> None:
        """Discard the active meeting session (UI callback target)."""
        engine = self._engine
        if engine is None:
            return
        self.controller.meeting_status_update.emit("Canceling meeting...")
        threading.Thread(
            target=self._cancel_worker, name="meeting-cancel", daemon=True
        ).start()

    def _cancel_worker(self) -> None:
        """Cancel and tear the engine down off the Qt thread."""
        try:
            engine = self._engine
            if engine is not None:
                engine.cancel()
            self._shutdown_engine()
        except Exception as exc:
            logger.error(f"Failed to cancel meeting: {exc}")
        finally:
            self.controller.meeting_active = False
            self.controller.meeting_status_update.emit("Meeting canceled")
            self.controller.meeting_state_changed.emit(
                {"active": False, "status": "canceled"}
            )

    def open_dashboard(self) -> None:
        """Open the host dashboard in the default browser (UI callback target)."""
        url = self._host_url
        if not url:
            self.controller.meeting_status_update.emit(
                "No meeting dashboard available yet"
            )
            return
        # Never log the path: it carries the host token, which grants full
        # host authority over the meeting.
        logger.info(f"Opening meeting dashboard: {redact_meeting_url(url)}")
        webbrowser.open(url)

    def copy_guest_link(self) -> None:
        """Announce the guest URL for clipboard copy (UI callback target).

        The actual clipboard write happens on the Qt main thread via the
        ``meeting_guest_link_ready`` signal.
        """
        url = self._guest_url
        if not url:
            self.controller.meeting_status_update.emit("No guest link available yet")
            return
        self.controller.meeting_guest_link_ready.emit(url)

    def toggle_cloud(self, enabled: bool) -> None:
        """Enable or disable cloud intelligence (UI callback target).

        Args:
            enabled: Desired cloud-intelligence state.
        """
        enabled = bool(enabled)
        if enabled and not self._cloud_consent_given():
            self._consent_pending_kind = "toggle"
            self.controller.meeting_consent_requested.emit()
            return
        self._apply_cloud(enabled)

    def _apply_cloud(self, enabled: bool) -> None:
        """Persist and apply the cloud-intelligence toggle."""
        try:
            settings_manager.save_setting(
                SettingsKey.MEETING_CLOUD_LAST_ENABLED, enabled
            )
        except Exception as exc:
            logger.warning(f"Could not persist meeting cloud toggle: {exc}")

        engine = self._engine
        if engine is not None and self.is_active:
            try:
                engine.set_cloud_enabled(enabled)
            except Exception as exc:
                logger.error(f"Failed to apply cloud toggle: {exc}")
                self.controller.meeting_error.emit(
                    f"Could not change cloud intelligence: {exc}"
                )
                return

        self.controller.meeting_state_changed.emit({"cloud_enabled": enabled})
        self.controller.meeting_status_update.emit(
            "Cloud intelligence enabled" if enabled
            else "Cloud intelligence disabled"
        )

    def _cloud_consent_given(self) -> bool:
        return bool(
            settings_manager.get(SettingsKey.MEETING_CLOUD_CONSENT_GIVEN, False)
        )

    # ------------------------------------------------------------------
    # Crash recovery actions (recovery dialog targets)
    # ------------------------------------------------------------------

    def finalize_recovered(self, meeting_id: str) -> None:
        """Finalize an interrupted meeting from the recovery dialog.

        Args:
            meeting_id: Interrupted meeting session id.
        """
        threading.Thread(
            target=self._finalize_recovered_worker, args=(meeting_id,),
            name="meeting-recover", daemon=True,
        ).start()

    def _finalize_recovered_worker(self, meeting_id: str) -> None:
        try:
            repository = self._repository()
            finalize = None
            try:
                from meeting import recovery as recovery_module
                finalize = getattr(recovery_module, "finalize_meeting", None)
            except Exception:
                pass
            if finalize is not None:
                # finalize_meeting expects the meeting dict (not the id).
                meeting = repository.get_meeting(meeting_id)
                if meeting is None:
                    raise ValueError(f"Unknown meeting '{meeting_id}'")
                if not finalize(repository, meeting):
                    self.controller.meeting_error.emit(
                        "Could not finalize the meeting — "
                        "transcription may still be pending"
                    )
                    return
            else:
                # Minimal honest fallback: mark the session ended so it shows
                # up in history instead of the recovery list.
                repository.update_meeting(
                    meeting_id, status="ended",
                    ended_at=datetime.now().isoformat(),
                )
            self.controller.meeting_status_update.emit(
                "Interrupted meeting finalized"
            )
        except Exception as exc:
            logger.error(f"Failed to finalize meeting '{meeting_id}': {exc}")
            self.controller.meeting_error.emit(
                f"Could not finalize the meeting: {exc}"
            )

    def discard_recovered(self, meeting_id: str) -> None:
        """Delete an interrupted meeting and its audio spool.

        Args:
            meeting_id: Interrupted meeting session id.
        """
        threading.Thread(
            target=self._discard_recovered_worker, args=(meeting_id,),
            name="meeting-discard", daemon=True,
        ).start()

    def _discard_recovered_worker(self, meeting_id: str) -> None:
        try:
            repository = self._repository()
            meeting = repository.get_meeting(meeting_id)
            repository.delete_meeting(meeting_id)
            spool_dir = (meeting or {}).get("spool_dir")
            if spool_dir:
                shutil.rmtree(spool_dir, ignore_errors=True)
            self.controller.meeting_status_update.emit(
                "Interrupted meeting discarded"
            )
        except Exception as exc:
            logger.error(f"Failed to discard meeting '{meeting_id}': {exc}")
            self.controller.meeting_error.emit(
                f"Could not discard the meeting: {exc}"
            )

    # ------------------------------------------------------------------
    # Engine event bridge (invoked from engine threads)
    # ------------------------------------------------------------------

    def _on_engine_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """Bridge one engine listener event to controller signals.

        Runs on engine worker threads; only thread-safe ``pyqtSignal.emit``
        calls are allowed here.
        """
        try:
            payload = payload or {}
            if kind == "status":
                status = payload.get("status", "")
                if status:
                    self.controller.meeting_status_update.emit(str(status))
            elif kind == "server_started":
                with self._lock:
                    self._host_url = payload.get("host_url") or self._host_url
                    self._guest_url = payload.get("guest_url") or self._guest_url
                self.controller.meeting_server_started.emit(dict(payload))
            elif kind == "error":
                message = payload.get("message") or str(payload)
                self.controller.meeting_error.emit(str(message))
            elif kind == "ended":
                self.controller.meeting_active = False
                self.controller.meeting_status_update.emit("Meeting ended")
                self.controller.meeting_state_changed.emit(
                    {"active": False, "status": "ended"}
                )
            elif kind == "intelligence":
                online = bool(
                    payload.get("online", payload.get("intelligence_online", False))
                )
                self.controller.meeting_status_update.emit(
                    "Meeting intelligence online" if online
                    else "Meeting intelligence offline — transcript-only"
                )
            elif kind == "segments":
                # Segments stream to the browser dashboard; the Qt panel does
                # not render them.
                pass
            else:
                logger.debug(f"Unhandled meeting engine event: {kind}")
        except Exception as exc:  # never propagate into engine threads
            logger.error(f"Error bridging meeting event '{kind}': {exc}")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _shutdown_engine(self, timeout: Optional[float] = None) -> None:
        """Shut down and drop the current engine, if any.

        Args:
            timeout: Seconds to wait for the shutdown. When given, the
                shutdown runs on a daemon thread and is abandoned once the
                budget expires; None blocks until the engine is down.
        """
        with self._lock:
            engine, self._engine = self._engine, None
            self._host_url = None
            self._guest_url = None
        if engine is None:
            return

        def shutdown_engine():
            try:
                engine.shutdown()
            except Exception as exc:
                logger.debug(f"Error shutting down meeting engine: {exc}")

        if timeout is None:
            shutdown_engine()
            return

        worker = threading.Thread(
            target=shutdown_engine, name="meeting-shutdown", daemon=True
        )
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            logger.warning(
                f"Meeting engine shutdown still running after {timeout:.1f}s; "
                "abandoning it so the application can exit"
            )

    def _repository(self):
        """Lazily build the shared SQL meeting repository."""
        if self._repo is None:
            from meeting.persist.repository import SqlMeetingRepository

            self._repo = SqlMeetingRepository()
        return self._repo

    def cleanup(self) -> None:
        """Release the meeting engine on application shutdown.

        Cleanup runs after ``app.exec()`` returns, so the window and tray are
        already gone: a full ``MeetingEngine.shutdown()`` (ASR drain plus a
        final consolidation pass) would leave an invisible process lingering
        for minutes. The wait is bounded instead — the meeting is already
        persisted, so only the tail of a consolidation pass is at risk.
        """
        self._shutdown_engine(timeout=SHUTDOWN_JOIN_TIMEOUT_S)
        self.controller.meeting_active = False
