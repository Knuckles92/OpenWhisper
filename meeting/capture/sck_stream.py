"""System-audio capture for Meeting Mode on macOS, via ScreenCaptureKit.

macOS exposes no WASAPI-style loopback *input* device, so the "Others" channel
cannot come from PortAudio the way it does on Windows. ScreenCaptureKit's
``SCStream`` delivers the OS-composited output mix instead, which needs no
virtual audio driver and follows the default output device on its own.

Three things about this API are not obvious:

* There is no audio-only stream mode. The stream is configured with the
  smallest, slowest video surface ScreenCaptureKit accepts and every video
  frame is discarded -- the same shape shipping macOS recorders use.
* Buffers are timestamped against the CoreMedia host clock, which is the same
  mach timebase as ``time.monotonic()`` (measured offset ~15us, no drift). The
  presentation timestamp is therefore used as ``CaptureBlock.t_mono`` directly.
  Stamping on arrival instead would fold ScreenCaptureKit's ~55 ms delivery
  latency and ~28 ms of jitter into the timeline and desync this channel from
  the PortAudio microphone.
* The stream keeps delivering buffers through silence rather than pausing, so
  an absence of buffers is a real fault. ``is_active`` treats a stalled stream
  as dead so the engine's capture watchdog can restart it.

Everything is imported lazily so this module imports cleanly on other
platforms and when the ScreenCaptureKit bindings are not installed.
"""
from __future__ import annotations

import logging
import platform
import sys
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np

from meeting.interfaces import CHANNEL_LOOPBACK, CaptureBlock

logger = logging.getLogger(__name__)

#: Capture sample rate requested from ScreenCaptureKit. Only 48000 and 44100
#: are honoured; other values are silently coerced by the framework.
SAMPLERATE = 48000

#: Channel count requested from ScreenCaptureKit, downmixed to mono on arrival.
CHANNELS = 2

#: Edge length of the discarded video surface. ScreenCaptureKit will not start
#: a stream with no video output, so the smallest legal frame is requested.
DUMMY_VIDEO_EDGE = 2

#: ScreenCaptureKit audio capture requires macOS 13 (Ventura).
MIN_MACOS_MAJOR = 13

#: ``kAudioFormatFlagIsNonInterleaved``. ScreenCaptureKit delivers planar
#: float32, but the flag is checked rather than assumed.
_NON_INTERLEAVED_FLAG = 1 << 5

#: PyObjC marshals ``AudioStreamBasicDescription`` as a plain tuple rather than
#: a struct, so its fields are reached positionally.
_ASBD_SAMPLE_RATE = 0
_ASBD_FORMAT_FLAGS = 2
_ASBD_CHANNELS_PER_FRAME = 6
_ASBD_BITS_PER_CHANNEL = 7

#: Bounds on the asynchronous shareable-content query and stream start, so a
#: wedged capture daemon cannot hang meeting startup.
_CONTENT_TIMEOUT_S = 5.0
_START_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 2.0

#: How long a freshly started stream may go without delivering its first
#: buffer before it is judged dead.
_FIRST_BLOCK_GRACE_S = 3.0

#: How long a running stream may go without delivering a buffer before it is
#: judged dead. Well above the ~20 ms cadence plus observed jitter.
_STALL_TIMEOUT_S = 3.0

#: Full tracebacks are logged for at most this many callback errors.
_MAX_LOGGED_CALLBACK_ERRORS = 5

#: The PyObjC delegate class, built once. Defining an Objective-C class twice
#: under the same name raises, so it cannot be created per instance.
_SINK_CLASS = None
_SINK_CLASS_LOCK = threading.Lock()


def _macos_major() -> int:
    """Major macOS version, or 0 when it cannot be determined."""
    release = platform.mac_ver()[0]
    try:
        return int(release.split(".")[0]) if release else 0
    except ValueError:
        return 0


def _sink_class():
    """Return the ScreenCaptureKit output delegate class, building it once."""
    global _SINK_CLASS
    with _SINK_CLASS_LOCK:
        if _SINK_CLASS is not None:
            return _SINK_CLASS

        import objc
        import ScreenCaptureKit as SCK
        from Foundation import NSObject

        class _MeetingAudioSink(
            NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]
        ):
            """Forwards SCStream audio buffers to a Python handler."""

            handler = None

            def stream_didOutputSampleBuffer_ofType_(self, stream, buf, kind):
                if kind != SCK.SCStreamOutputTypeAudio:
                    return
                handler = self.handler
                if handler is not None:
                    handler(buf)

        _SINK_CLASS = _MeetingAudioSink
        return _SINK_CLASS


def _decode_mono_int16(sample_buffer) -> Optional[Tuple[np.ndarray, int]]:
    """Decode one audio ``CMSampleBuffer`` into mono int16.

    Args:
        sample_buffer: A ``CMSampleBufferRef`` carrying LPCM float32 audio.

    Returns:
        ``(frames, sample_rate)`` or None when the buffer carries no decodable
        audio.
    """
    import CoreMedia as CM

    fmt = CM.CMSampleBufferGetFormatDescription(sample_buffer)
    if fmt is None:
        return None
    asbd = CM.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
    if asbd is None or len(asbd) <= _ASBD_BITS_PER_CHANNEL:
        return None
    if int(asbd[_ASBD_BITS_PER_CHANNEL]) != 32:
        return None

    rate = int(asbd[_ASBD_SAMPLE_RATE]) or SAMPLERATE
    channels = int(asbd[_ASBD_CHANNELS_PER_FRAME]) or 1
    non_interleaved = bool(int(asbd[_ASBD_FORMAT_FLAGS]) & _NON_INTERLEAVED_FLAG)

    block = CM.CMSampleBufferGetDataBuffer(sample_buffer)
    if block is None:
        return None
    length = CM.CMBlockBufferGetDataLength(block)
    if not length:
        return None
    status, data = CM.CMBlockBufferCopyDataBytes(block, 0, length, None)
    if status != 0 or not data:
        return None

    samples = np.frombuffer(bytes(data), dtype=np.float32)
    if channels > 1:
        # Planar buffers are channel-major, interleaved ones frame-major; a
        # ragged tail would break either reshape.
        usable = (samples.size // channels) * channels
        samples = samples[:usable]
        if non_interleaved:
            samples = samples.reshape(channels, -1).mean(axis=0)
        else:
            samples = samples.reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        return None
    frames = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
    return frames, rate


class ScreenCaptureKitLoopbackSource:
    """Loopback ``CaptureSource`` backed by ScreenCaptureKit on macOS."""

    def __init__(self) -> None:
        self.channel = CHANNEL_LOOPBACK
        self.device_id = "screencapturekit-system-mix"
        self._on_block: Optional[Callable[[CaptureBlock], None]] = None
        self._stream = None
        self._sink = None
        self._started = False
        self._start_mono = 0.0
        self._last_block_mono = 0.0
        self._callback_errors = 0
        self._logged_first_block = False

    @staticmethod
    def available() -> bool:
        """True when this machine can capture system audio right now.

        Checks the Screen Recording grant with a preflight that never prompts:
        raising the system dialog belongs to the meeting start flow, not to a
        capability probe that the capture watchdog also calls.
        """
        if sys.platform != "darwin":
            return False
        major = _macos_major()
        if major and major < MIN_MACOS_MAJOR:
            logger.warning(
                "ScreenCaptureKit system audio needs macOS %d+; found %d",
                MIN_MACOS_MAJOR, major,
            )
            return False
        try:
            import ScreenCaptureKit  # noqa: F401
            from Quartz import CGPreflightScreenCaptureAccess
        except Exception as exc:
            logger.warning("ScreenCaptureKit bindings unavailable: %s", exc)
            return False
        if not CGPreflightScreenCaptureAccess():
            logger.warning(
                "Screen Recording permission not granted; system audio "
                "capture is unavailable"
            )
            return False
        return True

    def start(self, on_block: Callable[[CaptureBlock], None]) -> None:
        """Start the stream, delivering blocks to ``on_block``.

        Returns only once ScreenCaptureKit has confirmed the stream started or
        refused to, so a caller that checks ``is_active()`` afterwards learns
        the truth. Never raises: a missing permission or a busy capture daemon
        is an expected outcome that must fall through to a mic-only meeting.

        Args:
            on_block: Called from a ScreenCaptureKit dispatch queue with each
                ``CaptureBlock``; must be fast and must never raise.
        """
        if self._stream is not None:
            logger.warning("ScreenCaptureKit loopback capture already running")
            return
        self._on_block = on_block
        self._callback_errors = 0
        self._logged_first_block = False
        self._last_block_mono = 0.0
        self._start_mono = time.monotonic()
        try:
            self._start_stream()
        except Exception:
            logger.exception("ScreenCaptureKit loopback capture failed to start")
            self._started = False
            self._release()

    def _start_stream(self) -> None:
        import CoreMedia as CM
        import ScreenCaptureKit as SCK

        display = self._first_display()
        if display is None:
            return

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(DUMMY_VIDEO_EDGE)
        config.setHeight_(DUMMY_VIDEO_EDGE)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
        config.setCapturesAudio_(True)
        config.setSampleRate_(SAMPLERATE)
        config.setChannelCount_(CHANNELS)
        # Without this the stream records OpenWhisper's own meeting playback
        # and UI sounds back into the "Others" channel.
        config.setExcludesCurrentProcessAudio_(True)

        content_filter = (
            SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                display, []
            )
        )
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )

        sink = _sink_class().alloc().init()
        sink.handler = self._on_sample
        # ScreenCaptureKit does not retain the output object, and a collected
        # sink silently stops the audio.
        self._sink = sink

        ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            sink, SCK.SCStreamOutputTypeAudio, None, None
        )
        if not ok:
            logger.error("Could not attach the system-audio output: %s", err)
            return

        started = threading.Event()
        outcome: dict = {}

        def on_started(error):
            outcome["error"] = error
            started.set()

        stream.startCaptureWithCompletionHandler_(on_started)
        if not started.wait(_START_TIMEOUT_S):
            logger.error("ScreenCaptureKit did not start within %.1fs",
                         _START_TIMEOUT_S)
            return
        if outcome.get("error") is not None:
            logger.error("ScreenCaptureKit refused to start: %s",
                         outcome["error"])
            return

        self._stream = stream
        self._started = True
        logger.info("ScreenCaptureKit system-audio capture started "
                    "(%d Hz, %d ch)", SAMPLERATE, CHANNELS)

    def _first_display(self):
        """Return the first shareable display, or None.

        The system mix is identical whichever display backs the stream; a
        display is required only because ScreenCaptureKit has no audio-only
        content filter.
        """
        import ScreenCaptureKit as SCK

        done = threading.Event()
        box: dict = {}

        def on_content(content, error):
            box["content"] = content
            box["error"] = error
            done.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(
            on_content
        )
        if not done.wait(_CONTENT_TIMEOUT_S):
            logger.error("Shareable-content query timed out after %.1fs",
                         _CONTENT_TIMEOUT_S)
            return None
        if box.get("error") is not None:
            logger.error("Shareable-content query failed: %s", box["error"])
            return None
        content = box.get("content")
        displays = list(content.displays()) if content is not None else []
        if not displays:
            logger.error("No shareable displays; system audio unavailable")
            return None
        return displays[0]

    def stop(self) -> None:
        """Stop the stream and release ScreenCaptureKit resources."""
        self._started = False
        stream = self._stream
        if stream is not None:
            stopped = threading.Event()
            try:
                stream.stopCaptureWithCompletionHandler_(
                    lambda error: stopped.set()
                )
                stopped.wait(_STOP_TIMEOUT_S)
            except Exception:
                logger.exception("ScreenCaptureKit stopCapture failed")
        self._release()
        logger.info("ScreenCaptureKit system-audio capture stopped")

    def _release(self) -> None:
        if self._sink is not None:
            self._sink.handler = None
        self._stream = None
        self._sink = None
        self._on_block = None

    def is_active(self) -> bool:
        """True while the stream is delivering frames.

        ScreenCaptureKit keeps emitting buffers through silence, so a gap in
        delivery means the stream has stalled rather than that the room is
        quiet -- the failure mode that otherwise yields a meeting-long silent
        recording. Reporting it as inactive lets the engine watchdog restart.
        """
        if not self._started or self._stream is None:
            return False
        last = self._last_block_mono
        if last <= 0.0:
            return (time.monotonic() - self._start_mono) < _FIRST_BLOCK_GRACE_S
        return (time.monotonic() - last) < _STALL_TIMEOUT_S

    def is_default_device_current(self) -> bool:
        """Always True: the stream captures the OS mix, not a device.

        ScreenCaptureKit follows the default output device on its own, so a
        device change needs no restart -- unlike the Windows loopback paths,
        which are bound to one endpoint.
        """
        return True

    def _on_sample(self, sample_buffer) -> None:
        """Convert one ScreenCaptureKit buffer into a ``CaptureBlock``."""
        try:
            import CoreMedia as CM

            decoded = _decode_mono_int16(sample_buffer)
            if decoded is None:
                return
            frames, rate = decoded
            if not self._logged_first_block:
                self._logged_first_block = True
                peak = int(np.max(np.abs(frames.astype(np.int32)))) if frames.size else 0
                logger.info(
                    "ScreenCaptureKit first audio block: %d frames @ %d Hz peak=%d",
                    frames.size, rate, peak,
                )
            t_mono = CM.CMTimeGetSeconds(
                CM.CMSampleBufferGetPresentationTimeStamp(sample_buffer)
            )
            self._last_block_mono = time.monotonic()
            on_block = self._on_block
            if on_block is not None and self._started:
                on_block(CaptureBlock(
                    channel=self.channel,
                    frames=frames,
                    sample_rate=rate,
                    t_mono=t_mono,
                ))
        except Exception:
            self._callback_errors += 1
            if self._callback_errors <= _MAX_LOGGED_CALLBACK_ERRORS:
                logger.exception("ScreenCaptureKit block delivery failed")
