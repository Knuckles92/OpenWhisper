"""macOS Meeting Mode capture: ScreenCaptureKit source and permission policy.

These run on every platform. The ScreenCaptureKit bindings are only reachable
on macOS, so the parts that would touch them are exercised through the seams
the source already has -- ``available()`` for the capability gate and
``_decode_mono_int16`` for buffer conversion -- with fakes standing in for
CoreMedia.
"""
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from meeting.capture.sck_stream import (
    _FIRST_BLOCK_GRACE_S,
    _STALL_TIMEOUT_S,
    ScreenCaptureKitLoopbackSource,
    _decode_mono_int16,
)
from meeting.interfaces import CHANNEL_LOOPBACK
from meeting.platform import (
    system_audio_permission_granted,
    system_audio_permission_required,
)


class FakeCoreMedia:
    """Minimal CoreMedia stand-in returning one canned audio buffer."""

    def __init__(self, samples: np.ndarray, channels: int, flags: int,
                 rate: int = 48000, bits: int = 32):
        self._samples = samples.astype(np.float32)
        self._asbd = (float(rate), 1819304813, flags, 4, 1, 4, channels, bits, 0)

    def CMSampleBufferGetFormatDescription(self, buf):
        return object()

    def CMAudioFormatDescriptionGetStreamBasicDescription(self, fmt):
        return self._asbd

    def CMSampleBufferGetDataBuffer(self, buf):
        return object()

    def CMBlockBufferGetDataLength(self, block):
        return self._samples.nbytes

    def CMBlockBufferCopyDataBytes(self, block, offset, length, _dest):
        return 0, self._samples.tobytes()


class TestDecodeAudioBuffer(unittest.TestCase):
    """ScreenCaptureKit delivers planar float32; the layout must be honoured."""

    NON_INTERLEAVED = 41  # float | packed | non-interleaved
    INTERLEAVED = 9  # float | packed

    def _decode(self, fake):
        with patch.dict(sys.modules, {"CoreMedia": fake}):
            return _decode_mono_int16(object())

    def test_planar_stereo_averages_channel_planes(self):
        # Channel-major: four frames of 1.0 then four of -1.0 average to zero.
        samples = np.concatenate([np.ones(4), -np.ones(4)])
        frames, rate = self._decode(
            FakeCoreMedia(samples, channels=2, flags=self.NON_INTERLEAVED)
        )
        self.assertEqual(rate, 48000)
        self.assertEqual(frames.dtype, np.int16)
        self.assertEqual(frames.size, 4)
        np.testing.assert_array_equal(frames, np.zeros(4, dtype=np.int16))

    def test_interleaved_stereo_averages_frame_pairs(self):
        # Frame-major: the same bytes read as L/R pairs never cancel out.
        samples = np.concatenate([np.ones(4), -np.ones(4)])
        frames, _ = self._decode(
            FakeCoreMedia(samples, channels=2, flags=self.INTERLEAVED)
        )
        self.assertEqual(frames.size, 4)
        np.testing.assert_array_equal(
            frames, np.array([32767, 32767, -32767, -32767], dtype=np.int16)
        )

    def test_full_scale_does_not_wrap(self):
        frames, _ = self._decode(
            FakeCoreMedia(np.array([1.0, -1.0, 2.0, -2.0]), channels=1,
                          flags=self.INTERLEAVED)
        )
        self.assertEqual(frames.dtype, np.int16)
        self.assertTrue((frames >= -32768).all() and (frames <= 32767).all())

    def test_odd_tail_is_dropped_rather_than_reshaped(self):
        frames, _ = self._decode(
            FakeCoreMedia(np.ones(5), channels=2, flags=self.NON_INTERLEAVED)
        )
        self.assertEqual(frames.size, 2)

    def test_non_float_depth_is_refused(self):
        self.assertIsNone(self._decode(
            FakeCoreMedia(np.ones(4), channels=1, flags=self.INTERLEAVED,
                          bits=16)
        ))

    def test_empty_buffer_is_refused(self):
        self.assertIsNone(self._decode(
            FakeCoreMedia(np.array([]), channels=1, flags=self.INTERLEAVED)
        ))


class TestAvailability(unittest.TestCase):
    def test_unavailable_off_macos(self):
        with patch("meeting.capture.sck_stream.sys.platform", "win32"):
            self.assertFalse(ScreenCaptureKitLoopbackSource.available())
        with patch("meeting.capture.sck_stream.sys.platform", "linux"):
            self.assertFalse(ScreenCaptureKitLoopbackSource.available())

    def test_unavailable_before_macos_13(self):
        with patch("meeting.capture.sck_stream.sys.platform", "darwin"), \
                patch("meeting.capture.sck_stream._macos_major", return_value=12):
            self.assertFalse(ScreenCaptureKitLoopbackSource.available())

    @unittest.skipUnless(sys.platform == "darwin", "needs the macOS bindings")
    def test_availability_tracks_the_permission_preflight(self):
        with patch("Quartz.CGPreflightScreenCaptureAccess", return_value=False):
            self.assertFalse(ScreenCaptureKitLoopbackSource.available())
        with patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True):
            self.assertTrue(ScreenCaptureKitLoopbackSource.available())


class TestSourceContract(unittest.TestCase):
    """The engine and its watchdog rely on this surface, not on the protocol."""

    def setUp(self):
        self.source = ScreenCaptureKitLoopbackSource()

    def test_reports_the_loopback_channel(self):
        self.assertEqual(self.source.channel, CHANNEL_LOOPBACK)

    def test_device_id_is_not_an_int(self):
        # An int device_id routes the watchdog into index comparison against
        # a WASAPI probe that never returns anything on macOS.
        self.assertNotIsInstance(self.source.device_id, int)

    def test_default_device_never_changes(self):
        # ScreenCaptureKit captures the OS mix and follows the default output
        # itself, so the watchdog must not restart it on a device change.
        self.assertTrue(self.source.is_default_device_current())

    def test_inactive_before_start(self):
        self.assertFalse(self.source.is_active())

    def test_active_during_the_first_block_grace_period(self):
        self.source._started = True
        self.source._stream = object()
        self.source._start_mono = time.monotonic()
        self.assertTrue(self.source.is_active())

    def test_dead_when_no_first_block_arrives(self):
        self.source._started = True
        self.source._stream = object()
        self.source._start_mono = time.monotonic() - _FIRST_BLOCK_GRACE_S - 1.0
        self.assertFalse(self.source.is_active())

    def test_dead_when_the_stream_stalls(self):
        # ScreenCaptureKit emits buffers through silence, so a gap is a fault
        # rather than a quiet room -- the silent-recording failure mode.
        self.source._started = True
        self.source._stream = object()
        self.source._last_block_mono = time.monotonic() - _STALL_TIMEOUT_S - 1.0
        self.assertFalse(self.source.is_active())

    def test_active_while_blocks_keep_arriving(self):
        self.source._started = True
        self.source._stream = object()
        self.source._last_block_mono = time.monotonic()
        self.assertTrue(self.source.is_active())

    def test_start_failure_leaves_the_source_inactive(self):
        # The engine falls through to a mic-only meeting on a False here, so
        # a raise would turn a missing permission into a failed start.
        with patch.object(ScreenCaptureKitLoopbackSource, "_start_stream",
                          side_effect=RuntimeError("no permission")):
            self.source.start(lambda block: None)
        self.assertFalse(self.source.is_active())


class TestEngineFallbackChain(unittest.TestCase):
    """``_start_fallback_loopback`` picks a backend without a platform switch.

    Each backend rules itself out through ``available()``, so the engine can
    keep one ordered list instead of branching on ``sys.platform``.
    """

    def _engine(self, started):
        from meeting.engine import MeetingEngine

        engine = SimpleNamespace(_sources=[])
        engine._start_source = lambda source: (started.append(source), True)[1]
        engine._start_fallback_loopback = (
            MeetingEngine._start_fallback_loopback.__get__(engine)
        )
        return engine

    def _run(self, sck_available, soundcard_available, started=None):
        started = started if started is not None else []
        with patch.object(ScreenCaptureKitLoopbackSource, "available",
                          return_value=sck_available), \
                patch("meeting.capture.soundcard_stream."
                      "SoundcardLoopbackSource.available",
                      return_value=soundcard_available):
            return self._engine(started)._start_fallback_loopback(), started

    def test_screencapturekit_is_preferred_when_available(self):
        ok, started = self._run(sck_available=True, soundcard_available=True)
        self.assertTrue(ok)
        self.assertEqual(len(started), 1)
        self.assertIsInstance(started[0], ScreenCaptureKitLoopbackSource)

    def test_soundcard_is_used_when_screencapturekit_is_not(self):
        ok, started = self._run(sck_available=False, soundcard_available=True)
        self.assertTrue(ok)
        self.assertEqual(len(started), 1)
        self.assertNotIsInstance(started[0], ScreenCaptureKitLoopbackSource)

    def test_no_backend_degrades_instead_of_raising(self):
        ok, started = self._run(sck_available=False, soundcard_available=False)
        self.assertFalse(ok)
        self.assertEqual(started, [])

    def test_a_raising_backend_does_not_block_the_next_one(self):
        from meeting.engine import MeetingEngine

        started = []
        engine = SimpleNamespace(_sources=[])
        engine._start_source = lambda source: (started.append(source), True)[1]
        engine._start_fallback_loopback = (
            MeetingEngine._start_fallback_loopback.__get__(engine)
        )
        with patch.object(ScreenCaptureKitLoopbackSource, "available",
                          side_effect=RuntimeError("bindings exploded")), \
                patch("meeting.capture.soundcard_stream."
                      "SoundcardLoopbackSource.available", return_value=True):
            self.assertTrue(engine._start_fallback_loopback())
        self.assertEqual(len(started), 1)


class TestPermissionPolicy(unittest.TestCase):
    def test_only_macos_requires_a_grant(self):
        self.assertTrue(system_audio_permission_required("darwin"))
        self.assertFalse(system_audio_permission_required("win32"))
        self.assertFalse(system_audio_permission_required("linux"))

    def test_granted_off_macos_where_no_grant_applies(self):
        with patch("meeting.platform.sys.platform", "win32"):
            self.assertTrue(system_audio_permission_granted())

    @unittest.skipUnless(sys.platform == "darwin", "needs the macOS bindings")
    def test_preflight_result_is_passed_through(self):
        with patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True):
            self.assertTrue(system_audio_permission_granted())
        with patch("Quartz.CGPreflightScreenCaptureAccess", return_value=False):
            self.assertFalse(system_audio_permission_granted())


if __name__ == "__main__":
    unittest.main()
