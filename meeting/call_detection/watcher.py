"""Background watcher coordinating call detection and call-end auto-stop.

Uses an injected :class:`AudioActivityAdapter` (lazy OS adapter in production,
fakes in unit tests) and exposes a synchronous :meth:`step` method so the full
coordination logic can be unit-tested deterministically without threads or sleep.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Mapping, Optional

from meeting.call_detection.adapters import AudioActivityAdapter, create_platform_adapter
from meeting.call_detection.catalog import (
    canonical_app_id,
    candidate_from_microphone,
    is_browser_id,
    only_native_call_app,
    pick_primary_process,
)
from meeting.call_detection.detector import CallDetector, CallEndTracker
from meeting.call_detection.models import (
    ActiveAudioProcess,
    CallCandidate,
    CallEndGraceCancelled,
    CallEndGraceStarted,
    CallEndStopDue,
    CallPolicy,
    DetectionAutoRecord,
    DetectionOffer,
    WatchedAppStatus,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_S: float = 1.0


class CallDetectionWatcher:
    """Polls audio activity to detect incoming calls and auto-stop ended calls."""

    def __init__(
        self,
        *,
        adapter: Optional[AudioActivityAdapter] = None,
        get_default_policy: Callable[[], str] = lambda: CallPolicy.ASK,
        get_app_policies: Callable[[], Mapping[str, object]] = lambda: {},
        get_auto_stop_enabled: Callable[[], bool] = lambda: False,
        is_meeting_busy: Callable[[], bool] = lambda: False,
        on_offer: Optional[Callable[[CallCandidate, int], None]] = None,
        on_offer_withdrawn: Optional[Callable[[int], None]] = None,
        on_auto_record: Optional[Callable[[CallCandidate], None]] = None,
        on_provider_seen: Optional[Callable[[str, str], None]] = None,
        on_call_end_grace_started: Optional[Callable[[str, int], None]] = None,
        on_call_end_grace_cancelled: Optional[Callable[[str], None]] = None,
        on_call_end_stop_due: Optional[Callable[[str], None]] = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._get_default_policy = get_default_policy
        self._get_app_policies = get_app_policies
        self._get_auto_stop_enabled = get_auto_stop_enabled
        self._is_meeting_busy = is_meeting_busy
        self._on_offer = on_offer
        self._on_offer_withdrawn = on_offer_withdrawn
        self._on_auto_record = on_auto_record
        self._on_provider_seen = on_provider_seen
        self._on_call_end_grace_started = on_call_end_grace_started
        self._on_call_end_grace_cancelled = on_call_end_grace_cancelled
        self._on_call_end_stop_due = on_call_end_stop_due
        self._poll_interval_s = max(0.1, float(poll_interval_s))
        self._clock = clock

        self._lock = threading.RLock()
        self._detector = CallDetector()
        self._end_tracker: Optional[CallEndTracker] = None
        self._seen_providers: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def detector(self) -> CallDetector:
        return self._detector

    @property
    def end_tracker(self) -> Optional[CallEndTracker]:
        with self._lock:
            return self._end_tracker

    def _ensure_adapter(self) -> AudioActivityAdapter:
        with self._lock:
            if self._adapter is None:
                self._adapter = create_platform_adapter()
            return self._adapter

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="meeting-call-detection",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            adapter = self._adapter
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logger.debug("Call detection adapter close failed", exc_info=True)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:
                logger.debug("Call detection poll iteration failed", exc_info=True)
            self._stop_event.wait(self._poll_interval_s)

    def _detection_enabled(
        self, default_policy: str, app_policies: Mapping[str, object]
    ) -> bool:
        if CallPolicy.normalize(default_policy) != CallPolicy.DONT_OFFER:
            return True
        for entry in app_policies.values():
            raw = entry.get("policy") if isinstance(entry, Mapping) else entry
            if str(raw or "").strip().lower() in (
                CallPolicy.ASK,
                CallPolicy.ALWAYS_RECORD,
            ):
                return True
        return False

    def notify_meeting_started(
        self,
        *,
        app_id: Optional[str] = None,
        app_label: Optional[str] = None,
        started_automatically: bool = False,
    ) -> None:
        """Register the active meeting's watched call app for auto-stop tracking."""
        with self._lock:
            withdrawn = self._detector.withdraw_offer()
            self._drain_withdrawn_locked(extra=withdrawn)

            resolved_id = app_id
            resolved_label = app_label
            if resolved_id is None and self._get_auto_stop_enabled():
                # Manual start: link to a single active native call app if present.
                try:
                    procs = self._ensure_adapter().active_microphone_processes()
                    match = only_native_call_app(procs)
                    if match is not None:
                        resolved_id, resolved_label = match
                except Exception:
                    logger.debug(
                        "Could not inspect mic for manual meeting auto-stop link",
                        exc_info=True,
                    )

            if resolved_id:
                self._end_tracker = CallEndTracker(
                    app_id=resolved_id,
                    app_label=resolved_label or resolved_id,
                    started_automatically=started_automatically,
                )
            else:
                self._end_tracker = None

    def notify_meeting_stopped(self) -> None:
        """Clear call-end tracking and shrink detector snooze after recording ends."""
        now = self._clock()
        cancelled = False
        with self._lock:
            if self._end_tracker is not None and self._end_tracker.armed:
                cancelled = True
            self._end_tracker = None
            self._detector.recording_stopped(now)
            withdrawn = self._detector.take_withdrawn_offers()
        if cancelled and self._on_call_end_grace_cancelled is not None:
            self._on_call_end_grace_cancelled("stopped")
        self._emit_withdrawn(withdrawn)

    def mark_offer_answered(self, offer_id: int) -> None:
        with self._lock:
            self._detector.mark_offer_answered(offer_id)

    def mark_offer_timed_out(self, offer_id: int) -> None:
        with self._lock:
            self._detector.offer_timed_out(offer_id)

    def keep_recording(self) -> None:
        event = None
        with self._lock:
            if self._end_tracker is not None:
                event = self._end_tracker.keep_recording()
        if event is not None and self._on_call_end_grace_cancelled is not None:
            self._on_call_end_grace_cancelled(event.reason)

    def step(self, *, now: Optional[float] = None) -> None:
        """Run one detection or call-end observation pass."""
        ts = self._clock() if now is None else now
        if self._is_meeting_busy():
            self._step_during_meeting(ts)
            return
        self._step_idle(ts)

    def _step_idle(self, now: float) -> None:
        default_policy = self._get_default_policy()
        app_policies = self._get_app_policies()
        if not self._detection_enabled(default_policy, app_policies):
            return

        adapter = self._ensure_adapter()
        try:
            processes = adapter.active_microphone_processes()
        except Exception:
            logger.debug("Could not read active microphone processes", exc_info=True)
            return

        primary = pick_primary_process(processes)
        candidate: Optional[CallCandidate] = None
        if primary is not None:
            canon = canonical_app_id(primary.bundle_id, primary.name)
            browser_ctx = None
            if is_browser_id(canon):
                try:
                    browser_ctx = adapter.browser_context(canon)
                except Exception:
                    logger.debug("Browser context inspection failed", exc_info=True)
            candidate = candidate_from_microphone(primary, browser_ctx)

        if candidate is not None:
            provider = candidate.provider()
            if provider is not None and provider.key not in self._seen_providers:
                self._seen_providers.add(provider.key)
                if self._on_provider_seen is not None:
                    self._on_provider_seen(provider.key, provider.label)

        with self._lock:
            event = self._detector.observe(
                candidate, app_policies, default_policy, now
            )
            withdrawn = self._detector.take_withdrawn_offers()

        self._emit_withdrawn(withdrawn)

        if isinstance(event, DetectionOffer) and self._on_offer is not None:
            self._on_offer(event.candidate, event.offer_id)
        elif isinstance(event, DetectionAutoRecord) and self._on_auto_record is not None:
            self._on_auto_record(event.candidate)

    def _step_during_meeting(self, now: float) -> None:
        with self._lock:
            tracker = self._end_tracker
        if tracker is None:
            return

        auto_stop = bool(self._get_auto_stop_enabled())
        adapter = self._ensure_adapter()

        status = self._read_watched_status(adapter, tracker.app_id, auto_stop)
        with self._lock:
            if status.present:
                self._detector.keep_snooze_alive(now)
            event = tracker.observe(status, auto_stop_enabled=auto_stop, now=now)

        if isinstance(event, CallEndGraceStarted):
            if self._on_call_end_grace_started is not None:
                self._on_call_end_grace_started(event.app_label, int(round(event.grace_s)))
        elif isinstance(event, CallEndGraceCancelled):
            if self._on_call_end_grace_cancelled is not None:
                self._on_call_end_grace_cancelled(event.reason)
        elif isinstance(event, CallEndStopDue):
            if self._on_call_end_stop_due is not None:
                self._on_call_end_stop_due(event.app_label)

    @staticmethod
    def _read_watched_status(
        adapter: AudioActivityAdapter,
        app_id: str,
        auto_stop_enabled: bool,
    ) -> WatchedAppStatus:
        try:
            procs = adapter.active_microphone_processes()
        except Exception:
            logger.debug("Could not read mic during active meeting", exc_info=True)
            return WatchedAppStatus(
                present=True, on_microphone=False, playing_audio=False, read_failed=True
            )

        on_mic = any(
            canonical_app_id(proc.bundle_id, proc.name) == app_id for proc in procs
        )
        if on_mic:
            return WatchedAppStatus(present=True, on_microphone=True, playing_audio=False)

        playing = False
        if auto_stop_enabled:
            try:
                playing = bool(adapter.is_app_playing_audio(app_id))
            except Exception:
                logger.debug("Could not read audio playback during meeting", exc_info=True)
                # Treat playback inspection failure as present so an OS error
                # never triggers a false auto-stop.
                return WatchedAppStatus(
                    present=True, on_microphone=False, playing_audio=False, read_failed=True
                )

        return WatchedAppStatus(
            present=playing,
            on_microphone=False,
            playing_audio=playing,
        )

    def _drain_withdrawn_locked(self, extra: Optional[int] = None) -> None:
        withdrawn = self._detector.take_withdrawn_offers()
        if extra is not None and extra not in withdrawn:
            withdrawn.append(extra)
        self._emit_withdrawn(withdrawn)

    def _emit_withdrawn(self, offer_ids: list[int]) -> None:
        if not offer_ids or self._on_offer_withdrawn is None:
            return
        for oid in offer_ids:
            self._on_offer_withdrawn(oid)
