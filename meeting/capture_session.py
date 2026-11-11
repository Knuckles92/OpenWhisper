"""MeetingCapture: one meeting's live audio capture.

Owns the microphone and system-audio sources, their per-channel spools and
signal monitors, microphone selection and failover, and the watchdog that
restarts failed sources and follows default-device changes. It reports
capture health into the meeting's state store.

The engine supplies everything else through the constructor: whether the
meeting is recording, the live ASR handle for voice previews, the state
store, where finished chunks go, and how to announce status and errors.

Like the engine, this module does no eager ``meeting.capture`` imports: the
capture layer is imported inside the methods that need it.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from meeting.interfaces import (
    CHANNEL_LOOPBACK,
    CHANNEL_MIC,
    CaptureBlock,
    SpooledChunk,
)

logger = logging.getLogger(__name__)

# Poll frequently enough that failure detection + SoundCard first-block wait
# (``_START_TIMEOUT_S`` = 4s) still fit the ≤12s restoration bound.
CAPTURE_WATCHDOG_INTERVAL_S = 1.0
CAPTURE_RETRY_INTERVAL_S = 3.0
#: Worst-case budget from a failed source until a successful restart attempt
#: must begin (poll jitter + retry spacing + start wait must stay under 12s).
CAPTURE_RECOVERY_BUDGET_S = 12.0
#: Microphones one open tries before waiting for the next watchdog pass.
MIC_OPEN_ATTEMPTS = 4


class MeetingCapture:
    """Capture sources, spools and the capture watchdog for one meeting.

    Attributes:
        sources: Live capture sources, at most one per channel.
        spools: Per-channel ``SpoolWriter``; kept across source restarts.
        stall_channels: Channels whose open stream stopped delivering blocks.
        mic_excluded: Keys of microphones that failed during this meeting.
        mic_device: The microphone currently open, as probed.
        integrity_error: First audio-storage failure, or None.
    """

    def __init__(
        self,
        options: Any,
        clock: Any,
        repository: Any,
        *,
        recording: Callable[[], bool],
        asr: Callable[[], Optional[Any]],
        store: Callable[[], Optional[Any]],
        on_chunk: Callable[[SpooledChunk], None],
        emit_status: Callable[..., None],
        emit: Callable[[str, Dict[str, Any]], None],
    ) -> None:
        """
        Args:
            options: The meeting's ``MeetingEngineOptions``.
            clock: The shared meeting clock.
            repository: Meeting persistence, for spools and chunk sequence.
            recording: True while the meeting is live (blocks are kept).
            asr: Current ASR handle (or None), for live voice previews.
            store: Current state store (or None), for capture status.
            on_chunk: Receives every finished chunk.
            emit_status: ``emit_status(note=...)`` announces a status change.
            emit: ``emit(kind, payload)`` reports an engine event.
        """
        self._options = options
        self._clock = clock
        self._repository = repository
        self._recording = recording
        self._asr = asr
        self._store = store
        self._on_chunk = on_chunk
        self._emit_status = emit_status
        self._emit = emit
        self._meeting_id: Optional[str] = None
        self._spool_dir: Optional[str] = None

        self.sources: List[Any] = []
        self.spools: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._watchdog_stop: Optional[threading.Event] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_attempt: Dict[str, float] = {}
        self._monitors: Dict[str, Any] = {}
        self.stall_channels: set[str] = set()
        # Keys of microphones that failed during this meeting. PortAudio's
        # device list is a snapshot, so an unplugged microphone keeps being
        # found until it is excluded here.
        self.mic_excluded: List[Dict[str, str]] = []
        self.mic_device: Optional[Dict[str, Any]] = None
        self._system_audio_disabled = False
        self._loopback_was_available = False
        self._explicit_message: Optional[str] = None
        self.integrity_error: Optional[str] = None

    def start(self, meeting_id: str, spool_dir: str) -> Optional[str]:
        """Open mic and loopback streams; returns a user-facing degradation
        note when one or both channels are unavailable.

        Every source is verified with ``is_active()`` after ``start()``: a
        source that opens no device must not be mistaken for a silent room,
        and a discovered-but-unusable WASAPI loopback device must still fall
        through to the ``soundcard`` fallback.
        """
        try:
            from meeting.capture.devices import find_loopback_device
            from meeting.capture.sd_stream import SdCaptureSource
            from meeting.capture.spool import SpoolWriter  # noqa: F401
        except Exception as exc:
            logger.exception("Capture layer unavailable")
            raise RuntimeError(
                f"Audio capture is unavailable: {exc}"
            ) from exc

        self._meeting_id = meeting_id
        self._spool_dir = spool_dir
        self.sources = []
        self._monitors = {}
        self.stall_channels = set()
        self._system_audio_disabled = (
            str(self._options.system_audio_policy or "auto").lower() == "disabled"
        )
        self._loopback_was_available = False
        self._explicit_message = None
        self.integrity_error = None
        self.mic_excluded = []
        self.mic_device = None
        if not self._open_mic():
            logger.warning("No microphone could be opened; mic channel disabled")

        if self._system_audio_disabled:
            logger.info("System audio disabled for this meeting by user choice")
        else:
            self._open_system_audio(find_loopback_device, SdCaptureSource)

        active = {source.channel for source in self.sources}
        notes = self._notes(active)
        if not active:
            self.update_status("No audio devices could be opened.")
            raise RuntimeError("No audio devices could be opened.")
        note = " ".join(notes) or None
        if self._system_audio_disabled:
            self._explicit_message = note
        self._loopback_was_available = CHANNEL_LOOPBACK in active
        self.update_status(note or "")
        self._start_watchdog()
        return note

    def _open_system_audio(self, find_loopback_device: Callable[[], Any], source_cls: Any) -> None:
        """Open loopback capture, falling back per OS; enforce a required policy."""
        loop_ok = False
        loop_dev = None
        try:
            loop_dev = find_loopback_device()
        except Exception:
            logger.exception("Loopback probe failed")
        if loop_dev is not None:
            try:
                loop_ok = self.start_source(source_cls(
                    CHANNEL_LOOPBACK, loop_dev["index"],
                    loop_dev["samplerate"], loop_dev["channels"],
                ))
            except Exception:
                logger.exception("Failed to open WASAPI loopback stream")
        if not loop_ok:
            loop_ok = self.start_fallback_loopback()
        if loop_ok:
            return
        logger.warning(
            "No loopback source available; meeting continues mic-only"
        )
        policy = str(self._options.system_audio_policy or "auto").lower()
        if policy == "required":
            raise RuntimeError(
                "LINUX_SYSTEM_AUDIO_REQUIRED: "
                "System-audio capture is required but unavailable."
            )

    def _notes(self, active: set) -> List[str]:
        """User-facing notes for the channels that did not open."""
        notes = []
        if CHANNEL_MIC not in active:
            notes.append("Microphone capture is unavailable.")
        if CHANNEL_LOOPBACK in active:
            return notes
        if self._system_audio_disabled:
            notes.append(
                "System audio disabled for this meeting by your "
                "microphone-only choice."
            )
        elif CHANNEL_MIC in active:
            notes.append(
                "System-audio capture is unavailable; "
                "recording microphone only."
            )
        else:
            notes.append("System-audio capture is unavailable.")
        return notes

    def start_fallback_loopback(self, *, reuse_spool: bool = False) -> bool:
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
                if backend.available() and self.start_source(
                    backend(), reuse_spool=reuse_spool,
                ):
                    return True
            except Exception:
                logger.exception("%s loopback fallback unavailable",
                                 backend.__name__)
        return False

    def start_source(
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
        from meeting.capture.health import CaptureSignalMonitor

        channel = source.channel
        created_spool = False
        with self._lock:
            spool = self.spools.get(channel) if reuse_spool else None
            monitor = self._monitors.get(channel)
            if monitor is None:
                monitor = CaptureSignalMonitor()
                self._monitors[channel] = monitor
            else:
                monitor.reset_stream()
        if spool is None:
            spool = SpoolWriter(
                self._meeting_id, channel, self._spool_dir,
                self._clock, self._repository, on_chunk=self._on_chunk,
                initial_seq=self._repository.next_chunk_seq(
                    self._meeting_id, channel
                ),
            )
            created_spool = True
        started = False
        try:
            source.start(self.make_block_router(channel, spool, monitor))
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
        with self._lock:
            self.spools[channel] = spool
            self.sources = [
                existing for existing in self.sources
                if existing.channel != channel
            ]
            self.sources.append(source)
        return True

    def make_block_router(self, channel: str, spool: Any,
                           monitor: Optional[Any] = None) -> Callable[[CaptureBlock], None]:
        """Audio-thread callback: drops blocks while paused, never raises."""
        error_logged = [False]
        health_error_logged = [False]

        def route(block: CaptureBlock) -> None:
            if monitor is not None:
                try:
                    monitor.observe(
                        block,
                        signal_enabled=self._recording() and not self._clock.is_paused,
                    )
                except Exception:
                    if not health_error_logged[0]:
                        health_error_logged[0] = True
                        logger.exception("Capture health observation failed on %s", channel)
            if not self._recording() or self._clock.is_paused:
                return
            try:
                spool.feed(block)
                feed_preview = getattr(self._asr(), "feed_preview", None)
                if callable(feed_preview):
                    feed_preview(block, self._clock.meeting_time(block.t_mono))
            except Exception:
                if not error_logged[0]:
                    error_logged[0] = True
                    logger.exception(
                        "Spool feed failed on channel %s (suppressing repeats)",
                        channel,
                    )
        return route

    def stop(self) -> None:
        self._stop_watchdog()
        with self._lock:
            sources, self.sources = self.sources, []
        for source in sources:
            try:
                source.stop()
            except Exception:
                logger.exception("Capture source stop failed")

    def flush(self) -> None:
        stop_preview = getattr(self._asr(), "stop_preview", None)
        if callable(stop_preview):
            stop_preview()
        with self._lock:
            spools = list(self.spools.values())
        for spool in spools:
            try:
                chunk = spool.flush()
            except Exception:
                logger.exception("Spool flush failed")
                self._record_integrity_error(
                    "Meeting audio storage could not finish. Partial audio was kept."
                )
                continue
            if chunk is not None:
                self._on_chunk(chunk)
        self.check_spool_health()

    def check_spool_health(self) -> None:
        """Surface writer failures away from the real-time audio callback."""
        with self._lock:
            spools = list(self.spools.values())
        for spool in spools:
            error = getattr(spool, "health_error", None)
            if error:
                self._record_integrity_error(str(error))

    def _record_integrity_error(self, message: str) -> None:
        with self._lock:
            if self.integrity_error:
                return
            self.integrity_error = message
        logger.error("Meeting audio incomplete: %s", message)
        self.update_status()
        self._emit("error", {
            "code": "capture_incomplete",
            "message": message + " Check available audio in Meeting History and "
                       "free storage or reduce system load before recording again.",
        })

    def _start_watchdog(self) -> None:
        """Start device-loss/default-device monitoring for this meeting."""
        if (self._watchdog_thread is not None
                and self._watchdog_thread.is_alive()):
            return
        stop = threading.Event()
        self._watchdog_stop = stop
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="meeting-capture-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        stop = self._watchdog_stop
        if stop is not None:
            stop.set()
        thread = self._watchdog_thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=3.0)
        self._watchdog_thread = None
        self._watchdog_stop = None

    def _watchdog_loop(self) -> None:
        """Restart failed sources and follow default-device changes."""
        stop = self._watchdog_stop
        while (stop is not None
               and not stop.wait(CAPTURE_WATCHDOG_INTERVAL_S)):
            if not self._recording():
                return
            for channel in (CHANNEL_MIC, CHANNEL_LOOPBACK):
                try:
                    self._watch_channel(channel)
                except Exception:
                    logger.exception("Capture watchdog failed for %s", channel)
            self.check_spool_health()
            self.update_status()

    def _watch_channel(self, channel: str) -> None:
        """One watchdog pass over ``channel``: restart it when failed or stale."""
        if (channel == CHANNEL_LOOPBACK
                and self._system_audio_disabled):
            return
        desired = self._probe_device(channel)
        source = self.source(channel)
        active = self._channel_healthy(channel, source)
        if active and not self._device_changed(channel, source, desired):
            return
        now = time.monotonic()
        if (now - self._last_attempt.get(channel, 0.0)
                < CAPTURE_RETRY_INTERVAL_S):
            return
        self._last_attempt[channel] = now
        self.restart_channel(channel, desired)

    def _channel_healthy(self, channel: str, source: Any) -> bool:
        active = source is not None and bool(source.is_active())
        monitor = self._monitors.get(channel)
        health = monitor.snapshot() if monitor is not None else None
        if active and health is not None and health["receiving"]:
            self.stall_channels.discard(channel)
        if (active and health is not None and health["stalled"]
                and getattr(source, "requires_audio_blocks", False)):
            # PortAudio can report an open/active stream without
            # ever invoking its callback. Other capture backends
            # already include block delivery in is_active().
            self.stall_channels.add(channel)
            logger.warning("%s capture stream stalled despite an open device", channel)
            return False
        return active

    def _device_changed(self, channel: str, source: Any,
                                desired: Optional[Dict[str, Any]]) -> bool:
        """True when an active source no longer matches the device to follow."""
        source_id = getattr(source, "device_id", None)
        desired_id = desired.get("index") if desired else None
        if channel == CHANNEL_MIC and desired is not None:
            # Only "System default" follows the default; a
            # ranked microphone stays until it fails.
            return (
                self._options.mic_device_id is None
                and not self._options.mic_priority
                and isinstance(source_id, int)
                and source_id != desired_id
            )
        if (channel == CHANNEL_LOOPBACK
                and isinstance(source_id, int)
                and desired is not None):
            return source_id != desired_id
        if channel == CHANNEL_LOOPBACK:
            current = getattr(
                source, "is_default_device_current", None
            )
            if callable(current):
                return not bool(current())
        return False

    def _probe_device(self, channel: str) -> Optional[Dict[str, Any]]:
        from meeting.capture.devices import find_loopback_device

        if channel == CHANNEL_LOOPBACK:
            return find_loopback_device()
        return self._find_mic()

    def _find_mic(self) -> Optional[Dict[str, Any]]:
        from meeting.capture.devices import find_mic_device

        choice = (list(self._options.mic_priority) if self._options.mic_priority
                  else self._options.mic_device_id)
        if self.mic_excluded:
            return find_mic_device(choice, exclude=tuple(self.mic_excluded))
        return find_mic_device(choice)

    @staticmethod
    def _mic_key(device: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not device or "name" not in device or "hostapi" not in device:
            return None
        return {"name": str(device["name"]), "hostapi": str(device["hostapi"])}

    def _exclude_mic(self, device: Optional[Dict[str, Any]]) -> bool:
        """Skip ``device`` for the rest of the meeting; False when it can't be named."""
        key = self._mic_key(device)
        if key is None or key in self.mic_excluded:
            return False
        self.mic_excluded.append(key)
        return True

    def _open_mic(
        self,
        desired: Optional[Dict[str, Any]] = None,
        *,
        reuse_spool: bool = False,
    ) -> bool:
        """Open the best microphone left, moving past ones that won't open."""
        from meeting.capture.sd_stream import SdCaptureSource

        retried_all = False
        for _attempt in range(MIC_OPEN_ATTEMPTS):
            device, desired = desired, None
            if device is None:
                try:
                    device = self._find_mic()
                except Exception:
                    logger.exception("Microphone probe failed")
                    return False
            if device is None and self.mic_excluded and not retried_all:
                # Every microphone failed once. One may only have hiccuped,
                # and trying it again beats leaving the channel dead.
                retried_all = True
                self.mic_excluded = []
                continue
            if device is None:
                return False
            try:
                opened = self.start_source(
                    SdCaptureSource(
                        CHANNEL_MIC, device["index"], device["samplerate"],
                        device["channels"],
                    ),
                    reuse_spool=reuse_spool,
                )
            except Exception:
                logger.exception("Failed to open microphone stream")
                opened = False
            if opened:
                self.mic_device = device
                return True
            if not self._exclude_mic(device):
                return False
        return False

    def source(self, channel: str) -> Optional[Any]:
        with self._lock:
            return next(
                (source for source in self.sources
                 if source.channel == channel), None
            )

    def restart_channel(
        self, channel: str, desired: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Stop one channel's live source and open its replacement.

        The channel ``SpoolWriter`` is kept so audio captured before the
        restart remains durable and is stitched with later chunks.
        """
        if channel == CHANNEL_LOOPBACK and self._system_audio_disabled:
            return False
        source = self.source(channel)
        lost = (source is None or not bool(source.is_active())
                or channel in self.stall_channels)
        self._stop_source(channel)
        try:
            from meeting.capture.devices import find_loopback_device
            from meeting.capture.sd_stream import SdCaptureSource

            if channel == CHANNEL_MIC:
                if lost and self._exclude_mic(self.mic_device):
                    # The watchdog probed before it knew this source was
                    # dead and may have named the same device again.
                    desired = None
                return self._open_mic(desired, reuse_spool=True)
            # Reuse the watchdog probe when provided so recovery does not pay
            # for a second device enumeration inside the 12s budget.
            if desired is None:
                desired = find_loopback_device()
            if desired is not None:
                if self.start_source(
                    SdCaptureSource(
                        channel, desired["index"], desired["samplerate"],
                        desired["channels"],
                    ),
                    reuse_spool=True,
                ):
                    return True
            if channel == CHANNEL_LOOPBACK:
                return self.start_fallback_loopback(reuse_spool=True)
        except Exception:
            logger.exception("Could not restart %s capture", channel)
        return False

    def _stop_source(self, channel: str) -> None:
        """Stop the live source for one channel without retiring its spool."""
        with self._lock:
            sources = [s for s in self.sources if s.channel == channel]
            self.sources = [s for s in self.sources if s.channel != channel]
        for source in sources:
            try:
                source.stop()
            except Exception:
                logger.exception("Could not stop stale %s source", channel)

    def update_status(self, message: str = "") -> None:
        store = self._store()
        if store is None:
            return
        with self._lock:
            active = {
                source.channel for source in self.sources
                if bool(source.is_active())
            }
            monitors = dict(self._monitors)
        health = {
            channel: monitor.snapshot()
            for channel, monitor in monitors.items()
        }
        loopback_available = CHANNEL_LOOPBACK in active
        if (
            loopback_available
            and not self._loopback_was_available
            and not self._system_audio_disabled
        ):
            message = message or "System audio restored."
            self._explicit_message = None
        self._loopback_was_available = loopback_available
        if not message:
            message = self._gap_message(active, loopback_available)
        if self.integrity_error:
            message = (
                f"{self.integrity_error} {message}".strip()
            )
        mic_health = health.get(CHANNEL_MIC, {})
        loopback_health = health.get(CHANNEL_LOOPBACK, {})
        capture = {
            "mic_available": CHANNEL_MIC in active,
            "loopback_available": loopback_available,
            "mic_receiving": bool(mic_health.get("receiving")),
            "loopback_receiving": bool(loopback_health.get("receiving")),
            "mic_signal_windows": int(mic_health.get("signal_windows", 0)),
            "loopback_signal_windows": int(loopback_health.get("signal_windows", 0)),
            "mic_source_generation": int(mic_health.get("generation", 0)),
            "loopback_source_generation": int(loopback_health.get("generation", 0)),
            "message": message,
            "audio_incomplete": bool(self.integrity_error),
            "integrity_error": self.integrity_error or "",
        }
        previous = store.with_state(lambda state: dict(state.capture))
        if capture == previous:
            return
        if store.update_runtime_fields(capture=capture):
            self._emit_status(note=message or None)

    def _missing_parts(self, active: set) -> List[str]:
        missing = []
        if CHANNEL_MIC not in active:
            missing.append("Microphone unavailable")
        elif CHANNEL_MIC in self.stall_channels:
            missing.append(
                "Microphone is connected but no audio blocks are arriving; "
                "check the selected input device"
            )
        if CHANNEL_LOOPBACK not in active:
            if self._system_audio_disabled:
                missing.append(
                    "System audio disabled for this meeting by your "
                    "microphone-only choice"
                )
            else:
                missing.append("System audio unavailable")
        elif CHANNEL_LOOPBACK in self.stall_channels:
            missing.append(
                "System audio is connected but no audio blocks are arriving; "
                "check the output device"
            )
        return missing

    def _gap_message(self, active: set, loopback_available: bool) -> str:
        """Describe missing or stalled channels when no message was given."""
        missing = self._missing_parts(active)
        if not (self._explicit_message and not loopback_available):
            return "; ".join(missing)
        # A deliberate microphone-only message must not hide a new
        # microphone callback stall.
        message = self._explicit_message
        if CHANNEL_MIC in self.stall_channels:
            message += " " + missing[0]
        return message
