"""Meeting Mode macOS gate: probe the ScreenCaptureKit system-audio path.

Run on macOS 13+ with the project venv active:
    .venv/bin/python scripts/probe_macos_loopback.py

macOS has no WASAPI-style loopback input device, so ``scripts/probe_loopback.py``
can never pass here. The supported substitute is ScreenCaptureKit: an
``SCStream`` with ``capturesAudio`` delivers the OS-composited output mix
without a virtual audio driver. There is no audio-only stream mode, so the
stream is configured with the smallest, slowest video surface the API accepts
and every video frame is discarded.

Section 1 reports the OS version and the Screen Recording grant, requesting it
if absent. Section 2 compares ``time.monotonic()`` with the CoreMedia host
clock that ScreenCaptureKit timestamps against: Meeting Mode stamps every
``CaptureBlock`` on the ``time.monotonic()`` timeline, so a drift between the
two would slowly desync the loopback channel from the microphone in the
finished transcript. Section 3 records a few seconds of system audio and prints
the RMS, which is what proves audio actually flows -- play something audible
while it runs, since a near-zero RMS means silence was captured, not that the
path is broken.
"""
from __future__ import annotations

import platform
import statistics
import sys
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

#: Seconds of system audio captured by the probe.
PROBE_SECONDS = 4.0

#: Sample rate requested from ScreenCaptureKit. Only 48000 and 44100 are
#: honoured; anything else is silently coerced by the framework.
PROBE_RATE = 48000

#: Channel count requested from ScreenCaptureKit.
PROBE_CHANNELS = 2

#: Edge length of the dummy video surface. ScreenCaptureKit refuses to start a
#: stream with no video output, so the smallest legal frame is requested and
#: thrown away.
DUMMY_VIDEO_EDGE = 2

#: ``kAudioFormatFlagIsNonInterleaved``. ScreenCaptureKit delivers planar
#: float32, but the flag is checked rather than assumed.
NON_INTERLEAVED_FLAG = 1 << 5

#: PyObjC marshals ``AudioStreamBasicDescription`` as a plain tuple rather
#: than a struct, so its fields are reached positionally.
ASBD_SAMPLE_RATE = 0
ASBD_FORMAT_FLAGS = 2
ASBD_CHANNELS_PER_FRAME = 6
ASBD_BITS_PER_CHANNEL = 7

#: Seconds to wait for the asynchronous shareable-content query.
CONTENT_TIMEOUT_S = 5.0


def probe_platform() -> bool:
    """Print the OS version and report whether ScreenCaptureKit can be used."""
    print(f"python {sys.version.split()[0]} on {platform.platform()}")
    if sys.platform != "darwin":
        print("GATE FAILED: this probe only applies to macOS.")
        return False

    release = platform.mac_ver()[0]
    major = int(release.split(".")[0]) if release else 0
    print(f"macOS {release}")
    # Darwin 22 / macOS 13 introduced ScreenCaptureKit audio capture.
    if major and major < 13:
        print("GATE FAILED: ScreenCaptureKit system audio needs macOS 13+.")
        return False

    try:
        import ScreenCaptureKit  # noqa: F401
    except Exception as exc:
        print(f"GATE FAILED: pyobjc-framework-ScreenCaptureKit unavailable: {exc}")
        return False
    print("ScreenCaptureKit bindings importable.")
    return True


def probe_permission() -> bool:
    """Report the Screen Recording grant, requesting it when it is missing.

    On macOS 26.1+ this single grant also covers system-audio capture; on 14
    and 15 the audio and screen TCC services are separately toggleable, so a
    granted preflight is necessary but not sufficient there.

    Returns:
        True when the process holds the grant.
    """
    print("\n--- Screen Recording permission ---")
    try:
        from Quartz import (
            CGPreflightScreenCaptureAccess,
            CGRequestScreenCaptureAccess,
        )
    except Exception as exc:
        print(f"Could not import the Quartz permission API: {exc}")
        return False

    if CGPreflightScreenCaptureAccess():
        print("Already granted.")
        return True

    print("Not granted; requesting (a system dialog may appear)...")
    granted = bool(CGRequestScreenCaptureAccess())
    if granted:
        print("Granted.")
        return True
    print(
        "Denied. Grant it under System Settings > Privacy & Security >\n"
        "  Screen & System Audio Recording, then re-run this probe.\n"
        "  Note: the grant attaches to the running binary, so a source\n"
        "  checkout grants the venv's python, not an OpenWhisper.app."
    )
    return False


def _first_display():
    """Return the first shareable display, or None.

    ``SCShareableContent`` is only available asynchronously, so the completion
    handler result is marshalled back to the calling thread.
    """
    import ScreenCaptureKit as SCK

    done = threading.Event()
    box: dict = {}

    def handler(content, error):
        box["content"] = content
        box["error"] = error
        done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    if not done.wait(CONTENT_TIMEOUT_S):
        print(f"Shareable-content query timed out after {CONTENT_TIMEOUT_S}s.")
        return None
    if box.get("error") is not None:
        print(f"Shareable-content query failed: {box['error']}")
        return None
    content = box.get("content")
    displays = list(content.displays()) if content is not None else []
    if not displays:
        print("No shareable displays found.")
        return None
    return displays[0]


def _decode_audio(sample_buffer) -> Optional[Tuple[np.ndarray, int, int]]:
    """Decode one audio ``CMSampleBuffer`` into mono float32.

    Returns:
        ``(mono_frames, sample_rate, channels)`` or None when the buffer
        carries no decodable audio.
    """
    import CoreMedia as CM

    fmt = CM.CMSampleBufferGetFormatDescription(sample_buffer)
    if fmt is None:
        return None
    asbd = CM.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
    if asbd is None or len(asbd) <= ASBD_BITS_PER_CHANNEL:
        return None

    rate = int(asbd[ASBD_SAMPLE_RATE])
    channels = int(asbd[ASBD_CHANNELS_PER_FRAME]) or 1
    bits = int(asbd[ASBD_BITS_PER_CHANNEL]) or 32
    non_interleaved = bool(int(asbd[ASBD_FORMAT_FLAGS]) & NON_INTERLEAVED_FLAG)
    if bits != 32:
        print(f"  unexpected bit depth {bits}; only float32 is handled")
        return None

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
        # Planar buffers are channel-major; interleaved are frame-major.
        usable = (samples.size // channels) * channels
        samples = samples[:usable]
        if non_interleaved:
            samples = samples.reshape(channels, -1).mean(axis=0)
        else:
            samples = samples.reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32), rate, channels


def probe_capture() -> bool:
    """Record system audio through an SCStream and report level and clock skew.

    Returns:
        True when audio buffers were delivered.
    """
    print("\n--- ScreenCaptureKit system audio ---")
    import CoreMedia as CM
    import objc
    import ScreenCaptureKit as SCK
    from Foundation import NSObject

    display = _first_display()
    if display is None:
        return False
    print(f"display: {display.width()}x{display.height()} "
          f"(id {display.displayID()})")

    blocks: List[np.ndarray] = []
    skews: List[float] = []
    meta: dict = {}
    errors: List[str] = []

    class AudioSink(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, buf, kind):
            if kind != SCK.SCStreamOutputTypeAudio:
                return
            try:
                # Sample the reference clocks together: the gap between the
                # buffer's presentation time and host "now" is the offset the
                # real capture source must subtract to land on time.monotonic().
                pts = CM.CMTimeGetSeconds(
                    CM.CMSampleBufferGetPresentationTimeStamp(buf))
                mono = time.monotonic()
                decoded = _decode_audio(buf)
                if decoded is None:
                    return
                frames, rate, channels = decoded
                meta.setdefault("rate", rate)
                meta.setdefault("channels", channels)
                blocks.append(frames)
                skews.append(mono - pts)
            except Exception as exc:
                errors.append(repr(exc))

    config = SCK.SCStreamConfiguration.alloc().init()
    config.setWidth_(DUMMY_VIDEO_EDGE)
    config.setHeight_(DUMMY_VIDEO_EDGE)
    config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
    config.setCapturesAudio_(True)
    config.setSampleRate_(PROBE_RATE)
    config.setChannelCount_(PROBE_CHANNELS)
    # Without this the probe records its own output and any app playback we
    # trigger, which turns a real capture into a feedback loop.
    config.setExcludesCurrentProcessAudio_(True)

    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, [])
    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        content_filter, config, None)

    sink = AudioSink.alloc().init()
    ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
        sink, SCK.SCStreamOutputTypeAudio, None, None)
    if not ok:
        print(f"addStreamOutput failed: {err}")
        return False

    started = threading.Event()
    start_error: dict = {}

    def on_start(error):
        start_error["error"] = error
        started.set()

    stream.startCaptureWithCompletionHandler_(on_start)
    if not started.wait(CONTENT_TIMEOUT_S):
        print("startCapture timed out.")
        return False
    if start_error.get("error") is not None:
        print(f"startCapture failed: {start_error['error']}")
        return False

    print(f"capturing {PROBE_SECONDS:.0f}s -- play some audio now...")
    time.sleep(PROBE_SECONDS)

    stopped = threading.Event()
    stream.stopCaptureWithCompletionHandler_(lambda error: stopped.set())
    stopped.wait(CONTENT_TIMEOUT_S)

    if errors:
        print(f"{len(errors)} callback error(s); first: {errors[0]}")
    if not blocks:
        print("No audio buffers delivered.")
        return False

    audio = np.concatenate(blocks)
    rate = meta.get("rate", PROBE_RATE)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(audio)))
    print(f"{len(blocks)} buffers, {audio.size} frames @ {rate} Hz "
          f"({meta.get('channels')} ch downmixed)  RMS={rms:.5f}  peak={peak:.5f}")
    expected = PROBE_SECONDS * rate
    print(f"coverage: {audio.size / expected:.1%} of {expected:.0f} expected frames")
    if rms < 1e-5:
        print("  (near-silence: play audio through the default output and re-run)")

    _report_clock(skews)
    return True


def probe_clock_base() -> None:
    """Compare ``time.monotonic()` against the CoreMedia host clock.

    ScreenCaptureKit stamps every buffer against the host time clock, and
    Meeting Mode stamps every ``CaptureBlock`` with ``time.monotonic()``. If
    the two are the same underlying mach timebase, a presentation timestamp
    converts to ``t_mono`` directly and the loopback channel stays aligned
    with the PortAudio microphone for the whole meeting. If they are not, the
    capture source needs a periodically re-estimated offset instead.
    """
    import CoreMedia as CM

    print("\n--- clock base (time.monotonic vs CoreMedia host clock) ---")
    samples = []
    for _ in range(200):
        host = CM.CMTimeGetSeconds(CM.CMClockGetTime(CM.CMClockGetHostTimeClock()))
        samples.append(time.monotonic() - host)
        time.sleep(0.005)
    mean = statistics.fmean(samples)
    spread = (max(samples) - min(samples)) * 1e6
    drift = (statistics.fmean(samples[-50:]) - statistics.fmean(samples[:50])) * 1e6
    print(f"offset mean={mean * 1e6:.1f}us  spread={spread:.1f}us  "
          f"drift={drift:+.1f}us over {len(samples) * 0.005:.1f}s")
    if abs(mean) < 1e-3 and abs(drift) < 500.0:
        print("Same timebase: a presentation timestamp is usable as t_mono "
              "directly, with no correction and no drift.")
    else:
        print("Different timebase: the capture source must re-estimate the "
              "offset periodically or the channels will desync.")


def _report_clock(skews: List[float]) -> None:
    """Print how far buffer arrival lags the buffer's presentation timestamp.

    This is ScreenCaptureKit's delivery latency plus scheduling jitter. It is
    the error a source would inherit by stamping blocks on arrival, and so the
    margin won by using the presentation timestamp instead.
    """
    if not skews:
        return
    print("\n--- delivery lag (arrival minus presentation timestamp) ---")
    mean = statistics.fmean(skews)
    spread = (max(skews) - min(skews)) * 1000.0
    print(f"lag mean={mean * 1000.0:.1f}ms  jitter spread={spread:.1f}ms  "
          f"n={len(skews)}")
    print("Stamping on arrival would inject this jitter into the timeline; "
          "the presentation timestamp avoids it.")


def main() -> None:
    if not probe_platform():
        return
    if not probe_permission():
        print("\n=== GATE ===\nFAILED: Screen Recording permission is required.")
        return
    probe_clock_base()
    captured = probe_capture()
    print("\n=== GATE ===")
    if captured:
        print("PASSED: ScreenCaptureKit delivers system audio; Meeting Mode "
              "can capture the 'Others' channel on this machine.")
    else:
        print("FAILED: no system audio captured; meetings would be mic-only.")


if __name__ == "__main__":
    main()
