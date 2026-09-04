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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

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
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)

#: Seconds between repository heartbeats while a meeting is live.
HEARTBEAT_INTERVAL_S = 10.0
#: ASR drain budget for a normal, user-initiated end.
END_DRAIN_TIMEOUT_S = 300.0
#: Cap on the forced end-of-meeting ASR polish so consolidation can start.
END_REVISE_TIMEOUT_S = 20.0
#: Shorter drain budget when the whole app is shutting down.
SHUTDOWN_DRAIN_TIMEOUT_S = 30.0
#: Budget for the post-end transcript polish (LLM parse of the clean ASR).
POLISH_TIMEOUT_S = 60.0
#: Worst-case wait for the post-end consolidation pass (hard wall). The
#: sidecar fails earlier if Pi emits no ``subscribe`` progress for
#: ``CONSOLIDATION_STALL_S``. Used for shutdown join, not the live stall.
CONSOLIDATION_TIMEOUT_S = 900.0
#: How long end/cancel waits for an in-flight ``start()`` to finish before
#: unwinding a partially built pipeline anyway.
START_WAIT_TIMEOUT_S = 120.0
#: Guest display names are clamped to this length.
MAX_GUEST_NAME_LEN = 80
#: Agent activity ticks kept for a host that opens the dashboard mid-meeting.
AGENT_ACTIVITY_HISTORY = 50
#: Minimum spacing between broadcasts of the same activity kind + tool, so a
#: long think or a burst of tool calls cannot flood the socket.
AGENT_ACTIVITY_MIN_INTERVAL_S = 1.0
# Poll frequently enough that failure detection + SoundCard first-block wait
# (``_START_TIMEOUT_S`` ≈ 2.5s) still fit the ≤12s restoration bound.
CAPTURE_WATCHDOG_INTERVAL_S = 1.0
CAPTURE_RETRY_INTERVAL_S = 3.0
#: Worst-case budget from a failed source until a successful restart attempt
#: must begin (poll jitter + retry spacing + start wait must stay under 12s).
CAPTURE_RECOVERY_BUDGET_S = 12.0

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
    asr_language: str = 'auto'
    llm_provider: str = 'openrouter'
    llm_model: str = ''
    llm_endpoint: Optional[Dict[str, Any]] = None
    agent_core_kind: str = 'pi'   # 'pi' | 'direct'
    sidecar_payload_dir: Optional[str] = None
    diarization_model_path: Optional[str] = None
    speaker_id_backend: str = 'local'  # 'local' | 'openai'
    speaker_id_audio_consent: bool = False
    server_bind: str = 'localhost'    # 'localhost' | 'lan'
    server_port: int = 0
    spool_root: str = ''              # parent dir for meeting spool dirs
    end_redecode: bool = False
    end_polish: bool = True
    end_report: bool = True
    report_views: Tuple[str, ...] = ('ribbon', 'brief', 'signal')
    demo_mode: bool = False
    #: System-audio policy for this session only (never persisted).
    #: ``auto`` keeps existing Windows/macOS degrade-to-mic-only behavior;
    #: ``required`` is Linux dual-channel readiness; ``disabled`` is an
    #: explicit microphone-only choice for the current meeting.
    system_audio_policy: str = 'auto'


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
        self._system_audio_disabled = False
        self._loopback_was_available = False
        self._explicit_capture_message: Optional[str] = None
        self._asr: Optional[Any] = None
        self._diarizer: Optional[Any] = None
        self._server: Optional[Any] = None
        self._agent_core: Optional[Any] = None
        self._scheduler: Optional[Any] = None
        # Agent tool writes are allowed for live checkpoints and the active
        # final consolidation pass only. Late workers after timeout/cancel
        # must not mutate durable state.
        self._agent_writes_allowed = True
        # Latches once the diarizer stops labeling, so the dashboard banner is
        # corrected exactly once per meeting.
        self._degraded_diarization = False
        # True once intelligence has been stopped at least once: a rebuilt
        # scheduler must not re-ship the whole meeting-to-date transcript.
        self._intelligence_restarted = False

        self._chunk_lock = threading.Lock()
        self._chunk_index: Dict[int, SpooledChunk] = {}
        self._enqueued_chunk_ids: set = set()

        # Ephemeral agent activity: never persisted, never part of the state
        # store. Written from the agent core's reader thread, read from the
        # web server's event loop when a host connects.
        self._activity_lock = threading.Lock()
        self._agent_activity: Deque[Dict[str, str]] = deque(
            maxlen=AGENT_ACTIVITY_HISTORY
        )
        self._agent_activity_last: Dict[Tuple[str, str], float] = {}

        self._hb_stop: Optional[threading.Event] = None
        self._hb_thread: Optional[threading.Thread] = None

    def add_listener(self, cb: Listener) -> None:
        """Register an event listener ``cb(kind, payload)``.

        Kinds: ``status``, ``segments``, ``server_started``, ``error``,
        ``ended``, ``intelligence``. Callbacks may fire from worker threads.
        """
        with self._listener_lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(kind, payload)
            except Exception:
                logger.exception("Meeting listener raised for %s event", kind)

    def _stop_asr(self, context: str) -> None:
        """Stop the ASR engine and announce that its model is freed.

        ``asr_released`` is deliberately a separate event from ``ended``:
        that one fires before the offline final pass, which reuses the
        still-loaded model. Only after ``stop()`` are the weights actually
        gone, so only then may a listener load an engine of its own.

        Args:
            context: Short description of the teardown path, for logging.
        """
        asr = self._asr
        if asr is None:
            return
        try:
            asr.stop()
        except Exception:
            logger.exception("ASR stop failed (%s)", context)
        self._emit("asr_released", {"meeting_id": self.meeting_id})

    def _broadcast(self, message: Dict[str, Any], *,
                   host_only: bool = False) -> None:
        """Push a message to dashboard clients, optionally to hosts only.

        Args:
            message: JSON-serializable payload.
            host_only: When True, only host-authenticated sockets receive it.
                A transport that predates the keyword drops the message rather
                than leaking host-only content to guests.
        """
        server = self._server
        if server is None:
            return
        try:
            if host_only:
                server.broadcast(message, host_only=True)
            else:
                server.broadcast(message)
        except TypeError:
            if host_only:
                logger.debug("Transport has no host-only broadcast; dropped %r",
                             message.get("type"))
                return
            logger.exception("Meeting web broadcast failed")
        except Exception:
            logger.exception("Meeting web broadcast failed")

    def _emit_status(self, note: Optional[str] = None) -> None:
        """Emit the current status to listeners and connected web clients."""
        if self.store is None:
            return
        status, intel, diar, capture, finalization = self.store.with_state(
            lambda s: (
                s.status, s.intelligence_online, s.diarization_available,
                dict(s.capture), s.finalization.to_dict(),
            )
        )
        payload = {
            "status": status,
            "intelligence_online": intel,
            "diarization_available": diar,
            "capture": capture,
            "finalization": dict(finalization),
        }
        listener_payload = dict(payload)
        if note:
            listener_payload["note"] = note
        self._emit("status", listener_payload)
        self._broadcast({"type": "status", **payload})

    def allow_agent_writes(self) -> None:
        """Permit agent tool-host mutations (live checkpoints / consolidation)."""
        self._agent_writes_allowed = True

    def revoke_agent_writes(self) -> None:
        """Reject further agent tool-host mutations.

        Called before terminal consolidation outcomes and on cancel/failure/
        shutdown so a late worker cannot apply patches after authority ends.
        """
        self._agent_writes_allowed = False

    def agent_writes_allowed(self) -> bool:
        """True while agent tool-host methods may mutate meeting state."""
        return bool(self._agent_writes_allowed)

    def _set_finalization(
        self,
        status: str,
        message: str = "",
        *,
        stage: str = "",
        current_step: int = 0,
        total_steps: int = 0,
        step_details: str = "",
        steps: Optional[List[Dict[str, Any]]] = None,
        summary_stats: Optional[Dict[str, Any]] = None,
        emit: bool = True,
    ) -> bool:
        """Persist and optionally broadcast a finalization outcome.

        Persistence remains authoritative. When a terminal outcome cannot be
        written, an ephemeral status override is still emitted so desktop and
        browser clients unlock and warn without inventing durable success.

        Args:
            status: One of the ``FINALIZATION_STATUSES`` values.
            message: Human-readable detail for the UI.
            stage: Current processing stage identifier.
            current_step: 1-based index of current step.
            total_steps: Total count of active steps.
            step_details: Detailed live progress information.
            steps: List of step progress dictionary records.
            summary_stats: Final meeting statistics summary dict.
            emit: When True, push the update through status listeners/WS.

        Returns:
            True when the finalization value was persisted successfully.
        """
        if self.store is None:
            return False
        from meeting.state.schema import FINALIZATION_STATUSES, FinalizationState

        payload = {
            "status": status,
            "message": message,
            "stage": stage,
            "current_step": current_step,
            "total_steps": total_steps,
            "step_details": step_details,
        }
        if steps is not None:
            payload["steps"] = [dict(s) for s in steps]
        if summary_stats is not None:
            payload["summary_stats"] = dict(summary_stats)
        current = self.store.with_state(lambda state: state.finalization)
        if current is not None and getattr(current, "card_deferred", False):
            payload["card_deferred"] = True

        finalization = FinalizationState.coerce(
            payload,
            cloud_enabled=self.store.with_state(lambda s: s.cloud_enabled),
            meeting_status=self.store.with_state(lambda s: s.status),
        )
        terminal = finalization.status in {
            "completed", "disabled", "unavailable", "failed",
        }
        if finalization.status not in FINALIZATION_STATUSES:
            return False
        if not self.store.update_runtime_fields(finalization=finalization):
            logger.warning(
                "Could not persist finalization status=%s", finalization.status,
            )
            if emit and terminal:
                # Ephemeral unlock/warn only — do not claim durable success.
                self._emit_ephemeral_finalization(finalization)
            return False
        if terminal:
            self._adopt_untitled_title_from_topic()
        if emit:
            self._emit_status()
        return True

    def _adopt_untitled_title_from_topic(self) -> None:
        """Copy topic.current into title when the host never named the meeting."""
        if self.store is None:
            return
        try:
            from meeting.content import build_untitled_title_ops

            ops = build_untitled_title_ops(self.store.snapshot())
            if ops:
                self.store.apply("system", "title_from_topic", ops)
        except Exception:
            logger.exception("Could not adopt untitled meeting title from topic")

    def _emit_ephemeral_finalization(self, finalization: Any) -> None:
        """Broadcast a non-persisted finalization override for UI unlock.

        Args:
            finalization: ``FinalizationState`` or mapping with status/message.
        """
        if self.store is None:
            return
        try:
            payload_fin = (
                finalization.to_dict()
                if hasattr(finalization, "to_dict")
                else dict(finalization)
            )
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
                "finalization": dict(payload_fin),
            }
            self._emit("status", dict(payload))
            self._broadcast({"type": "status", **payload})
        except Exception:
            logger.exception("Ephemeral finalization emit failed")

    def is_active(self) -> bool:
        """True from a successful ``start()`` until end/cancel completes."""
        return self._active

    def start(self) -> Dict[str, Any]:
        """Create the meeting and bring the whole pipeline up.

        Degrades gracefully when one capture channel, the ASR model, or the
        intelligence backend is unavailable. The dashboard and at least one
        live capture source are required: without either one the partial start
        is torn down and the meeting is not reported as active.
        ``options.demo_mode`` skips capture, ASR, and diarization and seeds a
        canned transcript so End can be tested without a live recording.

        Returns:
            ``{'meeting_id', 'url', 'host_url', 'guest_url'}``.

        Raises:
            RuntimeError: When the engine is already active or was ended.
            Exception: When required setup fails; partial setup is torn down
                and an empty meeting row is discarded.
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
                agent_endpoint_json=(
                    json.dumps(self.options.llm_endpoint)
                    if self.options.llm_endpoint else None
                ),
                spool_dir=spool_dir,
                state_json=None,
                state_seq=0,
                app_pid=os.getpid(),
                app_heartbeat_at=started_at,
            )
            self.meeting_id = meeting_id
            self._spool_dir = spool_dir

            from meeting.state.schema import FinalizationState

            state = MeetingState(
                meeting_id=meeting_id,
                cloud_enabled=self.options.cloud_enabled,
                title=self.options.title,
                finalization=FinalizationState.default_for_cloud(
                    self.options.cloud_enabled
                ),
                report_views=list(self.options.report_views),
            )
            self.store = MeetingStateStore(
                state,
                repository=self.repository,
                segment_handler=make_segment_handler(
                    self.repository, meeting_id
                ),
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

            if self.options.demo_mode:
                capture_note = self._seed_demo_meeting()
            else:
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

    def _seed_demo_meeting(self) -> str:
        """Load canned transcript/cards and skip live capture.

        Returns:
            User-facing status note for the dashboard banner.
        """
        from meeting.dev_fixture import DEMO_NOTE, seed_demo_meeting

        if self.store is None or not self.meeting_id:
            return DEMO_NOTE
        self.store.update_runtime_fields(
            capture={
                "mic_available": False,
                "loopback_available": False,
                "message": DEMO_NOTE,
            },
            diarization_available=False,
        )
        seed_demo_meeting(
            meeting_id=self.meeting_id,
            me_participant_id=self._me_participant_id,
            store=self.store,
            repository=self.repository,
            clock=self.clock,
            title=self.options.title or None,
        )
        logger.info(
            "Demo meeting seeded for %s (no capture or ASR)",
            self.meeting_id,
        )
        return DEMO_NOTE

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
            # Flip the dashboard off "Live" immediately — drain + consolidation
            # can take a while, and leaving status=active looks hung.
            if self.store is not None:
                try:
                    self.store.update_runtime_fields(status="ending")
                except Exception:
                    logger.exception("Could not mark meeting as ending")
                else:
                    self._emit_status()
            # Stop rolling checkpoints/polish and cancel any in-flight LLM turn
            # so end is not blocked waiting for a live pass to finish.
            scheduler = self._scheduler
            if scheduler is not None:
                prepare = getattr(scheduler, "prepare_for_end", None)
                if callable(prepare):
                    try:
                        prepare()
                    except Exception:
                        logger.exception("Scheduler prepare_for_end failed")
            self._stop_capture()
            self._flush_spools()
            drained = True
            if self._asr is not None:
                try:
                    drained = bool(self._asr.drain(drain_timeout_s))
                except Exception:
                    logger.exception("ASR drain failed at meeting end")
                    drained = False
                # Flush deferred rolling revises before consolidation sees the
                # transcript, while the Whisper model is still loaded — but
                # bound the wait so a long polish queue cannot delay insights.
                run_pending = getattr(self._asr, "run_pending_revises", None)
                if drained and callable(run_pending):
                    revise_deadline = time.monotonic() + END_REVISE_TIMEOUT_S
                    try:
                        for outcome in run_pending(
                            force=True, deadline_mono=revise_deadline,
                        ):
                            self._publish_revise_result(outcome)
                    except TypeError:
                        # Older ASR engines without deadline_mono.
                        try:
                            for outcome in run_pending(force=True):
                                self._publish_revise_result(outcome)
                                if time.monotonic() >= revise_deadline:
                                    logger.warning(
                                        "End-of-meeting ASR revise flush timed "
                                        "out after %.0fs; continuing to "
                                        "consolidation",
                                        END_REVISE_TIMEOUT_S,
                                    )
                                    break
                        except Exception:
                            logger.exception(
                                "End-of-meeting ASR revise flush failed"
                            )
                    except Exception:
                        logger.exception("End-of-meeting ASR revise flush failed")
            try:
                unfinished = self.repository.count_unfinished_chunks(
                    self.meeting_id
                )
            except Exception:
                logger.exception("Could not verify unfinished ASR chunks")
                unfinished = 1
            complete = drained and unfinished == 0
            scheduler = self._scheduler
            asr = self._asr
            will_offline = bool(
                self.options.end_redecode
                and asr is not None
                and callable(getattr(asr, "transcribe_offline_session", None))
            )
            will_speaker_id = bool(
                not self.options.demo_mode
                and self.options.speaker_id_backend == "openai"
            )
            # Immediate repair still uses the live draft so the dashboard is
            # useful the moment capture ends; the offline pass may replace it.
            if self.store is not None:
                try:
                    from meeting.state.repair import repair_meeting_state

                    repair_meeting_state(self.store, self.get_transcript())
                except Exception:
                    logger.exception("Immediate end-state repair failed")
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

            cloud_enabled = False
            if self.store is not None:
                cloud_enabled = bool(
                    self.store.with_state(lambda s: s.cloud_enabled)
                )
            want_polish = bool(self.options.end_polish)
            want_report = bool(self.options.end_report)
            run_cloud = bool(
                cloud_enabled
                and complete
                and scheduler is not None
                and self._core_is_healthy()
                and (want_polish or want_report)
            )

            steps: List[Dict[str, Any]] = []
            if will_offline:
                steps.append({
                    "id": "redecode",
                    "name": "Audio Re-transcription",
                    "status": "pending",
                    "detail": "High-accuracy full session Whisper decode",
                })
            if will_speaker_id:
                steps.append({
                    "id": "speaker_id",
                    "name": "Speaker Identification",
                    "status": "pending",
                    "detail": "OpenAI labels on the system-audio recording",
                })
            if run_cloud:
                if want_polish:
                    steps.append({
                        "id": "polish",
                        "name": "Transcript Cleanup",
                        "status": "pending",
                        "detail": "AI grammar, punctuation, and speaker formatting",
                    })
                if want_report:
                    steps.append({
                        "id": "consolidation",
                        "name": "Summary & Action Items",
                        "status": "pending",
                        "detail": "Synthesizing executive summary, key points, decisions, and action items",
                    })
            steps.append({
                "id": "finalize",
                "name": "State Finalization",
                "status": "pending",
                "detail": "Saving final transcript and consolidating meeting state",
            })
            total_steps = len(steps)

            def _update_step(step_id: str, step_status: str, detail_msg: str = "", *, message: str = "", emit: bool = True) -> None:
                curr_idx = 1
                for idx, s in enumerate(steps, 1):
                    if s["id"] == step_id:
                        s["status"] = step_status
                        if detail_msg:
                            s["detail"] = detail_msg
                        curr_idx = idx
                        break
                top_msg = message or (
                    "Re-transcribing meeting…" if step_id == "redecode"
                    else "Identifying speakers…" if step_id == "speaker_id"
                    else "Cleaning transcript…" if step_id == "polish"
                    else "Preparing final report…" if step_id == "consolidation"
                    else "Finalizing meeting state…"
                )
                self._set_finalization(
                    "running",
                    top_msg,
                    stage=step_id,
                    current_step=curr_idx,
                    total_steps=total_steps,
                    step_details=detail_msg,
                    steps=steps,
                    emit=emit,
                )

            if will_offline or will_speaker_id or run_cloud:
                _update_step(steps[0]["id"], "running", steps[0]["detail"], emit=False)
            elif not cloud_enabled:
                self._set_finalization(
                    "disabled",
                    "Cloud intelligence is off for this meeting.",
                    emit=False,
                )
            elif not complete:
                self._set_finalization(
                    "unavailable",
                    (
                        "Final cloud insights are unavailable until "
                        "transcription recovery finishes."
                    ),
                    emit=False,
                )
            else:
                self._set_finalization(
                    "unavailable",
                    (
                        "Meeting intelligence is offline; final cloud "
                        "insights could not run."
                    ),
                    emit=False,
                )

            self._active = False
            self._broadcast({"type": "meeting_ended", "status": status})
            self._emit_status()
            self._emit("ended", {
                "meeting_id": self.meeting_id,
                "canceled": False,
                "status": status,
                "unfinished_chunks": unfinished,
            })

            offline_ok = False
            if will_offline:
                _update_step(
                    "redecode",
                    "running",
                    "Starting high-accuracy session audio re-decoding...",
                    message="Re-transcribing meeting…",
                )
                def _offline_progress(detail: str, curr_win: int, total_win: int) -> None:
                    _update_step(
                        "redecode",
                        "running",
                        detail,
                        message=f"Re-transcribing meeting (window {curr_win}/{total_win})…",
                    )
                try:
                    offline_ok = bool(self._run_offline_final_pass(progress_cb=_offline_progress))
                except Exception:
                    logger.exception("Offline clean ASR pass failed")
                    offline_ok = False
                _update_step(
                    "redecode",
                    "completed" if offline_ok else "failed",
                    "High-accuracy re-decoding complete" if offline_ok else "Re-decoding failed; kept live transcript",
                )

            speaker_ok = False
            speaker_skipped = False
            speaker_error = ""
            if will_speaker_id:
                _update_step(
                    "speaker_id",
                    "running",
                    "Uploading system audio for speaker labels…",
                    message="Identifying speakers…",
                )

                def _speaker_progress(detail: str, curr: int, total: int) -> None:
                    _update_step(
                        "speaker_id",
                        "running",
                        detail,
                        message=(
                            f"Identifying speakers (window {curr}/{total})…"
                        ),
                    )

                try:
                    speaker_result = self._run_cloud_speaker_pass(
                        progress_cb=_speaker_progress,
                    )
                except Exception:
                    logger.exception("Cloud speaker identification failed")
                    speaker_result = {
                        "ok": False, "skipped": False,
                        "error": "Speaker identification failed.",
                    }
                speaker_ok = bool(speaker_result.get("ok"))
                speaker_skipped = bool(speaker_result.get("skipped"))
                speaker_error = str(speaker_result.get("error") or "")
                if speaker_ok:
                    applied = int(speaker_result.get("applied") or 0)
                    detail = (
                        f"Updated {applied} speaker label"
                        f"{'' if applied == 1 else 's'}"
                    )
                    step_status = "completed"
                elif speaker_skipped:
                    detail = speaker_error or "Speaker identification skipped."
                    step_status = "completed"
                else:
                    detail = speaker_error or "Speaker identification failed."
                    step_status = "failed"
                _update_step("speaker_id", step_status, detail)

            if run_cloud and not complete and not offline_ok:
                run_cloud = False
                self._set_finalization(
                    "unavailable",
                    (
                        "Final cloud insights are unavailable until "
                        "transcription recovery finishes."
                    ),
                )

            if run_cloud and scheduler is not None:
                try:
                    self.allow_agent_writes()
                    if want_polish:
                        _update_step(
                            "polish",
                            "running",
                            "Starting AI transcript cleanup and formatting...",
                            message="Cleaning transcript…",
                        )
                        def _polish_progress(detail: str, curr_blk: int, total_blks: int) -> None:
                            _update_step(
                                "polish",
                                "running",
                                detail,
                                message=f"Cleaning transcript (block {curr_blk}/{total_blks})…",
                            )
                        polish = getattr(scheduler, "run_final_polish", None)
                        polish_status = "completed"
                        polish_detail = "Transcript cleanup finished"
                        if callable(polish):
                            try:
                                try:
                                    polish_outcome = polish(
                                        timeout_s=POLISH_TIMEOUT_S,
                                        progress_cb=_polish_progress,
                                    )
                                except TypeError:
                                    polish_outcome = polish(
                                        timeout_s=POLISH_TIMEOUT_S,
                                    )
                                polish_status = getattr(
                                    polish_outcome, "status", "completed",
                                )
                                polish_detail = (
                                    getattr(polish_outcome, "message", "")
                                    or (
                                        "Transcript cleanup finished"
                                        if polish_status == "completed"
                                        else "Transcript cleanup failed"
                                    )
                                )
                            except Exception as exc:
                                logger.exception("Post-end transcript polish failed")
                                polish_status = "failed"
                                polish_detail = f"Transcript cleanup failed: {exc}"
                        _update_step(
                            "polish",
                            "completed" if polish_status == "completed" else "failed",
                            polish_detail,
                        )
                    if want_report:
                        _update_step(
                            "consolidation",
                            "running",
                            "Synthesizing executive summary, key points, decisions, and action items...",
                            message="Preparing final report…",
                        )
                        def _consolidation_progress(detail: str) -> None:
                            _update_step(
                                "consolidation",
                                "running",
                                detail,
                                message="Preparing final report…",
                            )
                        # live_notes is preserved into consolidation so the
                        # final report pass can synthesize the meeting notes
                        # and reconcile them against the final transcript.
                        try:
                            outcome = scheduler.run_consolidation(
                                progress_cb=_consolidation_progress,
                            )
                        except TypeError:
                            outcome = scheduler.run_consolidation()
                        status = getattr(outcome, "status", "failed")
                        message = getattr(outcome, "message", "") or ""
                        _update_step(
                            "consolidation",
                            "completed" if status == "completed" else "failed",
                            "Summary & action items ready" if status == "completed" else message,
                        )
                    else:
                        status = "completed"
                        message = "Transcript cleanup is ready."
                except Exception as exc:
                    logger.exception("Post-end consolidation pass failed")
                    self.revoke_agent_writes()
                    self._set_finalization(
                        "failed",
                        f"Final cloud insights failed: {exc}",
                        stage="failed",
                        current_step=total_steps,
                        total_steps=total_steps,
                        step_details=f"Final insights failed: {exc}",
                        steps=steps,
                    )
                else:
                    self.revoke_agent_writes()
                    _update_step(
                        "finalize",
                        "running",
                        "Saving final transcript and meeting state...",
                        message="Finalizing meeting state…",
                    )
                    summary_stats: Dict[str, Any] = {
                        "segments": 0,
                        "words": 0,
                        "key_points": 0,
                        "action_items": 0,
                        "decisions": 0,
                        "risks": 0,
                        "questions": 0,
                        "duration_s": 0.0,
                    }
                    try:
                        summary_stats["duration_s"] = float(self.clock.elapsed_s())
                    except Exception:
                        pass
                    if self.store is not None:
                        try:
                            cards = self.store.with_state(lambda s: dict(s.cards))
                            questions = self.store.with_state(lambda s: list(s.questions))
                            summary_stats["key_points"] = len(cards.get("key_points", []))
                            summary_stats["action_items"] = len(cards.get("action_items", []))
                            summary_stats["decisions"] = len(cards.get("decisions", []))
                            summary_stats["risks"] = len(cards.get("risks", []))
                            summary_stats["questions"] = len(questions)
                        except Exception:
                            pass
                    try:
                        transcript = self.get_transcript()
                        summary_stats["segments"] = len(transcript)
                        summary_stats["words"] = sum(len(str(seg.get("text") or "").split()) for seg in transcript)
                    except Exception:
                        pass

                    _update_step(
                        "finalize",
                        "completed",
                        f"Saved {summary_stats['segments']} segments ({summary_stats['words']} words)",
                    )

                    if any(s.get("status") == "failed" for s in steps):
                        status = "failed"
                        failed_names = [
                            str(s.get("name") or s.get("id"))
                            for s in steps
                            if s.get("status") == "failed"
                        ]
                        final_msg = (
                            f"{', '.join(failed_names)} failed. "
                            "The recording and transcript were kept."
                        )
                    elif status == "completed":
                        if not want_report:
                            final_msg = message
                        else:
                            parts = [f"{summary_stats['segments']} segments"]
                            if summary_stats["key_points"]:
                                parts.append(f"{summary_stats['key_points']} key points")
                            if summary_stats["action_items"]:
                                parts.append(f"{summary_stats['action_items']} action items")
                            if summary_stats["decisions"]:
                                parts.append(f"{summary_stats['decisions']} decisions")
                            summary_line = ", ".join(parts)
                            final_msg = f"Final insights ready — {summary_line}." if summary_line else "Final cloud insights are ready."
                    else:
                        final_msg = message

                    self._set_finalization(
                        status,
                        final_msg,
                        stage="complete" if status == "completed" else status,
                        current_step=total_steps,
                        total_steps=total_steps,
                        step_details=final_msg,
                        steps=steps,
                        summary_stats=summary_stats,
                    )
            else:
                self.revoke_agent_writes()
                if will_speaker_id:
                    _update_step(
                        "finalize",
                        "completed",
                        "Saved meeting state after speaker identification.",
                    )
                    if speaker_ok:
                        final_status = "completed"
                        final_msg = (
                            "Speaker identification finished."
                            if cloud_enabled else
                            "Speaker identification finished. "
                            "Cloud intelligence is off for this meeting."
                        )
                    elif speaker_skipped:
                        final_status = "completed"
                        final_msg = speaker_error or (
                            "Speaker identification skipped."
                        )
                    else:
                        final_status = "failed"
                        final_msg = speaker_error or (
                            "Speaker identification failed."
                        )
                    self._set_finalization(
                        final_status,
                        final_msg,
                        stage=(
                            "complete" if final_status == "completed"
                            else final_status
                        ),
                        current_step=total_steps,
                        total_steps=total_steps,
                        step_details=final_msg,
                        steps=steps,
                    )
                elif not cloud_enabled:
                    self._set_finalization(
                        "disabled",
                        "Cloud intelligence is off for this meeting.",
                    )
                elif not complete and not offline_ok:
                    self._set_finalization(
                        "unavailable",
                        (
                            "Final cloud insights are unavailable until "
                            "transcription recovery finishes."
                        ),
                    )
                elif not want_polish and not want_report:
                    self._set_finalization(
                        "completed",
                        "Post-meeting cleanup and report are off.",
                    )
                elif scheduler is None or not self._core_is_healthy():
                    self._set_finalization(
                        "unavailable",
                        (
                            "Meeting intelligence is offline; final cloud "
                            "insights could not run."
                        ),
                    )
            if scheduler is not None:
                try:
                    scheduler.stop()
                except Exception:
                    logger.exception("Scheduler stop failed")
                self._scheduler = None
            self._stop_asr("end")
            self._shutdown_agent_core()
        except Exception as exc:
            logger.exception("Meeting end failed")
            self._active = False
            self.revoke_agent_writes()
            self._emit("error", {"code": "end_failed", "message": str(exc)})
            self._finish_failed_end(exc, terminal_persisted)

    def _run_cloud_speaker_pass(
        self,
        *,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
        transcribe_fn: Optional[Callable[..., Any]] = None,
    ) -> Dict[str, Any]:
        """Upload loopback audio and relabel speakers. Never raises.

        Returns:
            ``{ok, skipped, applied, error}``. ``skipped`` is True when the
            backend, consent, or API key is missing.
        """
        if self.options.speaker_id_backend != "openai":
            return {
                "ok": False, "skipped": True, "applied": 0,
                "error": "Speaker identification is set to on-device.",
            }
        if not self.options.speaker_id_audio_consent:
            return {
                "ok": False, "skipped": True, "applied": 0,
                "error": "Audio-upload consent has not been given.",
            }
        if not self.meeting_id or self.store is None:
            return {
                "ok": False, "skipped": False, "applied": 0,
                "error": "Meeting is not ready for speaker identification.",
            }
        api_key = ""
        if transcribe_fn is None:
            try:
                from services.transcript_cleanup import find_api_key

                api_key = find_api_key("openai") or ""
            except Exception:
                logger.exception("Could not resolve the OpenAI API key")
                api_key = ""
            if not api_key:
                return {
                    "ok": False, "skipped": True, "applied": 0,
                    "error": "No OpenAI API key is configured.",
                }
        try:
            from meeting.diarize.cloud_pass import run_cloud_speaker_pass
        except Exception as exc:
            logger.exception("Cloud speaker pass unavailable")
            return {
                "ok": False, "skipped": False, "applied": 0, "error": str(exc),
            }
        spool_dir = self._spool_dir or ""
        try:
            result = run_cloud_speaker_pass(
                self.repository, self.meeting_id, self.store, spool_dir,
                api_key=api_key,
                transcribe_fn=transcribe_fn,
                progress_cb=progress_cb,
            )
        except Exception as exc:
            logger.exception("Cloud speaker pass raised")
            return {
                "ok": False, "skipped": False, "applied": 0, "error": str(exc),
            }
        return {
            "ok": bool(result.get("ok")),
            "skipped": False,
            "applied": int(result.get("applied") or 0),
            "created": int(result.get("created") or 0),
            "error": result.get("error"),
        }

    def _run_offline_final_pass(
        self,
        *,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> bool:
        """Re-decode session audio, replace the draft transcript, refresh UI.

        Returns:
            True when a non-empty offline transcript was committed.
        """
        asr = self._asr
        if asr is None or not self.meeting_id:
            return False
        if not self.options.end_redecode:
            return False
        transcribe = getattr(asr, "transcribe_offline_session", None)
        if not callable(transcribe):
            return False
        spool_dir = self._spool_dir or ""
        try:
            chunks = self.repository.get_audio_chunks(self.meeting_id)
        except Exception:
            logger.exception("Could not load audio chunks for offline ASR")
            chunks = []
        try:
            try:
                decoded = list(transcribe(spool_dir, chunks, progress_cb=progress_cb) or [])
            except TypeError:
                decoded = list(transcribe(spool_dir, chunks) or [])
        except Exception:
            logger.exception("Offline session transcription failed")
            return False
        if not decoded:
            return False
        try:
            existing = self.get_transcript()
        except Exception:
            existing = []
        new_words = sum(len(seg.text.split()) for seg in decoded)
        old_words = sum(
            len(str(row.get("text") or "").split()) for row in existing
        )
        if old_words and new_words < 0.8 * old_words:
            logger.warning(
                "Keeping live draft transcript: offline pass has %d words vs "
                "draft %d (AMI IN1009 guard: do not replace a sparser decode)",
                new_words, old_words,
            )
            return False
        try:
            self._assign_speakers_from_session(decoded, spool_dir, chunks)
        except Exception:
            logger.exception("Offline speaker assignment failed")
        replace = getattr(self.repository, "replace_final_transcript", None)
        if not callable(replace):
            return False
        try:
            rows, deleted, _id_map = replace(self.meeting_id, decoded)
        except Exception:
            logger.exception("Final transcript replace failed")
            return False
        mark_done = getattr(self.repository, "mark_chunks_done", None)
        if callable(mark_done):
            try:
                mark_done(self.meeting_id)
            except Exception:
                logger.exception("Could not mark chunks done after offline ASR")
        self._reload_store_from_repository()
        # The re-decode replaced every segment id; the repository remapped
        # evidence anchors onto the new transcript where an overlap match
        # exists. Proposed items that kept at least one live anchor stay on
        # the dashboard — their content is grounded in the actual meeting and
        # the final consolidation reconciles it. Only ghost-anchored items are
        # stripped. live_notes is deliberately kept whole: it provides
        # structured context for the final consolidation pass (and preserves
        # meeting notes when final report is off).
        from meeting.state.schema import CARD_KEYS

        self._strip_proposed_cards(
            cards=tuple(
                key for key in CARD_KEYS
                if key not in ("user_notes", "live_notes")
            ),
            keep_evidenced=True,
        )
        payload = {"items": rows, "removed_ids": deleted}
        self._emit("segments", payload)
        self._broadcast({"type": "segments", **payload})
        logger.info(
            "Offline ASR replaced transcript: %d segments (%d removed)",
            len(rows), len(deleted),
        )
        return True

    def _reload_store_from_repository(self) -> None:
        """Reload live state after the repository rewrote evidence ids."""
        if self.store is None or not self.meeting_id:
            return
        try:
            meeting = self.repository.get_meeting(self.meeting_id)
        except Exception:
            logger.exception("Could not reload meeting after transcript replace")
            return
        raw = (meeting or {}).get("state_json") or ""
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Corrupt state_json after transcript replace")
            return
        try:
            self.store.replace_document(MeetingState.from_dict(data))
        except Exception:
            logger.exception("Could not replace live meeting state document")

    def _strip_proposed_cards(
        self,
        cards: Optional[Iterable[str]] = None,
        keep_evidenced: bool = False,
    ) -> None:
        """Remove agent-only proposed cards so consolidation starts clean.

        Args:
            cards: Card keys to strip; defaults to every card except the
                human-only ``user_notes``.
            keep_evidenced: Skip proposed items that still carry at least one
                evidence anchor (used after the offline re-decode, where the
                repository remapped surviving anchors onto the new transcript
                and consolidation should reconcile grounded live items rather
                than rebuild from scratch).
        """
        if self.store is None:
            return
        from meeting.state.schema import CARD_KEYS

        keys = tuple(cards) if cards is not None else CARD_KEYS
        snapshot = self.store.snapshot()
        ops: List[Dict[str, Any]] = []
        cards_snapshot = snapshot.get("cards") or {}
        for key in keys:
            if key == "user_notes":
                continue
            for item in cards_snapshot.get(key) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("status") != "proposed" or item.get("pinned"):
                    continue
                if keep_evidenced and (item.get("evidence") or []):
                    continue
                ops.append({
                    "op": "remove_item",
                    "id": item.get("id"),
                    "base_revision": item.get("revision", 1),
                })
        if not ops:
            return
        try:
            self.store.apply("system", "finalization", ops)
        except Exception:
            logger.exception("Could not strip proposed cards before report")

    def _assign_speakers_from_session(
        self,
        segments: List[TranscriptSegment],
        spool_dir: str,
        chunks: List[Dict[str, Any]],
    ) -> None:
        """Assign Me/diarizer labels using session audio instead of chunks."""
        try:
            from meeting.asr.audio import prepare_for_whisper
            from meeting.asr.offline import load_channel_session
        except Exception:
            logger.exception(
                "Offline speaker helpers unavailable; skipping diarization"
            )
            return

        by_channel: Dict[str, List[TranscriptSegment]] = {}
        for seg in segments:
            if seg.channel == CHANNEL_MIC:
                seg.speaker_participant_id = self._me_participant_id
                seg.speaker_source = "channel"
            elif seg.channel == CHANNEL_LOOPBACK:
                by_channel.setdefault(seg.channel, []).append(seg)
        if not by_channel or self._diarizer is None:
            return
        for channel, channel_segments in by_channel.items():
            frames, rate, origin = load_channel_session(spool_dir, channel, chunks)
            if frames is None or frames.size == 0:
                continue
            for seg in channel_segments:
                start = max(0, int(round((seg.start_s - origin) * rate)))
                end = min(len(frames), int(round((seg.end_s - origin) * rate)))
                if end <= start:
                    continue
                try:
                    audio = prepare_for_whisper(frames[start:end], rate)
                    participant_id = self._diarizer.assign(seg, audio, 16000)
                except Exception:
                    logger.exception(
                        "Diarizer assignment failed for offline %s",
                        seg.segment_id,
                    )
                    participant_id = None
                if participant_id:
                    seg.speaker_participant_id = participant_id
                    seg.speaker_source = "diarizer"

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
        self._stop_asr("failed end")
        self.revoke_agent_writes()
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
            try:
                self._set_finalization(
                    "failed",
                    f"Meeting end failed before final insights could finish: {exc}",
                    emit=False,
                )
            except Exception:
                logger.exception(
                    "Failed to persist finalization after a failed end"
                )
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
        # Stop agent mutations before teardown so in-flight tools cannot land.
        self.revoke_agent_writes()
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
        self._stop_asr("cancel")
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
            self._set_finalization(
                "unavailable",
                "Meeting was canceled before final insights could run.",
                emit=False,
            )
        self._broadcast({"type": "meeting_ended", "status": "failed"})
        self._emit_status()
        self._emit("ended", {
            "meeting_id": self.meeting_id,
            "canceled": True,
            "status": "failed",
        })

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
                timeout=(
                    SHUTDOWN_DRAIN_TIMEOUT_S
                    + POLISH_TIMEOUT_S
                    + CONSOLIDATION_TIMEOUT_S
                    + 60.0
                )
            )
        # Best-effort: never leave a durable finalization=running across exit.
        self._interrupt_finalization_on_shutdown()
        self.revoke_agent_writes()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.stop()
            except Exception:
                logger.exception("Meeting web server stop failed")
        self._shutdown_agent_core()
        self._stop_heartbeat()

    def _interrupt_finalization_on_shutdown(self) -> None:
        """Persist a terminal interruption when shutdown cuts consolidation short."""
        if self.store is None:
            return
        try:
            fin = self.store.with_state(lambda s: s.finalization.to_dict())
        except Exception:
            logger.exception("Could not read finalization during shutdown")
            return
        status = str((fin or {}).get("status") or "")
        if status not in {"running", "pending"}:
            return
        cloud_enabled = bool(
            self.store.with_state(lambda s: s.cloud_enabled)
        )
        if not cloud_enabled:
            self._set_finalization(
                "disabled",
                "Cloud intelligence is off for this meeting.",
                emit=False,
            )
            return
        if status == "running":
            self._set_finalization(
                "failed",
                "Final cloud insights were interrupted by application shutdown.",
                emit=False,
            )
        else:
            self._set_finalization(
                "unavailable",
                "Final cloud insights did not run before shutdown.",
                emit=False,
            )

    def _abort_start(self) -> None:
        """Best-effort teardown after a failed ``start()``."""
        self._active = False
        self.revoke_agent_writes()
        self._stop_capture()
        self._flush_spools()  # releases the spool writer threads
        self._stop_asr("start abort")
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
        meeting_id = self.meeting_id
        if not meeting_id:
            return

        # A start that never captured anything is not a real meeting. Keeping
        # it would create an "Untitled · Ended" history row for every missing
        # dependency or unavailable device. Be conservative on inspection
        # failures: preserve the row whenever we cannot prove it is empty.
        has_artifacts = True
        try:
            has_artifacts = bool(
                self.repository.get_audio_chunks(meeting_id)
                or self.repository.get_segments(meeting_id)
            )
        except Exception:
            logger.exception("Could not inspect aborted meeting artifacts")
        if not has_artifacts:
            try:
                from meeting.persist.data_lifecycle import delete_meeting_data

                delete_meeting_data(
                    self.repository,
                    meeting_id,
                    self.options.spool_root or "meeting_audio",
                )
            except Exception:
                logger.exception("Failed to discard empty aborted meeting")
            else:
                logger.info("Discarded empty failed meeting start: %s", meeting_id)
                self.store = None
                return

        try:
            self.repository.update_meeting(
                meeting_id, status="failed", ended_at=now_iso()
            )
        except Exception:
            logger.exception("Failed to mark aborted meeting as failed")
        if self.store is not None:
            try:
                self.store.update_runtime_fields(status="failed")
            except Exception:
                logger.exception("Failed to mark aborted store status")
            try:
                self._set_finalization(
                    "unavailable",
                    "Meeting failed to start before final insights could run.",
                    emit=False,
                )
            except Exception:
                logger.exception(
                    "Failed to persist finalization after aborted start"
                )

    def _shutdown_agent_core(self) -> None:
        core = self._agent_core
        self._agent_core = None
        if core is not None:
            # Detached first: a dying core's reader thread must not push
            # activity ticks after intelligence was turned off.
            setter = getattr(core, "set_activity_callback", None)
            if callable(setter):
                try:
                    setter(None)
                except Exception:
                    logger.debug("Detaching agent activity callback failed",
                                 exc_info=True)
            try:
                core.shutdown()
            except Exception:
                logger.exception("Agent core shutdown failed")

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
            raise RuntimeError(
                f"Audio capture is unavailable: {exc}"
            ) from exc

        self._sources = []
        self._system_audio_disabled = (
            str(self.options.system_audio_policy or "auto").lower() == "disabled"
        )
        self._loopback_was_available = False
        self._explicit_capture_message = None
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

        loop_ok = False
        if self._system_audio_disabled:
            logger.info("System audio disabled for this meeting by user choice")
        else:
            loop_dev = None
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
                loop_ok = self._start_fallback_loopback()
            if not loop_ok:
                logger.warning(
                    "No loopback source available; meeting continues mic-only"
                )
                policy = str(self.options.system_audio_policy or "auto").lower()
                if policy == "required":
                    raise RuntimeError(
                        "LINUX_SYSTEM_AUDIO_REQUIRED: "
                        "System-audio capture is required but unavailable."
                    )

        active = {source.channel for source in self._sources}
        notes = []
        if CHANNEL_MIC not in active:
            notes.append("Microphone capture is unavailable.")
        if CHANNEL_LOOPBACK not in active:
            if self._system_audio_disabled:
                notes.append(
                    "System audio disabled for this meeting by your "
                    "microphone-only choice."
                )
            else:
                notes.append(
                    "System-audio capture is unavailable; "
                    "recording microphone only."
                    if CHANNEL_MIC in active else
                    "System-audio capture is unavailable."
                )
        if not active:
            self._update_capture_status("No audio devices could be opened.")
            raise RuntimeError("No audio devices could be opened.")
        note = " ".join(notes) or None
        if self._system_audio_disabled:
            self._explicit_capture_message = note
        self._loopback_was_available = CHANNEL_LOOPBACK in active
        self._update_capture_status(note or "")
        self._start_capture_watchdog()
        return note

    def _start_fallback_loopback(self, *, reuse_spool: bool = False) -> bool:
        """Open system audio through the best fallback for this OS.

        Reached when no WASAPI ``[Loopback]`` *input* device could be opened,
        which is always the case off Windows. ScreenCaptureKit is tried first
        because it is the only macOS path; SoundCard covers Windows fallback
        and Linux Pulse/PipeWire-Pulse monitors. Each backend's ``available()``
        rules itself out elsewhere, so the order is a preference rather than a
        platform switch.

        Returns:
            True when a loopback source is delivering audio; False degrades
            the meeting to mic-only rather than failing it.
        """
        from meeting.capture.sck_stream import ScreenCaptureKitLoopbackSource
        from meeting.capture.soundcard_stream import SoundcardLoopbackSource

        for backend in (ScreenCaptureKitLoopbackSource, SoundcardLoopbackSource):
            try:
                if backend.available() and self._start_source(
                    backend(), reuse_spool=reuse_spool,
                ):
                    return True
            except Exception:
                logger.exception("%s loopback fallback unavailable",
                                 backend.__name__)
        return False

    def _start_source(
        self,
        source: Any,
        *,
        reuse_spool: bool = False,
    ) -> bool:
        """Start one capture source and verify it is really delivering audio.

        Args:
            source: A ``CaptureSource`` that has not been started yet.
            reuse_spool: When True, keep the existing channel ``SpoolWriter``
                so a watchdog source swap does not discard pre-restart audio.

        Returns:
            True when the source started and reports itself active; False
            when it failed to open, in which case a newly created spool is
            torn down and the caller is free to try a fallback for the same
            channel. An existing reused spool is left intact on failure.
        """
        from meeting.capture.spool import SpoolWriter  # already validated

        channel = source.channel
        created_spool = False
        with self._capture_lock:
            spool = self._spools.get(channel) if reuse_spool else None
        if spool is None:
            spool = SpoolWriter(
                self.meeting_id, channel, self._spool_dir,
                self.clock, self.repository, on_chunk=self._on_chunk,
                initial_seq=self.repository.next_chunk_seq(
                    self.meeting_id, channel
                ),
            )
            created_spool = True
        started = False
        try:
            source.start(self._make_block_router(channel, spool))
            started = True
            active = bool(source.is_active())
        except Exception:
            logger.exception("Failed to start %s capture", channel)
            active = False
        if not active:
            if started:
                logger.error("%s capture reported inactive right after "
                             "starting; treating it as unavailable",
                             channel)
                try:
                    source.stop()
                except Exception:
                    logger.exception("Failed to stop inactive %s source",
                                     channel)
            if created_spool:
                try:
                    spool.flush()  # joins the writer thread; nothing to write
                except Exception:
                    logger.exception("Failed to release spool for %s",
                                     channel)
            return False
        with self._capture_lock:
            self._spools[channel] = spool
            self._sources = [
                existing for existing in self._sources
                if existing.channel != channel
            ]
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
                    if (channel == CHANNEL_LOOPBACK
                            and self._system_audio_disabled):
                        continue
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
        """Stop one channel's live source and open its replacement.

        The channel ``SpoolWriter`` is kept so audio captured before the
        restart remains durable and is stitched with later chunks.
        """
        if channel == CHANNEL_LOOPBACK and self._system_audio_disabled:
            return False
        self._stop_capture_source(channel)
        try:
            from meeting.capture.devices import find_loopback_device, find_mic_device
            from meeting.capture.sd_stream import SdCaptureSource

            # Reuse the watchdog probe when provided so recovery does not pay
            # for a second device enumeration inside the 12s budget.
            if desired is None:
                if channel == CHANNEL_MIC:
                    desired = find_mic_device(self.options.mic_device_id)
                else:
                    desired = find_loopback_device()
            if desired is not None:
                if self._start_source(
                    SdCaptureSource(
                        channel, desired["index"], desired["samplerate"],
                        desired["channels"],
                    ),
                    reuse_spool=True,
                ):
                    return True
            if channel == CHANNEL_LOOPBACK:
                return self._start_fallback_loopback(reuse_spool=True)
        except Exception:
            logger.exception("Could not restart %s capture", channel)
        return False

    def _stop_capture_source(self, channel: str) -> None:
        """Stop the live source for one channel without retiring its spool."""
        with self._capture_lock:
            sources = [s for s in self._sources if s.channel == channel]
            self._sources = [s for s in self._sources if s.channel != channel]
        for source in sources:
            try:
                source.stop()
            except Exception:
                logger.exception("Could not stop stale %s source", channel)

    def _retire_capture_channel(self, channel: str) -> None:
        """Stop a channel source and flush/remove its spool (end of life)."""
        self._stop_capture_source(channel)
        with self._capture_lock:
            spool = self._spools.pop(channel, None)
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
        loopback_available = CHANNEL_LOOPBACK in active
        if (
            loopback_available
            and not self._loopback_was_available
            and not self._system_audio_disabled
        ):
            message = message or "System audio restored."
            self._explicit_capture_message = None
        self._loopback_was_available = loopback_available
        if not message:
            if self._explicit_capture_message and not loopback_available:
                message = self._explicit_capture_message
            else:
                missing = []
                if CHANNEL_MIC not in active:
                    missing.append("Microphone unavailable")
                if CHANNEL_LOOPBACK not in active:
                    if self._system_audio_disabled:
                        missing.append(
                            "System audio disabled for this meeting by your "
                            "microphone-only choice"
                        )
                    else:
                        missing.append("System audio unavailable")
                message = "; ".join(missing)
        capture = {
            "mic_available": CHANNEL_MIC in active,
            "loopback_available": loopback_available,
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

    def _start_asr(self) -> None:
        try:
            from meeting.asr.engine import MeetingAsrEngine
            language = (self.options.asr_language or "auto").strip().lower()
            asr = MeetingAsrEngine(
                self.options.asr_model,
                self.meeting_id,
                self.repository,
                language=None if language == "auto" else language,
                # Real-meeting evaluation found rolling rewrites improved some
                # meetings but degraded others by up to 4.9 absolute WER.
                # A durable record must prefer the stable draft until a
                # no-reference quality gate is proven trustworthy.
                enable_revisions=False,
            )
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
        self._maybe_revise_transcript(chunk)

    def _maybe_revise_transcript(self, chunk: SpooledChunk) -> None:
        """Schedule and run a bounded rolling re-decode for recent audio."""
        asr = self._asr
        if asr is None:
            return
        frontier = float(chunk.start_s) + float(chunk.duration_s)
        schedule = getattr(asr, "schedule_revise", None)
        run_pending = getattr(asr, "run_pending_revises", None)
        if not callable(schedule) or not callable(run_pending):
            return
        try:
            schedule(chunk.channel, frontier)
            # A rolling re-decode must never jump ahead of even one queued
            # draft chunk; the ASR engine also enforces this internally.
            if getattr(asr, "backlog_depth", lambda: 0)() > 0:
                return
            for outcome in run_pending():
                self._publish_revise_result(outcome)
        except Exception:
            logger.exception("Rolling transcript revise failed")

    def _publish_revise_result(self, outcome: Dict[str, Any]) -> None:
        """Assign speakers for new revise rows and broadcast upserts/removals."""
        items = list(outcome.get("items") or [])
        removed_ids = list(outcome.get("removed_ids") or [])
        if not items and not removed_ids:
            return

        # Re-run speaker assignment for rows that still lack a participant.
        needing = []
        for row in items:
            if row.get("speaker_participant_id") or row.get("speaker_pinned"):
                continue
            needing.append(TranscriptSegment(
                segment_id=row["id"],
                meeting_id=self.meeting_id,
                chunk_id=row.get("chunk_id"),
                channel=row.get("channel") or CHANNEL_MIC,
                start_s=float(row.get("start_s") or 0.0),
                end_s=float(row.get("end_s") or 0.0),
                text=row.get("text") or "",
            ))
        if needing:
            try:
                self._assign_speakers(needing)
            except Exception:
                logger.exception("Speaker assignment failed for revise batch")
            for seg in needing:
                if not seg.speaker_participant_id:
                    continue
                try:
                    self.repository.update_segment_speaker(
                        self.meeting_id,
                        seg.segment_id,
                        seg.speaker_participant_id,
                        seg.speaker_source,
                        False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist revise speaker for %s", seg.segment_id
                    )
                for row in items:
                    if row.get("id") == seg.segment_id:
                        row["speaker_participant_id"] = seg.speaker_participant_id
                        row["speaker_source"] = seg.speaker_source
                        break

        payload = {"items": items, "removed_ids": removed_ids}
        self._emit("segments", payload)
        self._broadcast({"type": "segments", **payload})
        scheduler = self._scheduler
        if scheduler is not None and items:
            try:
                # Revised text should refresh agent context even when ids reuse.
                scheduler.notify_segments(len(items))
            except Exception:
                logger.exception("Scheduler notify failed after revise")

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

    def _start_server(self) -> str:
        try:
            from meeting.web.server import MeetingWebServer
            server = MeetingWebServer(
                self, self.repository,
                bind=self.options.server_bind, port=self.options.server_port,
            )
            url = server.start()
        except Exception as exc:
            logger.exception("Meeting web server failed to start")
            raise RuntimeError(
                f"Meeting dashboard could not start: {exc}"
            ) from exc
        self._server = server
        self._emit("server_started", {
            "url": url,
            "host_url": server.host_url,
            "guest_url": server.guest_url,
        })
        return url

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
                        endpoint=self.options.llm_endpoint,
                    ),
                    self,
                )
                self._agent_core = created_core
                # Duck-typed: cores without session events (the direct
                # OpenRouter core) simply publish no activity.
                setter = getattr(created_core, "set_activity_callback", None)
                if callable(setter):
                    setter(self._on_agent_activity)
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
            self.allow_agent_writes()
            self.store.update_runtime_fields(intelligence_online=True)
            # Fresh/recovered intelligence is ready for a later consolidation.
            self._set_finalization("pending", emit=False)
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
            self._set_finalization(
                "unavailable",
                f"Meeting intelligence failed to start: {exc}",
                emit=False,
            )
            self._emit("intelligence", {"online": False, "error": str(exc)})
            self._emit_status()

    def _on_agent_activity(self, activity: Any) -> None:
        """Publish one ephemeral agent activity tick to host dashboards.

        Called from the agent core's reader thread (and its tool worker), so
        the ring buffer is guarded and the broadcast goes through the
        thread-safe web transport. Ticks are dropped when the same kind and
        tool were already published less than
        :data:`AGENT_ACTIVITY_MIN_INTERVAL_S` ago.

        Args:
            activity: An ``AgentActivity``-shaped record from the agent core.
        """
        record = {
            "kind": str(getattr(activity, "kind", "") or "update"),
            "label": str(getattr(activity, "label", "") or ""),
            "tool": str(getattr(activity, "tool", "") or ""),
            "pass_kind": str(getattr(activity, "pass_kind", "") or ""),
            "ts": str(getattr(activity, "ts", "")
                      or datetime.now(timezone.utc).isoformat()),
        }
        key = (record["kind"], record["tool"])
        now = time.monotonic()
        with self._activity_lock:
            last = self._agent_activity_last.get(key)
            if last is not None and (now - last) < AGENT_ACTIVITY_MIN_INTERVAL_S:
                return
            self._agent_activity_last[key] = now
            self._agent_activity.append(record)
        self._broadcast({"type": "agent_activity", **record}, host_only=True)

    def recent_agent_activity(self) -> List[Dict[str, str]]:
        """Recent agent activity ticks, oldest first, for a host snapshot.

        Returns:
            Up to :data:`AGENT_ACTIVITY_HISTORY` activity records without a
            message ``type``, safe to embed in the WebSocket ``hello``.
        """
        with self._activity_lock:
            return [dict(record) for record in self._agent_activity]

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
        self._set_finalization("unavailable", reason, emit=False)
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
        if online:
            # Recovered checkpoints restore a pending consolidation path.
            meeting_status = self.store.with_state(lambda s: s.status) if self.store else "active"
            if meeting_status not in ("ending", "ended", "failed", "needs_recovery"):
                self._set_finalization("pending", emit=False)
        else:
            payload["error"] = ("Checkpoints failed repeatedly; continuing "
                                "transcript-only.")
            self._set_finalization(
                "unavailable",
                payload["error"],
                emit=False,
            )
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
            self._set_finalization(
                "disabled",
                "Cloud intelligence is off for this meeting.",
                emit=False,
            )
        self._emit("intelligence", {"online": False})
        self._emit_status()

    def set_cloud_enabled(self, enabled: bool) -> None:
        """Toggle cloud processing mid-meeting (host action).

        Args:
            enabled: True starts (or restarts) the intelligence layer,
                resuming from the current point in the transcript; False stops
                checkpoints, cancels any in-flight request, and releases the
                agent core so no process keeps holding the API key.

        Raises:
            RuntimeError: When end has already claimed or finished the session,
                or when the store rejects the cloud op.
        """
        if self.store is None:
            return
        with self._lifecycle_lock:
            end_claimed = self._end_thread is not None or not self._active
        if end_claimed:
            raise RuntimeError(
                "Cloud intelligence can only be changed while the meeting is "
                "active or paused."
            )
        meeting_status = self.store.with_state(lambda s: s.status)
        if meeting_status not in {"active", "paused"}:
            raise RuntimeError(
                "Cloud intelligence can only be changed while the meeting is "
                "active or paused."
            )
        results = self.store.apply("host", self._me_participant_id, [{
            "op": "set_cloud_enabled", "enabled": bool(enabled),
        }])
        if not results or not results[0].ok:
            raise RuntimeError(
                results[0].reason if results else "cloud state update failed"
            )
        if enabled:
            self.allow_agent_writes()
            self._set_finalization("pending", emit=False)
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

    # Client actions (web server entry points)

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

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply state-patch ops on behalf of the agent."""
        if self.store is None:
            return [
                OpResult(ok=False,
                         op=op if isinstance(op, dict) else {"op": op},
                         reason="inactive")
                for op in ops
            ]
        if not self.agent_writes_allowed():
            return [
                OpResult(
                    ok=False,
                    op=op if isinstance(op, dict) else {"op": op},
                    reason="agent_writes_revoked",
                )
                for op in ops
            ]
        return self.store.apply("agent", "agent", list(ops))

    def segment_exists(self, segment_id: str) -> bool:
        """Exact-match stored-segment lookup for agent evidence repair."""
        repository = getattr(self, "repository", None)
        meeting_id = getattr(self, "meeting_id", None)
        if not meeting_id or not hasattr(repository, "segment_exists"):
            return False
        try:
            return bool(repository.segment_exists(meeting_id, segment_id))
        except Exception:
            logger.exception("Segment existence probe failed")
            return False

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

    def search_past_meetings(
        self,
        query: str = "",
        meeting_id: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Bounded, consent-gated recall of earlier meeting transcripts."""
        from meeting.recall import search_past_meetings as recall

        return recall(
            getattr(self, "repository", None),
            query=query,
            current_meeting_id=getattr(self, "meeting_id", None) or "",
            meeting_id=meeting_id,
            limit=limit,
        )

    def search_context_files(
        self,
        query: str = "",
        relative_path: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Bounded, consent-gated search of the configured knowledge folder."""
        from meeting.context_folder import search_context_files as search

        return search(
            query=query,
            relative_path=relative_path,
            limit=limit,
        )

    def _apply_single_agent_op(self, op: Dict[str, Any]) -> OpResult:
        if self.store is None:
            return OpResult(ok=False, op=op, reason="inactive")
        if not self.agent_writes_allowed():
            return OpResult(ok=False, op=op, reason="agent_writes_revoked")
        return self.store.apply("agent", "agent", [op])[0]

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
