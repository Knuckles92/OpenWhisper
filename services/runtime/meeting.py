"""Meeting Mode runtime for the application controller.

Thin Qt-side adapter that owns the ``MeetingEngine`` lifecycle and bridges
its listener events to ``ApplicationController`` signals (thread-safe via
``pyqtSignal.emit``). All ``meeting.*`` imports are lazy so the app starts
even when Meeting Mode dependencies are unavailable.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config import config
from services.components import (
    ensure_speaker_model,
    meeting_agent_payload_dir,
    speaker_model_path,
)
try:
    from services.settings import (
        MeetingAgentCore,
        SettingsKey,
        resolve_meeting_agent_core,
        resolve_meeting_audio_upload_consent,
        resolve_meeting_end_polish,
        resolve_meeting_end_redecode,
        resolve_meeting_end_report,
        resolve_meeting_report_views,
        resolve_meeting_llm_endpoint,
        resolve_meeting_llm_model,
        resolve_meeting_llm_provider,
        resolve_meeting_language,
        resolve_meeting_server_bind,
        resolve_meeting_server_port,
        resolve_meeting_speaker_id_backend,
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


def _with_history_target(url: str, meeting_id: str) -> str:
    """Add a past-meeting deep link without changing the capability path."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["history"] = meeting_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


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
        self._archive_dashboard = None
        self._archive_starting = False
        # Claimed by start_meeting for the whole start attempt (including the
        # consent round-trip), before the engine exists to report itself.
        self._starting = False
        # True while post-meeting cloud finalization is still running. Blocks a
        # second Meeting Mode session only — not Quick Record / dictation.
        self._finalizing = False
        self._finalization: Optional[Dict[str, Any]] = None
        # Meeting whose checklist is currently shown on the Meeting Mode tab.
        self._card_meeting_id: Optional[str] = None
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
        threading.Thread(
            target=self._restore_last_finalization_worker,
            name="meeting-restore-finalization", daemon=True,
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

    def _restore_last_finalization_worker(self) -> None:
        """Reload the most recent meeting's checklist onto the Meeting Mode tab."""
        if self.is_active or self.controller.meeting_active:
            return
        try:
            repo = self._repository()
            meetings = repo.list_meetings()
        except Exception:
            logger.exception("Could not restore the last meeting checklist")
            return
        for meeting in meetings:
            status = str(meeting.get("status") or "")
            if status in {"active", "paused", "ending"}:
                continue
            if self._hydrate_finalization_card(meeting):
                return

    def _hydrate_finalization_card(
        self,
        meeting: Dict[str, Any],
        *,
        reveal: bool = False,
    ) -> bool:
        """Show a persisted meeting's finalization payload on the desktop tab.

        Args:
            meeting: Repository meeting row.
            reveal: When True, clear a persisted Keep-for-later flag and show
                the card even if the user previously dismissed it.

        Returns:
            True when a card payload was emitted.
        """
        meeting_id = str(meeting.get("id") or "")
        raw = meeting.get("state_json")
        data: Dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                data = parsed
        try:
            from meeting.state.schema import FinalizationState

            fin = FinalizationState.normalize_historical(
                data.get("finalization"),
                cloud_enabled=bool(
                    data.get("cloud_enabled", meeting.get("cloud_enabled"))
                ),
                meeting_status=str(meeting.get("status") or "ended"),
            )
        except Exception:
            logger.exception(
                "Could not coerce finalization for meeting %s", meeting_id
            )
            return False
        if fin.card_deferred and not reveal:
            return False
        if reveal and fin.card_deferred:
            self._persist_card_deferred(meeting_id, False)
            fin.card_deferred = False
            self._refresh_past_meetings()
        if fin.status in {"pending"} and not fin.steps:
            return False
        payload = fin.to_dict()
        payload["content_summary"] = self._meeting_content_summary(
            meeting_id, meeting=meeting
        )
        with self._lock:
            self._card_meeting_id = meeting_id
            self._finalization = payload
            self._finalizing = False
        self.controller.meeting_state_changed.emit({
            "active": False,
            "meeting_id": meeting_id,
            "status": str(meeting.get("status") or "ended"),
            "finalization": payload,
        })
        return True

    def _persist_card_deferred(self, meeting_id: str, deferred: bool) -> bool:
        """Write the desktop-card deferral flag without changing insights.

        Args:
            meeting_id: Persisted meeting session id.
            deferred: True hides the card across restarts until the meeting is
                selected from Past Meetings.

        Returns:
            True when the flag was persisted.
        """
        from meeting.state.schema import FinalizationState, MeetingState

        engine = self._engine
        store = getattr(engine, "store", None) if engine is not None else None
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            try:
                current = store.with_state(lambda state: state.finalization)
                cloud, status = store.with_state(
                    lambda state: (state.cloud_enabled, state.status)
                )
                payload = (
                    current.to_dict()
                    if hasattr(current, "to_dict")
                    else dict(current or {})
                )
                payload["card_deferred"] = bool(deferred)
                updated = FinalizationState.coerce(
                    payload,
                    cloud_enabled=bool(cloud),
                    meeting_status=str(status or "ended"),
                )
                return bool(store.update_runtime_fields(finalization=updated))
            except Exception:
                logger.exception(
                    "Could not persist card deferral on the live store"
                )
                return False

        try:
            repo = self._repository()
            meeting = repo.get_meeting(meeting_id)
            if meeting is None:
                return False
            raw = meeting.get("state_json")
            data: Dict[str, Any] = {}
            if raw:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    data = parsed
            data.setdefault("meeting_id", meeting_id)
            data.setdefault("status", meeting.get("status") or "ended")
            data.setdefault(
                "cloud_enabled", bool(meeting.get("cloud_enabled", False))
            )
            data.setdefault("title", meeting.get("title") or "")
            state = MeetingState.from_dict(data)
            state.finalization.card_deferred = bool(deferred)
            repo.persist_state(meeting_id, state.to_dict())
            return True
        except Exception:
            logger.exception(
                "Could not persist card deferral for meeting %s", meeting_id
            )
            return False

    def _hide_finalization_card(self) -> None:
        """Clear the in-memory Final Insights card and refresh Past Meetings."""
        with self._lock:
            self._finalization = None
            self._card_meeting_id = None
        self.controller.meeting_state_changed.emit({
            "active": False,
            "finalization": None,
            "meeting_id": None,
        })
        self._refresh_past_meetings()

    def _refresh_past_meetings(self) -> None:
        """Reload the Past Meetings sidebar when the Qt window is available."""
        try:
            self.controller.ui_controller.main_window.refresh_past_meetings()
        except Exception:
            pass

    def defer_finalization_card(self) -> bool:
        """Persist Keep for later and hide the desktop Final Insights card.

        The meeting, transcript, and audio stay in Past Meetings. The card
        stays hidden across restarts until that meeting is selected again.

        Returns:
            True when the card was hidden. False while finalization is still
            running or no meeting is attached to the card.
        """
        with self._lock:
            finalizing = self._finalizing
            status = str((self._finalization or {}).get("status") or "")
            meeting_id = self._card_meeting_id
            busy = (
                self._starting
                or self.controller.meeting_active
                or self.is_active
            )
        if finalizing or status == "running":
            self.controller.meeting_status_update.emit(
                "Final insights are still being prepared."
            )
            return False
        if busy:
            self.controller.meeting_status_update.emit(
                "A meeting is already in progress"
            )
            return False
        if not meeting_id:
            logger.warning("Cannot defer finalization card: no meeting selected")
            return False
        if not self._persist_card_deferred(meeting_id, True):
            self.controller.meeting_error.emit(
                "Could not save this meeting for later"
            )
            return False
        self._hide_finalization_card()
        self.controller.meeting_status_update.emit(
            "Meeting saved for later. Open it from Past Meetings when you "
            "want to continue."
        )
        return True

    def start_new_meeting(self, cloud_enabled: Optional[bool]) -> None:
        """Defer the shown incomplete card, then start a new session.

        Args:
            cloud_enabled: Explicit cloud-intelligence choice, or None to use
                the remembered per-meeting toggle.
        """
        with self._lock:
            finalizing = self._finalizing
            status = str((self._finalization or {}).get("status") or "")
        if finalizing or status == "running":
            self.controller.meeting_status_update.emit(
                "Final insights are still being prepared."
            )
            return
        if self._card_meeting_id:
            if not self.defer_finalization_card():
                return
        self._begin_start(cloud_enabled, demo=False)

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

    @property
    def is_claimed(self) -> bool:
        """True during consent/startup as well as an active meeting.

        Post-meeting finalization is intentionally excluded so Quick Record
        and rule dictation unlock once capture ends.
        """
        with self._lock:
            return self._starting or self.controller.meeting_active or self.is_active

    @property
    def is_finalizing(self) -> bool:
        """True while optional post-meeting cloud finalization is running."""
        with self._lock:
            return self._finalizing

    def start_meeting(self, cloud_enabled: Optional[bool] = None) -> None:
        """Start a meeting session (UI/tray callback target).

        Args:
            cloud_enabled: Explicit cloud-intelligence choice, or None to use
                the remembered per-meeting toggle.
        """
        self._begin_start(cloud_enabled, demo=False)

    def start_demo_meeting(self, cloud_enabled: Optional[bool] = None) -> None:
        """Start a developer-mode meeting with canned transcript data.

        Skips microphone, system audio, and Whisper so End can be tested
        against polish and the final report without a live recording.

        Args:
            cloud_enabled: Explicit cloud-intelligence choice, or None to use
                the remembered per-meeting toggle.
        """
        self._begin_start(cloud_enabled, demo=True)

    def _begin_start(
        self,
        cloud_enabled: Optional[bool],
        *,
        demo: bool,
    ) -> None:
        """Claim exclusive mode, resolve consent, and launch the engine.

        Args:
            cloud_enabled: Explicit cloud-intelligence choice, or None to use
                the remembered per-meeting toggle.
            demo: When True, seed a fake transcript instead of capturing audio.
        """
        # ``is_active`` is engine-derived and stays False for the seconds
        # ``_start_worker`` takes, so the authoritative guard is the
        # controller's exclusive-mode flag plus a claim held across the consent
        # round-trip. Without it a second launch would build a second engine —
        # second Whisper model, second web server — and leak the first.
        with self._lock:
            finalizing = self._finalizing
            busy = self._starting or self.controller.meeting_active or self.is_active
            if not busy and not finalizing:
                self._starting = True
        if finalizing:
            self.controller.meeting_status_update.emit(
                "Final insights are still being prepared."
            )
            return
        if busy:
            self.controller.meeting_status_update.emit(
                "A meeting is already in progress"
            )
            return
        backend = getattr(self.controller, "current_backend", None)
        if (self.controller.recorder.is_recording
                or bool(getattr(backend, "is_transcribing", False))):
            with self._lock:
                self._starting = False
            self.controller.status_update.emit(
                "Finish recording or transcription before starting a meeting"
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
            self._consent_pending_kind = "start_demo" if demo else "start"
            self.controller.meeting_consent_requested.emit()
            return

        self._launch(cloud, demo=demo)

    def on_consent_result(self, granted: bool) -> None:
        """Continue the pending action after the consent dialog resolves.

        Args:
            granted: True when the user enabled cloud intelligence.
        """
        kind, self._consent_pending_kind = self._consent_pending_kind, None
        if kind in ("start", "start_demo"):
            # A declined consent still starts the meeting — transcript-only.
            # _launch releases the start claim taken by start_meeting.
            self._launch(cloud=granted, demo=(kind == "start_demo"))
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

    def _launch(self, cloud: bool, *, demo: bool = False) -> None:
        """Kick off the meeting start on a dedicated thread.

        ``MeetingEngine.start()`` loads a Whisper model and boots the web
        server, so it must not run on the Qt main thread. The runtime's
        ``_starting`` claim excludes dictation without telling the UI that a
        meeting is active before required startup checks have passed.

        Args:
            cloud: Whether cloud intelligence should start with the meeting.
            demo: When True, seed canned transcript data instead of capturing.
        """
        try:
            settings_manager.save_setting(
                SettingsKey.MEETING_CLOUD_LAST_ENABLED, cloud
            )
        except Exception as exc:
            logger.warning(f"Could not persist meeting cloud toggle: {exc}")

        self.controller.meeting_active = False
        with self._lock:
            # A new meeting replaces any retained post-meeting finalization.
            self._finalizing = False
            self._finalization = None
            self._card_meeting_id = None
        self.controller.meeting_status_update.emit(
            "Starting demo meeting..." if demo else "Starting meeting..."
        )
        self.controller.meeting_state_changed.emit(
            {"active": False, "paused": False, "status": "starting",
             "cloud_enabled": cloud, "finalization": None}
        )
        threading.Thread(
            target=self._start_worker, args=(cloud, demo),
            name="meeting-start", daemon=True,
        ).start()

    def _start_worker(self, cloud: bool, demo: bool = False) -> None:
        """Build and start the engine off the Qt thread; report via signals."""
        engine = None
        try:
            from meeting.engine import MeetingEngine

            self._shutdown_archive_dashboard()
            self._shutdown_engine()  # drop a previous (ended) session's server
            if not demo and speaker_model_path() is None:
                self.controller.meeting_status_update.emit(
                    "Downloading speaker identification model..."
                )
            options = self._build_options(cloud, demo=demo)
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
            if not (result.get("host_url") or result.get("url")):
                abort = getattr(engine, "_abort_start", None)
                if callable(abort):
                    abort()
                raise RuntimeError(
                    "Meeting dashboard did not provide an address."
                )
        except Exception as exc:
            logger.error("Failed to start meeting", exc_info=True)
            with self._lock:
                self._starting = False
                if engine is not None and self._engine is engine:
                    self._engine = None
                self._host_url = None
                self._guest_url = None
            self.controller.meeting_active = False
            self.controller.meeting_error.emit(f"Could not start the meeting: {exc}")
            self.controller.meeting_state_changed.emit(
                {
                    "active": False,
                    "paused": False,
                    "status": "failed",
                    "dashboard_available": False,
                }
            )
            return

        with self._lock:
            self._starting = False
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
        self.controller.meeting_status_update.emit(
            "Demo meeting loaded — End to test cleanup and the report"
            if demo else "Meeting in progress"
        )
        self.controller.meeting_active = True
        elapsed_s = 0.0
        if demo:
            try:
                elapsed_s = float(engine.clock.now_s())
            except Exception:
                elapsed_s = 0.0
        self.controller.meeting_state_changed.emit(
            {"active": True, "paused": False, "status": "active",
             "cloud_enabled": cloud, "elapsed_s": elapsed_s}
        )
        logger.info(
            "Demo meeting started: %s" if demo else "Meeting started: %s",
            result.get("meeting_id"),
        )

    def _build_options(self, cloud: bool, *, demo: bool = False):
        """Compose ``MeetingEngineOptions`` from settings, config, and components.

        Args:
            cloud: Whether cloud intelligence is enabled for this session.
            demo: When True, skip live audio and seed canned transcript data.
        """
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
            title="Demo Planning Sync" if demo else "",
            cloud_enabled=cloud,
            mic_device_id=settings_manager.load_audio_input_device(),
            asr_model=resolve_meeting_whisper_model(settings),
            asr_language=resolve_meeting_language(settings),
            llm_provider=resolve_meeting_llm_provider(settings),
            llm_model=resolve_meeting_llm_model(settings),
            llm_endpoint=resolve_meeting_llm_endpoint(settings),
            agent_core_kind=agent_kind,
            sidecar_payload_dir=payload_dir,
            diarization_model_path=(
                None if demo else ensure_speaker_model()
            ),
            speaker_id_backend=(
                "local" if demo
                else resolve_meeting_speaker_id_backend(settings)
            ),
            speaker_id_audio_consent=(
                False if demo
                else resolve_meeting_audio_upload_consent(settings)
            ),
            server_bind=resolve_meeting_server_bind(settings),
            server_port=resolve_meeting_server_port(settings),
            spool_root=config.MEETINGS_FOLDER,
            end_redecode=(
                False if demo else resolve_meeting_end_redecode(settings)
            ),
            end_polish=resolve_meeting_end_polish(settings),
            end_report=resolve_meeting_end_report(settings),
            report_views=resolve_meeting_report_views(settings),
            demo_mode=demo,
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

    def retry_insights(self) -> None:
        """Retry every failed post-meeting step for the card's meeting."""
        self.retry_finalization("failed")

    def retry_speakers(self) -> None:
        """Re-run speaker identification for the card's meeting."""
        self.retry_finalization("speaker_id")

    def retry_finalization(self, from_step: str = "failed") -> None:
        """Retry post-meeting steps from ``from_step`` through dependents.

        Args:
            from_step: Checklist step id, or ``failed`` for the earliest
                failed/skipped step.
        """
        step_key = str(from_step or "failed").strip() or "failed"
        with self._lock:
            if (
                self._starting
                or self.controller.meeting_active
                or self.is_active
                or self._finalizing
            ):
                logger.warning(
                    "Cannot retry finalization: meeting is active, starting, "
                    "or already finalizing"
                )
                return
            engine = self._engine
            meeting_id = getattr(engine, "meeting_id", None) or self._card_meeting_id
            if not meeting_id:
                repo = self._repository()
                recent = repo.list_meetings()
                if recent:
                    meeting_id = recent[0].get("id")
            if not meeting_id:
                logger.warning("Cannot retry finalization: no meeting found")
                return
            self._card_meeting_id = meeting_id
            self._finalizing = True
            self._finalization = {
                "status": "running",
                "message": "Retrying post-meeting steps…",
            }

        if step_key in {"failed", "redecode"}:
            ensure = getattr(self.controller, "ensure_local_model_available", None)
            if callable(ensure):
                try:
                    ensure()
                except Exception:
                    logger.exception("Could not ensure the local Whisper model")

        self._publish_finalization(
            meeting_id,
            {
                "status": "running",
                "message": "Retrying post-meeting steps…",
            },
            engine=engine,
        )
        self.controller.meeting_status_update.emit("Retrying post-meeting steps…")

        def _worker() -> None:
            status = "failed"
            message = "Post-meeting retry failed."
            finalization: Dict[str, Any] = {
                "status": status,
                "message": message,
            }
            try:
                from meeting.refinalize import DEFAULT_TIMEOUT_S, rerun_finalization
                from services.settings import resolve_meeting_language
                from services.transcript_cleanup import find_api_key

                repo = self._repository()
                settings = settings_manager.load_all_settings()
                provider = resolve_meeting_llm_provider(settings)
                model = resolve_meeting_llm_model(settings)
                agent_core_kind = resolve_meeting_agent_core(settings)
                payload_dir = meeting_agent_payload_dir()
                meeting = repo.get_meeting(meeting_id) or {}
                store = None
                if (
                    engine is not None
                    and getattr(engine, "meeting_id", None) == meeting_id
                ):
                    store = getattr(engine, "store", None)
                    try:
                        engine.allow_agent_writes()
                    except Exception:
                        logger.exception(
                            "Could not allow agent writes for finalization retry"
                        )

                def _progress(snapshot: Dict[str, Any]) -> None:
                    self._publish_finalization(
                        meeting_id, snapshot, engine=engine,
                    )
                    note = str(snapshot.get("message") or "").strip()
                    if note:
                        self.controller.meeting_status_update.emit(note)

                result = rerun_finalization(
                    repo,
                    meeting_id,
                    from_step=step_key,
                    provider=provider,
                    model=model,
                    endpoint=resolve_meeting_llm_endpoint(settings),
                    agent_core_kind=agent_core_kind,
                    sidecar_payload_dir=payload_dir,
                    store=store,
                    timeout_s=DEFAULT_TIMEOUT_S,
                    asr_model_name=str(
                        meeting.get("asr_model")
                        or resolve_meeting_whisper_model(settings)
                    ),
                    language=resolve_meeting_language(settings),
                    speaker_api_key=find_api_key("openai") or "",
                    progress_cb=_progress,
                )
                finalization = dict(result.get("finalization") or {})
                status = str(finalization.get("status") or (
                    "completed" if result.get("ok") else "failed"
                ))
                message = str(
                    finalization.get("message")
                    or result.get("error")
                    or message
                )
                finalization["status"] = status
                finalization["message"] = message
            except Exception as exc:
                logger.exception(
                    "Retry finalization worker raised for meeting %s", meeting_id
                )
                status = "failed"
                message = f"Post-meeting retry failed: {exc}"
                finalization = {"status": status, "message": message}
            finally:
                if (
                    engine is not None
                    and getattr(engine, "meeting_id", None) == meeting_id
                ):
                    try:
                        engine.revoke_agent_writes()
                    except Exception:
                        pass

            with self._lock:
                self._finalizing = False
                self._finalization = finalization
                self._card_meeting_id = meeting_id
            self._publish_finalization(meeting_id, finalization, engine=engine)
            self.controller.meeting_status_update.emit(message)
            try:
                self.controller.ui_controller.main_window.refresh_past_meetings()
            except Exception:
                pass

        threading.Thread(
            target=_worker, name="meeting-retry-finalization", daemon=True
        ).start()

    def _publish_finalization(
        self,
        meeting_id: str,
        finalization: Dict[str, Any],
        *,
        engine: Any = None,
    ) -> None:
        """Persist-aware UI/engine broadcast for a finalization snapshot."""
        payload = dict(finalization or {})
        payload["content_summary"] = self._meeting_content_summary(meeting_id)
        if (
            engine is not None
            and getattr(engine, "meeting_id", None) == meeting_id
        ):
            try:
                engine._set_finalization(
                    str(payload.get("status") or "running"),
                    str(payload.get("message") or ""),
                    stage=str(payload.get("stage") or ""),
                    current_step=int(payload.get("current_step") or 0),
                    total_steps=int(payload.get("total_steps") or 0),
                    step_details=str(payload.get("step_details") or ""),
                    steps=list(payload.get("steps") or []),
                    summary_stats=dict(payload.get("summary_stats") or {}),
                )
                return
            except Exception:
                logger.exception(
                    "Could not publish finalization on the live engine"
                )
        self.controller.meeting_state_changed.emit({
            "active": False,
            "meeting_id": meeting_id,
            "finalization": payload,
        })

    def open_dashboard(self) -> None:
        """Open the host dashboard in the default browser (UI callback target)."""
        url = self._host_url
        if not url:
            # A retained finalization card may outlive its original server.
            # Reuse the archive path so the tab button and Past Meetings Open
            # have identical startup/dependency handling.
            meeting_id = self._card_meeting_id
            if meeting_id:
                self.open_past_meeting(meeting_id)
                return
            self._report_dashboard_error(
                "No meeting dashboard is available. Start a meeting or open "
                "one from Past Meetings."
            )
            return
        # Never log the path: it carries the host token, which grants full
        # host authority over the meeting.
        logger.info(f"Opening meeting dashboard: {redact_meeting_url(url)}")
        webbrowser.open(url)

    def open_past_meeting(self, meeting_id: str) -> None:
        """Open one persisted meeting in the host web dashboard.

        Reuses the live/most-recent meeting server when available. After an
        application restart, a lightweight archive dashboard server is started
        without initializing capture, ASR, diarization, or meeting intelligence.

        Args:
            meeting_id: Persisted meeting session id selected in the sidebar.
        """
        target_id = str(meeting_id or "").strip()
        if not target_id:
            return
        with self._lock:
            if self._archive_starting:
                self.controller.meeting_status_update.emit(
                    "Opening a past meeting..."
                )
                return
            self._archive_starting = True
        threading.Thread(
            target=self._open_past_meeting_worker,
            args=(target_id,),
            name="meeting-history-dashboard",
            daemon=True,
        ).start()

    def _open_past_meeting_worker(self, meeting_id: str) -> None:
        """Resolve or start a host dashboard server and open the history view."""
        try:
            repository = self._repository()
            meeting = repository.get_meeting(meeting_id)
            if meeting is None:
                self._report_dashboard_error("That meeting no longer exists")
                return
            self._hydrate_finalization_card(meeting, reveal=True)

            url = self._host_url
            archive = self._archive_dashboard
            if not url and archive is not None and archive.is_running():
                url = archive.host_url

            if not url:
                if self.is_active:
                    self._report_dashboard_error(
                        "The live meeting dashboard is unavailable"
                    )
                    return
                self._shutdown_engine()
                self._shutdown_archive_dashboard()

                from meeting.web.archive import ArchivedMeetingDashboard
                from meeting.web.server import MeetingWebServer

                settings = settings_manager.load_all_settings()
                archive = ArchivedMeetingDashboard(
                    repository,
                    meeting,
                    spool_root=config.MEETINGS_FOLDER,
                    llm_provider=resolve_meeting_llm_provider(settings),
                    llm_model=resolve_meeting_llm_model(settings),
                    llm_endpoint=resolve_meeting_llm_endpoint(settings),
                    agent_core_kind=resolve_meeting_agent_core(settings),
                    sidecar_payload_dir=meeting_agent_payload_dir(),
                )
                server = MeetingWebServer(
                    archive,
                    repository,
                    bind=resolve_meeting_server_bind(settings),
                    port=resolve_meeting_server_port(settings),
                )
                server.start()
                archive.attach_server(server)
                self._archive_dashboard = archive
                url = server.host_url

            if not url:
                self._report_dashboard_error(
                    "Could not create a meeting dashboard link"
                )
                return

            history_url = _with_history_target(url, meeting_id)
            logger.info(
                "Opening past meeting dashboard: %s",
                redact_meeting_url(history_url),
            )
            self.controller.meeting_status_update.emit("Opening past meeting")
            webbrowser.open(history_url)
        except Exception as exc:
            logger.error("Failed to open past meeting", exc_info=True)
            self._report_dashboard_error(
                f"Could not open the past meeting: {exc}"
            )
        finally:
            with self._lock:
                self._archive_starting = False

    def _report_dashboard_error(self, message: str) -> None:
        """Surface dashboard failures consistently for every UI entry point."""
        self.controller.meeting_error.emit(str(message))

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
                settings = settings_manager.load_all_settings()
                if not finalize(
                    repository,
                    meeting,
                    asr_language=resolve_meeting_language(settings),
                ):
                    self.controller.meeting_error.emit(
                        "Could not finalize the meeting — "
                        "transcription may still be pending"
                    )
                    return
            else:
                # Minimal honest fallback: mark the session ended so it shows
                # up in history instead of the recovery list.
                from meeting.time_utils import utc_now_iso

                repository.update_meeting(
                    meeting_id, status="ended",
                    ended_at=utc_now_iso(),
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
            from meeting.persist.data_lifecycle import delete_meeting_data

            repository = self._repository()
            delete_meeting_data(
                repository, meeting_id, config.MEETINGS_FOLDER
            )
            with self._lock:
                shown = self._card_meeting_id == meeting_id
            if shown:
                self._hide_finalization_card()
            else:
                self._refresh_past_meetings()
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
        calls are allowed here. Finalization outcomes are non-modal status
        updates — never ``meeting_error``.
        """
        try:
            payload = payload or {}
            if kind == "status":
                status = payload.get("status", "")
                finalization = payload.get("finalization")
                state_payload: Dict[str, Any] = {}
                if status:
                    state_payload["status"] = str(status)
                    # Prefer human notes when present; otherwise keep the short
                    # lifecycle status for the status line.
                    note = payload.get("note")
                    self.controller.meeting_status_update.emit(
                        str(note or status)
                    )
                if isinstance(finalization, dict):
                    self._apply_finalization(finalization, state_payload)
                elif finalization is None and "finalization" in payload:
                    with self._lock:
                        self._finalizing = False
                        self._finalization = None
                    state_payload["finalization"] = None
                if state_payload:
                    # Capture ownership is engine-derived; status alone must not
                    # flip active. Finalization rides along for the Meeting tab.
                    if "active" not in state_payload and not self.is_active:
                        # After end, keep active=False while finalization updates.
                        if self.controller.meeting_active is False:
                            state_payload.setdefault("active", False)
                    self.controller.meeting_state_changed.emit(state_payload)
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
                status = str(payload.get("status") or "ended")
                with self._lock:
                    finalization = dict(self._finalization or {})
                    finalizing = self._finalizing
                if finalizing:
                    message = "Meeting ended — preparing final insights…"
                elif status == "needs_recovery":
                    message = "Meeting ended — transcription recovery needed"
                else:
                    message = "Meeting ended"
                self.controller.meeting_status_update.emit(message)
                state_payload = {"active": False, "status": status}
                if finalization:
                    state_payload["finalization"] = finalization
                self.controller.meeting_state_changed.emit(state_payload)
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

    def _apply_finalization(
        self,
        finalization: Dict[str, Any],
        state_payload: Dict[str, Any],
    ) -> None:
        """Track finalization and attach it to a meeting-state payload.

        Args:
            finalization: Mapping from the engine status event.
            state_payload: Mutable payload being built for the UI.
        """
        status = str(finalization.get("status") or "")
        message = str(finalization.get("message") or "")
        meeting_id = getattr(self._engine, "meeting_id", None)
        normalized = {
            "status": status,
            "message": message,
            "stage": str(finalization.get("stage") or ""),
            "current_step": int(finalization.get("current_step") or 0),
            "total_steps": int(finalization.get("total_steps") or 0),
            "step_details": str(finalization.get("step_details") or ""),
            "steps": list(finalization.get("steps") or []),
            "summary_stats": dict(finalization.get("summary_stats") or {}),
            "card_deferred": bool(finalization.get("card_deferred", False)),
            "content_summary": dict(
                finalization.get("content_summary")
                or self._meeting_content_summary(meeting_id)
            ),
        }
        terminal = status in {
            "completed", "disabled", "unavailable", "failed",
        }
        with self._lock:
            self._finalization = normalized
            self._finalizing = status == "running"
            if meeting_id:
                self._card_meeting_id = meeting_id
        state_payload["finalization"] = normalized
        if status == "running":
            self.controller.meeting_status_update.emit(
                message or "Preparing final cloud insights…"
            )
        elif terminal and message:
            # Persistent non-modal feedback; never a meeting_error dialog.
            self.controller.meeting_status_update.emit(message)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _shutdown_archive_dashboard(self) -> None:
        """Stop and discard the lightweight archived-meeting dashboard."""
        archive, self._archive_dashboard = self._archive_dashboard, None
        if archive is None:
            return
        try:
            archive.shutdown()
        except Exception as exc:
            logger.debug("Error shutting down archive dashboard: %s", exc)

    def _shutdown_engine(self, timeout: Optional[float] = None) -> None:
        """Shut down and drop the current engine, if any.

        Args:
            timeout: Seconds to wait for the shutdown. When given, the
                shutdown runs on a daemon thread and is abandoned once the
                budget expires; None blocks until the engine is down.
        """
        with self._lock:
            engine, self._engine = self._engine, None
            # Drop retained dashboard URLs only when the engine itself is torn
            # down (new meeting or app exit) — not merely when capture ends.
            self._host_url = None
            self._guest_url = None
            self._finalizing = False
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

    def _meeting_content_summary(
        self,
        meeting_id: Optional[str],
        *,
        meeting: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Derive durable audio/transcript capabilities for desktop cards."""
        if not meeting_id:
            return {}
        from meeting.content import summarize_meeting_content

        summary = summarize_meeting_content(self._repository(), meeting_id)
        row = meeting
        if row is None:
            try:
                row = self._repository().get_meeting(meeting_id)
            except Exception:
                logger.exception(
                    "Could not read meeting status for content summary"
                )
        summary["meeting_status"] = str((row or {}).get("status") or "")
        return summary

    def cleanup(self) -> None:
        """Release the meeting engine on application shutdown.

        Cleanup runs after ``app.exec()`` returns, so the window and tray are
        already gone: a full ``MeetingEngine.shutdown()`` (ASR drain plus a
        final consolidation pass) would leave an invisible process lingering
        for minutes. The wait is bounded instead — the meeting is already
        persisted, so only the tail of a consolidation pass is at risk.
        """
        self._shutdown_archive_dashboard()
        self._shutdown_engine(timeout=SHUTDOWN_JOIN_TIMEOUT_S)
        self.controller.meeting_active = False
