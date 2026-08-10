"""MeetingEngine: the Meeting Mode orchestrator.

Owns one meeting's whole pipeline — capture sources, chunk spools, the
dedicated ASR engine, the diarizer, the state store, the web server, and the
intelligence layer (agent core + checkpoint scheduler) — and implements the
``AgentToolHost`` protocol so the agent's only authority is validated state
patches.

No Qt imports; the Qt runtime observes the engine through ``add_listener``.
Sibling subsystem imports (``meeting.capture``, ``meeting.asr``,
``meeting.web``, ``meeting.agent``, ``meeting.diarize``) are lazy, inside
methods, so partial availability of those packages never breaks importing
this module.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from meeting.clock import MeetingClock
from meeting.interfaces import (
    CHANNEL_LOOPBACK,
    CHANNEL_MIC,
    AgentConfig,
    CaptureBlock,
    OpResult,
    SpooledChunk,
    TranscriptSegment,
)
from meeting.state.schema import MeetingState, new_id, now_iso
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)

#: Seconds between repository heartbeats while a meeting is live.
HEARTBEAT_INTERVAL_S = 10.0
#: ASR drain budget for a normal, user-initiated end.
END_DRAIN_TIMEOUT_S = 300.0
#: Shorter drain budget when the whole app is shutting down.
SHUTDOWN_DRAIN_TIMEOUT_S = 30.0
#: Budget for the final consolidation pass.
CONSOLIDATION_TIMEOUT_S = 120.0
#: How long end/cancel waits for an in-flight ``start()`` to finish before
#: unwinding a partially built pipeline anyway.
START_WAIT_TIMEOUT_S = 120.0
#: Guest display names are clamped to this length.
MAX_GUEST_NAME_LEN = 80
CAPTURE_WATCHDOG_INTERVAL_S = 2.0
CAPTURE_RETRY_INTERVAL_S = 10.0

#: Engine listener: ``cb(kind, payload)`` with kinds ``status``, ``segments``,
#: ``server_started``, ``error``, ``ended``, ``intelligence``.
Listener = Callable[[str, Dict[str, Any]], None]


@dataclass
class MeetingEngineOptions:
    """Plain-value configuration for one meeting (resolved by the caller)."""
    title: str = ''
    cloud_enabled: bool = False
    mic_device_id: Optional[int] = None
    asr_model: str = 'auto'
    llm_provider: str = 'openrouter'
    llm_model: str = ''
    agent_core_kind: str = 'pi'   # 'pi' | 'direct'
    sidecar_payload_dir: Optional[str] = None
    diarization_model_path: Optional[str] = None
    server_bind: str = 'localhost'    # 'localhost' | 'lan'
    server_port: int = 0
    spool_root: str = ''              # parent dir for meeting spool dirs


class MeetingEngine:
    """Single-meeting orchestrator; also the agent's ``AgentToolHost``.

    Lifecycle: construct → ``start()`` → (``pause()``/``resume()``/
    ``set_cloud_enabled()`` while live) → ``end()`` or ``cancel()`` →
    ``shutdown()`` on app exit. ``end()`` returns immediately and finalizes on
    a worker thread; the web server stays up serving the final state until
    ``shutdown()``.

    Attributes:
        meeting_id: Id of the running meeting; None before ``start()``.
        store: The meeting's ``MeetingStateStore``; available after ``start()``.
        clock: Shared pausable meeting clock.
        repository: The ``MeetingRepository`` used for all persistence.
    """

    def __init__(self, options: MeetingEngineOptions, repository: Optional[Any] = None) -> None:
        """Args:
            options: Resolved plain-value configuration for this meeting.
            repository: A ``MeetingRepository``; defaults to the SQL
                repository on the app database (imported lazily).
        """
        if repository is None:
            from meeting.persist.repository import SqlMeetingRepository
            repository = SqlMeetingRepository()
        self.options = options
        self.repository = repository
        self.clock = MeetingClock()
        self.meeting_id: Optional[str] = None
        self.store: Optional[MeetingStateStore] = None

        self._lifecycle_lock = threading.RLock()
        self._active = False
        # True for the whole of start(); end/cancel wait it out rather than
        # tearing down a pipeline that is still being built.
        self._starting = False
        self._start_thread_id: Optional[int] = None
        self._start_complete = threading.Event()
        self._end_thread: Optional[threading.Thread] = None
        self._spool_dir: Optional[str] = None
        self._me_participant_id: Optional[str] = None

        self._listeners: List[Listener] = []
        self._listener_lock = threading.Lock()

        self._sources: List[Any] = []
        self._spools: Dict[str, Any] = {}
        self._capture_lock = threading.RLock()
        self._capture_watchdog_stop: Optional[threading.Event] = None
        self._capture_watchdog_thread: Optional[threading.Thread] = None
        self._capture_last_attempt: Dict[str, float] = {}
        self._asr: Optional[Any] = None
        self._diarizer: Optional[Any] = None
        self._server: Optional[Any] = None
        self._agent_core: Optional[Any] = None
        self._scheduler: Optional[Any] = None
        # Latches once the diarizer stops labeling, so the dashboard banner is
        # corrected exactly once per meeting.
        self._degraded_diarization = False
        # True once intelligence has been stopped at least once: a rebuilt
        # scheduler must not re-ship the whole meeting-to-date transcript.
        self._intelligence_restarted = False

        self._chunk_lock = threading.Lock()
        self._chunk_index: Dict[int, SpooledChunk] = {}
        self._enqueued_chunk_ids: set = set()

        self._hb_stop: Optional[threading.Event] = None
        self._hb_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def add_listener(self, cb: Listener) -> None:
        """Register an event listener ``cb(kind, payload)``.

        Kinds: ``status``, ``segments``, ``server_started``, ``error``,
        ``ended``, ``intelligence``. Callbacks may fire from worker threads.
        """
        with self._listener_lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb: Listener) -> None:
        """Unregister a previously added listener (no-op when unknown)."""
        with self._listener_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(kind, payload)
            except Exception:
                logger.exception("Meeting listener raised for %s event", kind)

    def _broadcast(self, message: Dict[str, Any]) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.broadcast(message)
        except Exception:
            logger.exception("Meeting web broadcast failed")

    def _emit_status(self, note: Optional[str] = None) -> None:
        """Emit the current status to listeners and connected web clients."""
        if self.store is None:
            return
        status, intel, diar, capture = self.store.with_state(
            lambda s: (
                s.status, s.intelligence_online, s.diarization_available,
                dict(s.capture),
            )
        )
        payload = {
            "status": status,
            "intelligence_online": intel,
            "diarization_available": diar,
            "capture": capture,
        }
        listener_payload = dict(payload)
        if note:
            listener_payload["note"] = note
        self._emit("status", listener_payload)
        self._broadcast({"type": "status", **payload})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """True from a successful ``start()`` until end/cancel completes."""
        return self._active

    def start(self) -> Dict[str, Any]:
        """Create the meeting and bring the whole pipeline up.

        Degrades gracefully: a missing loopback device, ASR model, web
        stack, or intelligence backend logs and emits ``error``/``status``
        events but never prevents the rest of the meeting from running.

        Returns:
            ``{'meeting_id', 'url', 'host_url', 'guest_url'}`` — URL values
            are None when the web server could not start.

        Raises:
            RuntimeError: When the engine is already active or was ended.
            Exception: When core setup (persistence/state) fails; partial
                setup is torn down and the meeting row marked ``failed``.
        """
        with self._lifecycle_lock:
            if self._active or self._starting or self._end_thread is not None:
                raise RuntimeError("Meeting engine is already active or finished")
            self._starting = True
            self._start_thread_id = threading.get_ident()
            self._start_complete.clear()
            self._active = True
        try:
            meeting_id = new_id("m")
            host_token = secrets.token_urlsafe(32)
            guest_token = secrets.token_urlsafe(32)
            spool_root = self.options.spool_root or "meeting_audio"
            spool_dir = os.path.join(spool_root, meeting_id[:12])
            os.makedirs(spool_dir, exist_ok=True)

            started_at = now_iso()
            self.repository.create_meeting(
                id=meeting_id,
                title=self.options.title,
                status="active",
                started_at=started_at,
                ended_at=None,
                paused_total_s=0.0,
                host_token=host_token,
                guest_token=guest_token,
                cloud_enabled=self.options.cloud_enabled,
                asr_model=self.options.asr_model,
                agent_provider=self.options.llm_provider,
                agent_model=self.options.llm_model,
                spool_dir=spool_dir,
                state_json=None,
                state_seq=0,
                app_pid=os.getpid(),
                app_heartbeat_at=started_at,
            )
            self.meeting_id = meeting_id
            self._spool_dir = spool_dir

            state = MeetingState(
                meeting_id=meeting_id,
                cloud_enabled=self.options.cloud_enabled,
                title=self.options.title,
            )
            self.store = MeetingStateStore(
                state,
                repository=self.repository,
                segment_handler=self._handle_segment_op,
                segment_exists=lambda sg_id: self.repository.segment_exists(
                    meeting_id, sg_id
                ),
                # Lets the validator reject a stale diarizer re-cluster that
                # would revert a human speaker correction.
                segment_pinned=lambda sg_id: bool(
                    (self.repository.get_segment(meeting_id, sg_id) or {}).get(
                        "speaker_pinned"
                    )
                ),
            )
            results = self.store.apply("system", None, [{
                "op": "upsert_participant", "display_name": "Me",
                "kind": "me", "is_provisional": False,
            }])
            if results and results[0].ok and results[0].effect:
                self._me_participant_id = results[0].effect["participant"]["id"]

            self.clock.start()
            self._start_heartbeat()

            # Consumers before producers: diarizer and ASR are in place
            # before capture can finalize the first chunk.
            self._start_diarizer()
            self._start_asr()
            capture_note = self._start_capture()
            url = self._start_server()
            self._maybe_start_intelligence()
            self._emit_status(note=capture_note)

            return {
                "meeting_id": meeting_id,
                "url": url,
                "host_url": getattr(self._server, "host_url", None) if self._server else None,
                "guest_url": getattr(self._server, "guest_url", None) if self._server else None,
            }
        except Exception:
            logger.exception("Meeting start failed")
            self._abort_start()
            raise
        finally:
            with self._lifecycle_lock:
                self._starting = False
                self._start_thread_id = None
            self._start_complete.set()

    def pause(self) -> None:
        """Freeze the meeting clock and mark the meeting paused."""
        with self._lifecycle_lock:
            # Reject once end/cancel has claimed the session so we cannot
            # overwrite a terminal status with "paused".
            if (not self._active or self._end_thread is not None
                    or self.store is None):
                return
            self.clock.pause()
            if not self.store.update_runtime_fields(status="paused"):
                self.clock.resume()
                raise RuntimeError("Could not persist paused meeting state")
        self._emit_status()

    def resume(self) -> None:
        """Unfreeze the meeting clock and mark the meeting active again."""
        with self._lifecycle_lock:
            if (not self._active or self._end_thread is not None
                    or self.store is None):
                return
            self.clock.resume()
            if not self.store.update_runtime_fields(status="active"):
                self.clock.pause()
                raise RuntimeError("Could not persist resumed meeting state")
        self._emit_status()

    def end(self) -> None:
        """Finish the meeting asynchronously.

        Stops capture, flushes spools, drains ASR, runs the consolidation
        pass, marks the meeting ended, and emits ``ended`` — all on a worker
        thread. The web server stays up serving the final state until
        ``shutdown()``.
        """
        self._begin_end(END_DRAIN_TIMEOUT_S)

    def _await_start(self, timeout_s: float = START_WAIT_TIMEOUT_S) -> None:
        """Block until an in-flight ``start()`` has finished.

        ``start()`` claims the session before it builds anything, so an end or
        cancel arriving in that window would otherwise unwind a half-built
        pipeline while ``start()`` kept bringing capture, ASR, the server, and
        the agent up for an already-finished meeting.

        Args:
            timeout_s: Maximum seconds to wait before proceeding anyway.
        """
        with self._lifecycle_lock:
            if (not self._starting
                    or self._start_thread_id == threading.get_ident()):
                return
        if not self._start_complete.wait(timeout=timeout_s):
            logger.warning(
                "Meeting start did not finish within %.0fs; unwinding a "
                "partially started pipeline", timeout_s,
            )

    def _begin_end(self, drain_timeout_s: float) -> Optional[threading.Thread]:
        self._await_start()
        with self._lifecycle_lock:
            if self._end_thread is not None:
                return self._end_thread
            if not self._active:
                return None
            thread = threading.Thread(
                target=self._end_worker, args=(drain_timeout_s,),
                name="meeting-end", daemon=True,
            )
            self._end_thread = thread
        thread.start()
        return thread

    def _end_worker(self, drain_timeout_s: float) -> None:
        terminal_persisted = False
        try:
            self._stop_capture()
            self._flush_spools()
            drained = True
            if self._asr is not None:
                try:
                    drained = bool(self._asr.drain(drain_timeout_s))
                except Exception:
                    logger.exception("ASR drain failed at meeting end")
                    drained = False
            try:
                unfinished = self.repository.count_unfinished_chunks(
                    self.meeting_id
                )
            except Exception:
                logger.exception("Could not verify unfinished ASR chunks")
                unfinished = 1
            complete = drained and unfinished == 0
            scheduler = self._scheduler
            if scheduler is not None and complete:
                try:
                    scheduler.run_consolidation(timeout_s=CONSOLIDATION_TIMEOUT_S)
                except Exception:
                    logger.exception("Consolidation pass failed")
            if scheduler is not None:
                try:
                    scheduler.stop()
                except Exception:
                    logger.exception("Scheduler stop failed")
                self._scheduler = None
            if self._asr is not None:
                try:
                    self._asr.stop()
                except Exception:
                    logger.exception("ASR stop failed")
            self._shutdown_agent_core()
            self.clock.pause()
            status = "ended" if complete else "needs_recovery"
            if self.store is not None:
                if not self.store.update_runtime_fields(status=status):
                    raise RuntimeError("Could not persist terminal meeting state")
            try:
                self.repository.update_meeting(
                    self.meeting_id,
                    status=status,
                    ended_at=now_iso(),
                    paused_total_s=self.clock.paused_total_s(),
                )
                terminal_persisted = True
            except Exception:
                logger.exception("Failed to persist terminal meeting metadata")
                raise
            self._stop_heartbeat()
            self._active = False
            self._broadcast({"type": "meeting_ended", "status": status})
            self._emit_status()
            self._emit("ended", {
                "meeting_id": self.meeting_id,
                "canceled": False,
                "status": status,
                "unfinished_chunks": unfinished,
            })
        except Exception as exc:
            logger.exception("Meeting end failed")
            self._active = False
            self._emit("error", {"code": "end_failed", "message": str(exc)})
            self._finish_failed_end(exc, terminal_persisted)

    def _finish_failed_end(self, exc: Exception, ended_persisted: bool) -> None:
        """Unwind after ``_end_worker`` raised, still reporting ``ended``.

        The Qt runtime only leaves exclusive meeting mode on the ``ended``
        event, so a failed end that stayed silent would block dictation until
        the app restarted. Everything here is best-effort and never raises.

        Args:
            exc: The exception that aborted the end.
            ended_persisted: True when the meeting row was already marked
                ended before the failure.
        """
        asr = self._asr
        if asr is not None:
            try:
                asr.stop()
            except Exception:
                logger.exception("ASR stop failed after a failed end")
        self._shutdown_agent_core()
        try:
            self.clock.pause()
        except Exception:
            logger.exception("Clock pause failed after a failed end")
        self._stop_heartbeat()
        status = "needs_recovery"
        if not ended_persisted:
            try:
                self.repository.update_meeting(
                    self.meeting_id, status=status, ended_at=now_iso(),
                )
            except Exception:
                logger.exception("Failed to persist failed end status")
        if self.store is not None:
            try:
                self.store.update_runtime_fields(status=status)
            except Exception:
                logger.exception("Failed to update state status after a failed end")
        self._broadcast({"type": "meeting_ended", "status": status})
        try:
            self._emit_status()
        except Exception:
            logger.exception("Status emit failed after a failed end")
        self._emit("ended", {
            "meeting_id": self.meeting_id, "canceled": False,
            "status": status, "error": str(exc),
        })

    def cancel(self) -> None:
        """Discard the session fast: no drain, no consolidation.

        The meeting row is marked ``failed``; spooled audio and any
        already-transcribed segments are kept on disk/DB (nothing deleted).
        """
        self._await_start()
        with self._lifecycle_lock:
            if not self._active or self._end_thread is not None:
                return
            self._active = False
        self._stop_capture()
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:
                logger.exception("Scheduler stop failed during cancel")
        if self._agent_core is not None:
            try:
                self._agent_core.cancel()
            except Exception:
                logger.exception("Agent cancel failed")
        self._shutdown_agent_core()
        if self._asr is not None:
            try:
                self._asr.stop()
            except Exception:
                logger.exception("ASR stop failed during cancel")
        # After ASR is down: releases each spool's writer thread and leaves
        # the last partial chunk on disk as a recoverable pending row.
        self._flush_spools()
        self.clock.pause()
        self._stop_heartbeat()
        try:
            self.repository.update_meeting(
                self.meeting_id, status="failed", ended_at=now_iso(),
                paused_total_s=self.clock.paused_total_s(),
            )
        except Exception:
            logger.exception("Failed to persist canceled status")
        if self.store is not None:
            self.store.update_runtime_fields(status="failed")
        self._broadcast({"type": "meeting_ended"})
        self._emit_status()
        self._emit("ended", {"meeting_id": self.meeting_id, "canceled": True})

    def shutdown(self) -> None:
        """Full teardown for app exit, including the web server.

        Ends the meeting (short drain budget) when still active, waits for
        finalization, then stops the server and the agent core.
        """
        thread = None
        if self._active:
            thread = self._begin_end(SHUTDOWN_DRAIN_TIMEOUT_S)
        else:
            with self._lifecycle_lock:
                thread = self._end_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(
                timeout=SHUTDOWN_DRAIN_TIMEOUT_S + CONSOLIDATION_TIMEOUT_S + 60.0
            )
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.stop()
            except Exception:
                logger.exception("Meeting web server stop failed")
        self._shutdown_agent_core()
        self._stop_heartbeat()

    def _abort_start(self) -> None:
        """Best-effort teardown after a failed ``start()``."""
        self._active = False
        self._stop_capture()
        self._flush_spools()  # releases the spool writer threads
        if self._asr is not None:
            try:
                self._asr.stop()
            except Exception:
                logger.exception("ASR stop failed during start abort")
            self._asr = None
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:
                logger.exception("Scheduler stop failed during start abort")
        self._shutdown_agent_core()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.stop()
            except Exception:
                logger.exception("Server stop failed during start abort")
        self._stop_heartbeat()
        self.clock.pause()
        if self.meeting_id:
            try:
                self.repository.update_meeting(
                    self.meeting_id, status="failed", ended_at=now_iso()
                )
            except Exception:
                logger.exception("Failed to mark aborted meeting as failed")

    def _shutdown_agent_core(self) -> None:
        core = self._agent_core
        self._agent_core = None
        if core is not None:
            try:
                core.shutdown()
            except Exception:
                logger.exception("Agent core shutdown failed")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="meeting-heartbeat", daemon=True
        )
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        stop = self._hb_stop
        while stop is not None and not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self.repository.heartbeat(self.meeting_id)
            except Exception:
                logger.exception("Meeting heartbeat failed")

    def _stop_heartbeat(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()

    # ------------------------------------------------------------------
    # Capture + spool
    # ------------------------------------------------------------------

    def _start_capture(self) -> Optional[str]:
        """Open mic and loopback streams; returns a user-facing degradation
        note when one or both channels are unavailable.

        Every source is verified with ``is_active()`` after ``start()``: a
        source that opens no device must not be mistaken for a silent room,
        and a discovered-but-unusable WASAPI loopback device must still fall
        through to the ``soundcard`` fallback.
        """
        try:
            from meeting.capture.devices import find_loopback_device, find_mic_device
            from meeting.capture.sd_stream import SdCaptureSource
            from meeting.capture.spool import SpoolWriter  # noqa: F401
        except Exception as exc:
            logger.exception("Capture layer unavailable")
            self._emit("error", {"code": "capture_unavailable", "message": str(exc)})
            return "Audio capture is unavailable; no audio is being recorded."

        self._sources = []
        mic_dev = None
        try:
            mic_dev = find_mic_device(self.options.mic_device_id)
        except Exception:
            logger.exception("Microphone probe failed")
        if mic_dev is not None:
            try:
                self._start_source(SdCaptureSource(
                    CHANNEL_MIC, mic_dev["index"],
                    mic_dev["samplerate"], mic_dev["channels"],
                ))
            except Exception:
                logger.exception("Failed to open microphone stream")
        else:
            logger.warning("No microphone device found; mic channel disabled")

        loop_dev = None
        loop_ok = False
        try:
            loop_dev = find_loopback_device()
        except Exception:
            logger.exception("Loopback probe failed")
        if loop_dev is not None:
            try:
                loop_ok = self._start_source(SdCaptureSource(
                    CHANNEL_LOOPBACK, loop_dev["index"],
                    loop_dev["samplerate"], loop_dev["channels"],
                ))
            except Exception:
                logger.exception("Failed to open WASAPI loopback stream")
        if not loop_ok:
            try:
                from meeting.capture.soundcard_stream import SoundcardLoopbackSource
                if SoundcardLoopbackSource.available():
                    loop_ok = self._start_source(SoundcardLoopbackSource())
            except Exception:
                logger.exception("soundcard loopback fallback unavailable")
        if not loop_ok:
            logger.warning("No loopback source available; meeting continues mic-only")

        active = {source.channel for source in self._sources}
        notes = []
        if CHANNEL_MIC not in active:
            notes.append("Microphone capture is unavailable.")
        if CHANNEL_LOOPBACK not in active:
            notes.append("System-audio capture is unavailable; "
                         "recording microphone only."
                         if CHANNEL_MIC in active else
                         "System-audio capture is unavailable.")
        if not active:
            self._emit("error", {
                "code": "capture_unavailable",
                "message": "No audio devices could be opened.",
            })
            note = "No audio devices could be opened; no audio is being recorded."
        else:
            note = " ".join(notes) or None
        self._update_capture_status(note or "")
        self._start_capture_watchdog()
        return note

    def _start_source(self, source: Any) -> bool:
        """Start one capture source and verify it is really delivering audio.

        Args:
            source: A ``CaptureSource`` that has not been started yet.

        Returns:
            True when the source started and reports itself active; False
            when it failed to open, in which case its spool is torn down and
            the caller is free to try a fallback for the same channel.
        """
        from meeting.capture.spool import SpoolWriter  # already validated

        spool = SpoolWriter(
            self.meeting_id, source.channel, self._spool_dir,
            self.clock, self.repository, on_chunk=self._on_chunk,
            initial_seq=self.repository.next_chunk_seq(
                self.meeting_id, source.channel
            ),
        )
        started = False
        try:
            source.start(self._make_block_router(source.channel, spool))
            started = True
            active = bool(source.is_active())
        except Exception:
            logger.exception("Failed to start %s capture", source.channel)
            active = False
        if not active:
            if started:
                logger.error("%s capture reported inactive right after "
                             "starting; treating it as unavailable",
                             source.channel)
                try:
                    source.stop()
                except Exception:
                    logger.exception("Failed to stop inactive %s source",
                                     source.channel)
            try:
                spool.flush()  # joins the writer thread; nothing to write
            except Exception:
                logger.exception("Failed to release spool for %s",
                                 source.channel)
            return False
        with self._capture_lock:
            self._spools[source.channel] = spool
            self._sources.append(source)
        return True

    def _make_block_router(self, channel: str, spool: Any) -> Callable[[CaptureBlock], None]:
        """Audio-thread callback: drops blocks while paused, never raises."""
        error_logged = [False]

        def route(block: CaptureBlock) -> None:
            if not self._active or self.clock.is_paused:
                return
            try:
                spool.feed(block)
            except Exception:
                if not error_logged[0]:
                    error_logged[0] = True
                    logger.exception(
                        "Spool feed failed on channel %s (suppressing repeats)",
                        channel,
                    )
        return route

    def _stop_capture(self) -> None:
        self._stop_capture_watchdog()
        with self._capture_lock:
            sources, self._sources = self._sources, []
        for source in sources:
            try:
                source.stop()
            except Exception:
                logger.exception("Capture source stop failed")

    def _flush_spools(self) -> None:
        with self._capture_lock:
            spools = list(self._spools.values())
        for spool in spools:
            try:
                chunk = spool.flush()
            except Exception:
                logger.exception("Spool flush failed")
                continue
            if chunk is not None:
                self._on_chunk(chunk)

    def _start_capture_watchdog(self) -> None:
        """Start device-loss/default-device monitoring for this meeting."""
        if (self._capture_watchdog_thread is not None
                and self._capture_watchdog_thread.is_alive()):
            return
        stop = threading.Event()
        self._capture_watchdog_stop = stop
        self._capture_watchdog_thread = threading.Thread(
            target=self._capture_watchdog_loop,
            name="meeting-capture-watchdog",
            daemon=True,
        )
        self._capture_watchdog_thread.start()

    def _stop_capture_watchdog(self) -> None:
        stop = self._capture_watchdog_stop
        if stop is not None:
            stop.set()
        thread = self._capture_watchdog_thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=3.0)
        self._capture_watchdog_thread = None
        self._capture_watchdog_stop = None

    def _capture_watchdog_loop(self) -> None:
        """Restart failed sources and follow default-device changes."""
        stop = self._capture_watchdog_stop
        while (stop is not None
               and not stop.wait(CAPTURE_WATCHDOG_INTERVAL_S)):
            if not self._active:
                return
            for channel in (CHANNEL_MIC, CHANNEL_LOOPBACK):
                try:
                    desired = self._probe_capture_device(channel)
                    source = self._capture_source(channel)
                    active = source is not None and bool(source.is_active())
                    changed = False
                    if active:
                        source_id = getattr(source, "device_id", None)
                        desired_id = desired.get("index") if desired else None
                        if channel == CHANNEL_MIC and desired is not None:
                            changed = (
                                self.options.mic_device_id is None
                                and isinstance(source_id, int)
                                and source_id != desired_id
                            )
                        elif (channel == CHANNEL_LOOPBACK
                              and isinstance(source_id, int)
                              and desired is not None):
                            changed = (
                                source_id != desired_id
                            )
                        elif channel == CHANNEL_LOOPBACK:
                            current = getattr(
                                source, "is_default_device_current", None
                            )
                            if callable(current):
                                changed = not bool(current())
                    if active and not changed:
                        continue
                    now = time.monotonic()
                    if (now - self._capture_last_attempt.get(channel, 0.0)
                            < CAPTURE_RETRY_INTERVAL_S):
                        continue
                    self._capture_last_attempt[channel] = now
                    self._restart_capture_channel(channel, desired)
                except Exception:
                    logger.exception("Capture watchdog failed for %s", channel)
            self._update_capture_status()

    def _probe_capture_device(self, channel: str) -> Optional[Dict[str, Any]]:
        from meeting.capture.devices import find_loopback_device, find_mic_device

        if channel == CHANNEL_LOOPBACK:
            return find_loopback_device()
        return find_mic_device(self.options.mic_device_id)

    def _capture_source(self, channel: str) -> Optional[Any]:
        with self._capture_lock:
            return next(
                (source for source in self._sources
                 if source.channel == channel), None
            )

    def _restart_capture_channel(
        self, channel: str, desired: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Retire one channel and open its configured/default replacement."""
        self._retire_capture_channel(channel)
        try:
            from meeting.capture.devices import find_loopback_device, find_mic_device
            from meeting.capture.sd_stream import SdCaptureSource

            if channel == CHANNEL_MIC:
                desired = find_mic_device(self.options.mic_device_id)
            elif desired is None:
                desired = find_loopback_device()
            if desired is not None:
                if self._start_source(SdCaptureSource(
                    channel, desired["index"], desired["samplerate"],
                    desired["channels"],
                )):
                    return True
            if channel == CHANNEL_LOOPBACK:
                from meeting.capture.soundcard_stream import SoundcardLoopbackSource
                if SoundcardLoopbackSource.available():
                    return self._start_source(SoundcardLoopbackSource())
        except Exception:
            logger.exception("Could not restart %s capture", channel)
        return False

    def _retire_capture_channel(self, channel: str) -> None:
        with self._capture_lock:
            sources = [s for s in self._sources if s.channel == channel]
            self._sources = [s for s in self._sources if s.channel != channel]
            spool = self._spools.pop(channel, None)
        for source in sources:
            try:
                source.stop()
            except Exception:
                logger.exception("Could not stop stale %s source", channel)
        if spool is not None:
            try:
                chunk = spool.flush()
                if chunk is not None:
                    self._on_chunk(chunk)
            except Exception:
                logger.exception("Could not flush stale %s spool", channel)

    def _update_capture_status(self, message: str = "") -> None:
        if self.store is None:
            return
        with self._capture_lock:
            active = {
                source.channel for source in self._sources
                if bool(source.is_active())
            }
        if not message:
            missing = []
            if CHANNEL_MIC not in active:
                missing.append("Microphone unavailable")
            if CHANNEL_LOOPBACK not in active:
                missing.append("System audio unavailable")
            message = "; ".join(missing)
        capture = {
            "mic_available": CHANNEL_MIC in active,
            "loopback_available": CHANNEL_LOOPBACK in active,
            "message": message,
        }
        previous = self.store.with_state(lambda state: dict(state.capture))
        if capture == previous:
            return
        if self.store.update_runtime_fields(capture=capture):
            self._emit_status(note=message or None)

    def _on_chunk(self, chunk: SpooledChunk) -> None:
        """Route a finalized chunk to ASR (idempotent per chunk id)."""
        with self._chunk_lock:
            if chunk.chunk_id in self._enqueued_chunk_ids:
                return
            self._enqueued_chunk_ids.add(chunk.chunk_id)
            self._chunk_index[chunk.chunk_id] = chunk
        if self._asr is None:
            logger.warning("Chunk %s spooled but ASR is unavailable; left pending",
                           chunk.chunk_id)
            return
        try:
            self._asr.enqueue(chunk)
        except Exception:
            logger.exception("Failed to enqueue chunk %s", chunk.chunk_id)

    # ------------------------------------------------------------------
    # ASR + segments
    # ------------------------------------------------------------------

    def _start_asr(self) -> None:
        try:
            from meeting.asr.engine import MeetingAsrEngine
            asr = MeetingAsrEngine(self.options.asr_model, self.meeting_id,
                                   self.repository)
        except Exception as exc:
            logger.exception("Meeting ASR engine unavailable")
            self._emit("error", {"code": "asr_unavailable", "message": str(exc)})
            return
        if not getattr(asr, "is_available", True):
            logger.error("Meeting ASR model %r is not available; "
                         "chunks will stay pending", self.options.asr_model)
            self._emit("error", {
                "code": "asr_unavailable",
                "message": "The transcription model is not available; "
                           "audio is recorded and can be transcribed later.",
            })
            return
        asr.start(self._on_chunk_result)
        self._asr = asr
        requeue = getattr(asr, "requeue_pending", None)
        if callable(requeue):
            try:
                requeue()
            except Exception:
                logger.exception("requeue_pending failed")
        else:
            logger.debug("ASR engine exposes no requeue_pending; skipping")

    def _on_chunk_result(
        self, chunk: SpooledChunk, segments: List[TranscriptSegment]
    ) -> None:
        """Assign speakers, atomically commit the chunk, then publish it."""
        try:
            self._assign_speakers(segments)
        except Exception:
            logger.exception("Speaker assignment failed")
        rows, committed = self.repository.commit_chunk_transcription(
            self.meeting_id, chunk.chunk_id, segments
        )
        if not committed:
            return
        scheduler = self._scheduler
        if scheduler is not None:
            try:
                scheduler.notify_segments(len(rows))
            except Exception:
                logger.exception("Scheduler notify failed")
        if rows:
            self._emit("segments", {"items": rows})
            self._broadcast({"type": "segments", "items": rows})

    def _assign_speakers(self, segments: List[TranscriptSegment]) -> None:
        """Mic segments become 'Me'; loopback segments go to the diarizer."""
        by_chunk: Dict[Optional[int], List[TranscriptSegment]] = {}
        for seg in segments:
            if seg.channel == CHANNEL_MIC:
                seg.speaker_participant_id = self._me_participant_id
                seg.speaker_source = "channel"
            elif seg.channel == CHANNEL_LOOPBACK and self._diarizer is not None:
                by_chunk.setdefault(seg.chunk_id, []).append(seg)
        if not by_chunk:
            return
        try:
            from meeting.asr.audio import load_wav_int16, prepare_for_whisper
        except Exception:
            logger.exception("meeting.asr.audio unavailable; skipping diarization")
            return
        for chunk_id, chunk_segments in by_chunk.items():
            chunk = self._chunk_index.get(chunk_id) if chunk_id is not None else None
            if chunk is None:
                continue
            try:
                frames, rate = load_wav_int16(chunk.file_path)
            except Exception:
                logger.exception("Failed to reload chunk %s for diarization",
                                 chunk_id)
                continue
            for seg in chunk_segments:
                start = max(0, int((seg.start_s - chunk.start_s) * rate))
                end = min(len(frames), int((seg.end_s - chunk.start_s) * rate))
                if end <= start:
                    continue
                try:
                    audio = prepare_for_whisper(frames[start:end], rate)
                    participant_id = self._diarizer.assign(seg, audio, 16000)
                except Exception:
                    logger.exception("Diarizer assignment failed for %s",
                                     seg.segment_id)
                    participant_id = None
                if participant_id:
                    seg.speaker_participant_id = participant_id
                    seg.speaker_source = "diarizer"
                elif not self._degraded_diarization:
                    self._check_diarizer_degraded()

    @staticmethod
    def _segment_dict(seg: TranscriptSegment, created_at: str) -> Dict[str, Any]:
        return {
            "id": seg.segment_id,
            "meeting_id": seg.meeting_id,
            "chunk_id": seg.chunk_id,
            "channel": seg.channel,
            "start_s": seg.start_s,
            "end_s": seg.end_s,
            "text": seg.text,
            "speaker_participant_id": seg.speaker_participant_id,
            "speaker_source": seg.speaker_source,
            "speaker_pinned": seg.speaker_pinned,
            "created_at": created_at,
        }

    # ------------------------------------------------------------------
    # Diarization
    # ------------------------------------------------------------------

    def _start_diarizer(self) -> None:
        self._degraded_diarization = False
        diarizer = None
        try:
            from meeting.diarize.clustering import create_diarizer
            diarizer = create_diarizer(
                self.options.diarization_model_path, self.store,
                self.repository, self.meeting_id,
            )
        except Exception:
            logger.exception("Diarizer unavailable")
        available = False
        if diarizer is not None:
            try:
                available = bool(diarizer.is_available())
            except Exception:
                logger.exception("Diarizer availability probe failed")
        if available:
            try:
                diarizer.set_relabel_callback(self._on_diarizer_relabel)
            except Exception:
                logger.exception("Failed to register diarizer relabel callback")
            self._diarizer = diarizer
        else:
            self._diarizer = None
            logger.info("Diarization disabled; loopback stays channel-labeled")
        if self.store is not None:
            self.store.update_runtime_fields(diarization_available=available)

    def _check_diarizer_degraded(self) -> None:
        """Flip diarization off once the diarizer stops being available.

        Called only after ``assign`` declined to label a segment: a bare None
        is normal for segments too short to embed, so the availability probe
        is what distinguishes a quiet segment from a diarizer that failed
        mid-meeting. The diarizer object is kept so human speaker corrections
        still reach ``pin``.
        """
        diarizer = self._diarizer
        if diarizer is None or self._degraded_diarization:
            return
        try:
            if diarizer.is_available():
                return
        except Exception:
            logger.exception("Diarizer availability probe failed")
        self._degraded_diarization = True
        if self.store is not None:
            self.store.update_runtime_fields(diarization_available=False)
        logger.warning("Diarization degraded mid-meeting; loopback segments "
                       "fall back to channel-level labels")
        self._emit_status()

    def _on_diarizer_relabel(self, ops: List[Dict[str, Any]]) -> None:
        """Apply re-clustering relabel ops through the single-writer store."""
        if self.store is None or not ops:
            return
        try:
            self.store.apply("system", "diarizer", ops)
        except Exception:
            logger.exception("Diarizer relabel batch failed")

    def _handle_segment_op(self, result: OpResult) -> Optional[Dict[str, Any]]:
        """Store segment handler: persist a speaker reassignment.

        Args:
            result: The validated ``reassign_segment_speaker`` result whose
                ``effect`` carries segment_id/participant_id/source/pinned.

        Returns:
            The inverse op dict restoring the prior speaker, or None when the
            segment had no prior row.
        """
        effect = result.effect or {}
        segment_id = effect.get("segment_id")
        participant_id = effect.get("participant_id")
        source = effect.get("source", "human")
        pinned = bool(effect.get("pinned"))
        prior = None
        try:
            prior = self.repository.get_segment(self.meeting_id, segment_id)
        except Exception:
            logger.exception("Failed to read prior segment %s", segment_id)
        if prior is None:
            return None
        return {
            "op": "reassign_segment_speaker",
            "segment_id": segment_id,
            "participant_id": prior.get("speaker_participant_id"),
            "_source": prior.get("speaker_source", "channel"),
            "_pinned": bool(prior.get("speaker_pinned")),
            "force": True,
        }

    # ------------------------------------------------------------------
    # Web server
    # ------------------------------------------------------------------

    def _start_server(self) -> Optional[str]:
        try:
            from meeting.web.server import MeetingWebServer
            server = MeetingWebServer(
                self, self.repository,
                bind=self.options.server_bind, port=self.options.server_port,
            )
            url = server.start()
        except Exception as exc:
            logger.exception("Meeting web server failed to start")
            self._emit("error", {"code": "server_failed", "message": str(exc)})
            return None
        self._server = server
        self._emit("server_started", {
            "url": url,
            "host_url": server.host_url,
            "guest_url": server.guest_url,
        })
        return url

    # ------------------------------------------------------------------
    # Intelligence
    # ------------------------------------------------------------------

    def _maybe_start_intelligence(self) -> None:
        """Bring up agent core + scheduler when cloud processing is enabled.

        Failure is bounded: the meeting continues transcript-only with
        ``intelligence_online`` False and an ``intelligence`` error event. A
        core that initializes without raising but reports itself unhealthy is
        treated exactly the same way — the reported state is the core's own
        ``is_healthy()`` verdict, never the mere fact that objects were built.
        """
        if self.store is None:
            return
        if not self.store.with_state(lambda s: s.cloud_enabled):
            return
        created_core = None
        try:
            from meeting.agent.base import create_agent_core
            from meeting.agent.scheduler import CheckpointScheduler
            if self._agent_core is None:
                system_prompt = ""
                try:
                    from meeting.agent.prompts import build_system_prompt
                    system_prompt = build_system_prompt()
                except Exception:
                    logger.exception("build_system_prompt failed; using empty prompt")
                created_core = create_agent_core(
                    self.options.agent_core_kind, self.options.sidecar_payload_dir
                )
                created_core.initialize(
                    AgentConfig(
                        meeting_id=self.meeting_id,
                        provider=self.options.llm_provider,
                        model=self.options.llm_model,
                        api_key=None,  # resolved inside the agent layer
                        system_prompt=system_prompt,
                    ),
                    self,
                )
                self._agent_core = created_core
            if not self._core_is_healthy():
                # Locked degradation path: an unusable backend (no API key,
                # missing SDK, dead sidecar) never blocks the meeting.
                self._report_intelligence_unusable(created_core)
                return
            if self._scheduler is None:
                scheduler = CheckpointScheduler(
                    self, self._agent_core, on_health=self._on_intelligence_health,
                )
                self._seed_scheduler_watermark(scheduler)
                scheduler.start()
                self._scheduler = scheduler
            self.store.update_runtime_fields(intelligence_online=True)
            self._emit("intelligence", {"online": True})
            self._emit_status()
        except Exception as exc:
            logger.exception("Meeting intelligence failed to start")
            if created_core is not None and self._agent_core is created_core:
                self._agent_core = None
                try:
                    created_core.shutdown()
                except Exception:
                    logger.exception("Agent core cleanup failed")
            self.store.update_runtime_fields(intelligence_online=False)
            self._emit("intelligence", {"online": False, "error": str(exc)})
            self._emit_status()

    def _core_is_healthy(self) -> bool:
        """Whether the agent core can actually accept checkpoints."""
        core = self._agent_core
        if core is None:
            return False
        try:
            return bool(core.is_healthy())
        except Exception:
            logger.exception("Agent core health probe failed")
            return False

    def _report_intelligence_unusable(self, created_core: Optional[Any]) -> None:
        """Report a constructed-but-unusable agent core as offline.

        A core that initialized without raising can still be unusable (no API
        key, missing SDK, sidecar already gone). It is released so a later
        retry — the host re-toggling cloud processing after adding a key —
        builds a fresh one instead of reusing the dead instance.

        Args:
            created_core: The core created by this call, when any; a
                pre-existing core is left alone.
        """
        reason = ("The meeting-intelligence backend is unavailable "
                  "(no API key or backend); continuing transcript-only.")
        logger.warning("Agent core reported unhealthy after initialize; %s", reason)
        if created_core is not None and self._agent_core is created_core:
            self._shutdown_agent_core()
        if self.store is not None:
            self.store.update_runtime_fields(intelligence_online=False)
        self._emit("intelligence", {"online": False, "error": reason})
        self._emit_status(note=reason)

    def _on_intelligence_health(self, online: bool) -> None:
        """Scheduler health callback: publish the real intelligence state.

        Args:
            online: True when checkpoints succeed again, False when the
                scheduler declared intelligence offline after repeated
                failures.
        """
        online = bool(online)
        if self.store is not None:
            self.store.update_runtime_fields(intelligence_online=online)
        payload: Dict[str, Any] = {"online": online}
        if not online:
            payload["error"] = ("Checkpoints failed repeatedly; continuing "
                                "transcript-only.")
        self._emit("intelligence", payload)
        self._emit_status()

    def _seed_scheduler_watermark(self, scheduler: Any) -> None:
        """Mark the meeting-to-date transcript as already delivered.

        Re-enabling cloud processing builds a fresh ``CheckpointScheduler``
        whose send cursor starts at the beginning of the meeting, so its next
        checkpoint would ship the whole transcript — including everything
        spoken while cloud processing was off. Seeding it with the existing
        segments keeps re-enabling forward-only.

        Args:
            scheduler: The freshly constructed scheduler, not yet started.
        """
        if not self._intelligence_restarted:
            return
        seed = getattr(scheduler, "seed_sent_segments", None)
        if not callable(seed):
            seed = getattr(scheduler, "_mark_sent", None)
        if not callable(seed):
            logger.warning("Checkpoint scheduler exposes no send-cursor seam; "
                           "the next checkpoint may resend earlier transcript")
            return
        try:
            seed(self.get_transcript())
        except Exception:
            logger.exception("Failed to seed the checkpoint scheduler cursor")

    def _stop_intelligence(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:
                logger.exception("Scheduler stop failed")
        if self._agent_core is not None:
            try:
                self._agent_core.cancel()
            except Exception:
                logger.exception("Agent cancel failed")
        # Revoked consent must not leave a sidecar process alive holding the
        # API key and an open session.
        self._shutdown_agent_core()
        self._intelligence_restarted = True
        if self.store is not None:
            self.store.update_runtime_fields(intelligence_online=False)
        self._emit("intelligence", {"online": False})
        self._emit_status()

    def set_cloud_enabled(self, enabled: bool) -> None:
        """Toggle cloud processing mid-meeting (host action).

        Args:
            enabled: True starts (or restarts) the intelligence layer,
                resuming from the current point in the transcript; False stops
                checkpoints, cancels any in-flight request, and releases the
                agent core so no process keeps holding the API key.
        """
        if self.store is None:
            return
        results = self.store.apply("host", self._me_participant_id, [{
            "op": "set_cloud_enabled", "enabled": bool(enabled),
        }])
        if not results or not results[0].ok:
            raise RuntimeError(
                results[0].reason if results else "cloud state update failed"
            )
        if enabled:
            self._maybe_start_intelligence()
        else:
            self._stop_intelligence()

    def regenerate_tokens(self) -> Dict[str, str]:
        """Rotate host/guest capability links and disconnect old sessions."""
        if not self.meeting_id:
            raise RuntimeError("No active meeting")
        from meeting.web.auth import generate_token_pair

        host_token, guest_token = generate_token_pair()
        self.repository.replace_tokens(
            self.meeting_id, host_token, guest_token
        )
        server = self._server
        host_url = getattr(server, "host_url", "") if server else ""
        guest_url = getattr(server, "guest_url", "") if server else ""
        self._emit("server_started", {
            "url": getattr(server, "base_url", "") if server else "",
            "host_url": host_url,
            "guest_url": guest_url,
        })
        if server is not None:
            invalidate = getattr(server, "invalidate_connections", None)
            if callable(invalidate):
                invalidate()
        return {"host_url": host_url, "guest_url": guest_url}

    # ------------------------------------------------------------------
    # Client actions (web server entry points)
    # ------------------------------------------------------------------

    def apply_client_action(self, actor_type: str, actor_id,
                            op: Dict[str, Any]) -> List[OpResult]:
        """Apply one human dashboard op through the single-writer store.

        Args:
            actor_type: ``host`` | ``user`` (guests act as ``user``).
            actor_id: The acting participant's id, when known.
            op: An op dict from the shared vocabulary.

        Returns:
            The store's per-op results (a single-element list).
        """
        if self.store is None:
            return [OpResult(ok=False, op=dict(op), reason="inactive")]
        results = self.store.apply(actor_type, actor_id, [op])
        self._apply_diarizer_pins(results)
        return results

    def undo(self, seq: int, actor_id) -> List[OpResult]:
        """Host undo of a past event by its ``seq`` (empty when impossible)."""
        if self.store is None:
            return []
        results = self.store.undo(seq, actor_id)
        self._apply_diarizer_pins(results)
        return results

    def _apply_diarizer_pins(self, results: List[OpResult]) -> None:
        """Teach the diarizer only after a speaker edit commits."""
        if self._diarizer is None:
            return
        for result in results:
            effect = result.effect or {}
            if (not result.ok or effect.get("entity") != "segment_speaker"
                    or not effect.get("pinned")
                    or effect.get("source") != "human"
                    or not effect.get("participant_id")):
                continue
            try:
                self._diarizer.pin(
                    effect["segment_id"], effect["participant_id"]
                )
            except Exception:
                logger.exception("Diarizer pin failed for %s", effect["segment_id"])

    def add_guest(self, display_name: str) -> Dict[str, Any]:
        """Create a guest participant for a joining dashboard client.

        Args:
            display_name: The name the guest joined with (clamped to 80 chars).

        Returns:
            The created participant dict.

        Raises:
            RuntimeError: When no meeting is active.
            ValueError: When the participant op was rejected.
        """
        if self.store is None:
            raise RuntimeError("No active meeting")
        name = (display_name or "").strip()[:MAX_GUEST_NAME_LEN] or "Guest"
        results = self.store.apply("system", None, [{
            "op": "upsert_participant", "display_name": name,
            "kind": "guest", "is_provisional": False,
        }])
        result = results[0]
        if not result.ok or not result.effect:
            raise ValueError(result.reason or "guest_rejected")
        return dict(result.effect["participant"])

    def get_transcript(self, after_start_s: float = -1.0,
                       limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Transcript segment dicts for this meeting, in timeline order."""
        if not self.meeting_id:
            return []
        return self.repository.get_segments(
            self.meeting_id, after_start_s=after_start_s, limit=limit
        )

    # ------------------------------------------------------------------
    # AgentToolHost
    # ------------------------------------------------------------------

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply state-patch ops on behalf of the agent."""
        if self.store is None:
            return [
                OpResult(ok=False,
                         op=op if isinstance(op, dict) else {"op": op},
                         reason="inactive")
                for op in ops
            ]
        return self.store.apply("agent", "agent", list(ops))

    def ask_question(self, text: str, evidence: List[str]) -> OpResult:
        """Add a question to the quiet inbox (agent tool)."""
        return self._apply_single_agent_op({
            "op": "ask_question", "text": text,
            "evidence": list(evidence or []),
        })

    def resolve_question(self, question_id: str, answer_text: str,
                         confidence: float, evidence: List[str]) -> OpResult:
        """Answer an open question from audio evidence (agent tool)."""
        return self._apply_single_agent_op({
            "op": "resolve_question", "question_id": question_id,
            "answer_text": answer_text, "confidence": confidence,
            "evidence": list(evidence or []),
        })

    def _apply_single_agent_op(self, op: Dict[str, Any]) -> OpResult:
        if self.store is None:
            return OpResult(ok=False, op=op, reason="inactive")
        return self.store.apply("agent", "agent", [op])[0]

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_snapshot(self) -> None:
        """Write the current full state snapshot to the meeting row."""
        if self.store is None or not self.meeting_id:
            return
        try:
            self.repository.update_meeting(
                self.meeting_id,
                state_json=json.dumps(self.store.snapshot(), ensure_ascii=False),
                state_seq=self.store.seq,
            )
        except Exception:
            logger.exception("Failed to persist state snapshot")
