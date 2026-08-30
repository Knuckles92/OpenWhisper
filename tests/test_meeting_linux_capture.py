"""Linux Meeting Mode capture: capability probe and SoundCard source."""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from meeting.capture.linux_audio import (
    REASON_AUDIO_SERVER_UNAVAILABLE,
    REASON_DEFAULT_SINK_MISSING,
    REASON_LIBPULSE_MISSING,
    REASON_MONITOR_OPEN_FAILED,
    REASON_MONITOR_SOURCE_MISSING,
    REASON_PACTL_MISSING,
    REASON_PIPEWIRE_PULSE_MISSING,
    REASON_READY,
    REASON_SOUNDCARD_MISSING,
    REASON_UNSUPPORTED_ARCHITECTURE,
    LinuxMonitorSelection,
    probe_linux_audio,
    resolve_linux_monitor,
)
from meeting.capture.soundcard_stream import SoundcardLoopbackSource
from meeting.platform import meeting_mode_supported, normalize_linux_machine


class _FakeMic:
    def __init__(self, device_id, name, *, loopback=False, channels=2):
        self.id = device_id
        self.name = name
        self.isloopback = loopback
        self.channels = channels
        self._blocks = [np.zeros((512, channels), dtype=np.float32)]

    def recorder(self, samplerate=48000, channels=None):
        mic = self

        class _Recorder:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def record(self_inner, numframes=1024):
                if mic._blocks:
                    return mic._blocks.pop(0)
                return np.zeros((numframes, channels or mic.channels), dtype=np.float32)

        return _Recorder()


class _FakeSoundcard:
    def __init__(self, speaker, monitors):
        self._speaker = speaker
        self._monitors = list(monitors)

    def default_speaker(self):
        if self._speaker is None:
            raise RuntimeError("no default sink")
        return self._speaker

    def get_microphone(self, id, include_loopback=False):
        needle = str(id)
        # SoundCard resolves a speaker name/id to its monitor when loopback
        # is requested; mirror that for tests.
        if include_loopback and self._speaker is not None:
            speaker_id = str(getattr(self._speaker, "id", "") or "")
            speaker_name = str(getattr(self._speaker, "name", "") or "")
            if needle in {speaker_id, speaker_name}:
                for mic in self._monitors:
                    if mic.isloopback:
                        return mic
        for mic in self._monitors:
            if str(mic.id) == needle or str(mic.name) == needle:
                if include_loopback or not mic.isloopback:
                    return mic
        raise RuntimeError(f"microphone not found: {id}")

    def all_microphones(self, include_loopback=False):
        if include_loopback:
            return list(self._monitors)
        return [m for m in self._monitors if not m.isloopback]


class TestArchitecturePolicy(unittest.TestCase):
    def test_linux_architecture_aliases(self):
        self.assertEqual(normalize_linux_machine("x86_64"), "linux_x86_64")
        self.assertEqual(normalize_linux_machine("AMD64"), "linux_x86_64")
        self.assertEqual(normalize_linux_machine("aarch64"), "linux_aarch64")
        self.assertEqual(normalize_linux_machine("arm64"), "linux_aarch64")
        self.assertIsNone(normalize_linux_machine("i686"))
        self.assertIsNone(normalize_linux_machine("armv7l"))

    def test_meeting_mode_supported_linux_architectures(self):
        from meeting.platform import linux_meeting_implementation_ready
        # Public promotion stays gated; implementation readiness is separate.
        self.assertFalse(meeting_mode_supported("linux", machine="x86_64"))
        self.assertFalse(meeting_mode_supported("linux", machine="aarch64"))
        self.assertTrue(linux_meeting_implementation_ready("x86_64"))
        self.assertTrue(linux_meeting_implementation_ready("aarch64"))
        self.assertFalse(meeting_mode_supported("linux", machine="i686"))
        self.assertTrue(meeting_mode_supported("win32"))
        with patch("meeting.platform.platform_module.mac_ver",
                   return_value=("13.0", ("", "", ""), "arm64")):
            self.assertTrue(meeting_mode_supported("darwin"))


class TestLinuxProbe(unittest.TestCase):
    def test_unsupported_architecture(self):
        result = probe_linux_audio(platform="linux", machine="ppc64")
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, REASON_UNSUPPORTED_ARCHITECTURE)

    def test_soundcard_missing(self):
        with patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ):
            real_import = __import__

            def fake_import(name, *args, **kwargs):
                if name == "soundcard" or name.startswith("soundcard."):
                    raise ModuleNotFoundError("soundcard")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                result = probe_linux_audio(platform="linux", machine="x86_64")
        self.assertEqual(result.reason, REASON_SOUNDCARD_MISSING)

    def test_libpulse_checked_before_soundcard_import(self):
        """Missing libpulse must win even if SoundCard import would fail."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "soundcard" or name.startswith("soundcard."):
                raise ModuleNotFoundError("soundcard-should-not-load")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=False
        ):
            result = probe_linux_audio(platform="linux", machine="x86_64")
        self.assertEqual(result.reason, REASON_LIBPULSE_MISSING)

    def test_libpulse_missing(self):
        sc = _FakeSoundcard(
            SimpleNamespace(id="sink0", name="Speakers"),
            [_FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)],
        )

        def loader(_name):
            raise OSError("missing")

        result = probe_linux_audio(
            platform="linux",
            machine="x86_64",
            soundcard_module=sc,
            libpulse_loader=loader,
            verify_open=False,
        )
        self.assertEqual(result.reason, REASON_LIBPULSE_MISSING)

    def test_valid_monitor(self):
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pipewire-pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink"
        ) as pactl_monitor:
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=True,
            )
        self.assertTrue(result.ready)
        self.assertEqual(result.reason, REASON_READY)
        self.assertEqual(result.default_sink, "sink0")
        self.assertEqual(result.monitor_source, "sink0.monitor")
        pactl_monitor.assert_not_called()

    def test_sink_keyed_soundcard_loopback_succeeds_without_pactl(self):
        monitor = _FakeMic("sink0", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink"
        ) as pactl_monitor:
            selection = resolve_linux_monitor(sc, server_kind="pulse")
        self.assertEqual(selection.monitor_id, "sink0.monitor")
        self.assertEqual(selection.soundcard_id, "sink0")
        pactl_monitor.assert_not_called()

    def test_nonstandard_monitor_name_uses_exact_pactl_fallback(self):
        monitor = _FakeMic("custom.monitor.name", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="custom.monitor.name",
        ) as pactl_monitor:
            selection = resolve_linux_monitor(sc, server_kind="pulse")
        self.assertEqual(selection.monitor_id, "custom.monitor.name")
        self.assertEqual(selection.soundcard_id, "custom.monitor.name")
        pactl_monitor.assert_called_once_with("sink0")

    def test_missing_pactl_is_distinct_when_fallback_is_needed(self):
        monitor = _FakeMic("custom.monitor.name", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink", return_value=None
        ), patch(
            "meeting.capture.linux_audio.shutil.which", return_value=None
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_PACTL_MISSING)

    def test_non_loopback_rejected(self):
        mic = _FakeMic("mic0", "USB Mic", loopback=False)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [mic])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_MONITOR_SOURCE_MISSING)

    def test_name_only_monitor_device_without_flag_is_rejected(self):
        """A physical mic named 'monitor' must not pass without isloopback."""
        mic = _FakeMic("mic0", "USB Monitor Loopback", loopback=False)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [mic])

        def get_microphone(id, include_loopback=False):
            return mic

        sc.get_microphone = get_microphone
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_MONITOR_SOURCE_MISSING)

    def test_pipewire_without_pulse(self):
        sc = _FakeSoundcard(None, [])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="unavailable",
        ), patch(
            "meeting.capture.linux_audio._unit_active",
            side_effect=lambda unit: True if unit == "pipewire" else False,
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_PIPEWIRE_PULSE_MISSING)

    def test_audio_server_unavailable(self):
        sc = _FakeSoundcard(None, [])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="unavailable",
        ), patch(
            "meeting.capture.linux_audio._unit_active", return_value=False
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_AUDIO_SERVER_UNAVAILABLE)

    def test_default_sink_missing(self):
        sc = _FakeSoundcard(None, [])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=False,
            )
        self.assertEqual(result.reason, REASON_DEFAULT_SINK_MISSING)

    def test_monitor_open_failed(self):
        class BrokenMic(_FakeMic):
            def recorder(self, samplerate=48000, channels=None):
                raise RuntimeError("busy")

        monitor = BrokenMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=True,
            )
        self.assertEqual(result.reason, REASON_MONITOR_OPEN_FAILED)

    def test_monitor_open_timeout_classified(self):
        class BlockingMic(_FakeMic):
            def recorder(self, samplerate=48000, channels=None):
                mic = self

                class _Recorder:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

                    def record(self_inner, numframes=1024):
                        import time
                        time.sleep(2.0)
                        return np.zeros((numframes, 2), dtype=np.float32)

                return _Recorder()

        monitor = BlockingMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=True,
                open_timeout_s=0.2,
            )
        self.assertEqual(result.reason, REASON_MONITOR_OPEN_FAILED)

    def test_resolve_linux_monitor_ids(self):
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        with patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            selection = resolve_linux_monitor(sc, server_kind="pulse")
        self.assertIsInstance(selection, LinuxMonitorSelection)
        self.assertEqual(selection.sink_id, "sink0")
        self.assertEqual(selection.monitor_id, "sink0.monitor")

    def test_wrong_sink_monitor_is_rejected(self):
        """Default sink0 must not accept another sink's monitor."""
        wrong = _FakeMic("sink1.monitor", "Other Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [wrong])

        def get_microphone(id, include_loopback=False):
            # Simulate fuzzy SoundCard match returning the wrong loopback.
            return wrong

        sc.get_microphone = get_microphone
        with patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            with self.assertRaises(RuntimeError) as raised:
                resolve_linux_monitor(sc, server_kind="pulse")
        self.assertEqual(str(raised.exception), REASON_MONITOR_SOURCE_MISSING)

    def test_pactl_without_evidence_does_not_fabricate_monitor(self):
        from meeting.capture.linux_audio import _pactl_monitor_for_sink

        with patch(
            "meeting.capture.linux_audio._run_text", return_value=""
        ):
            self.assertIsNone(_pactl_monitor_for_sink("sink0"))

    def test_monitor_open_timeout_leaves_only_daemon_workers(self):
        import threading
        import time

        class ForeverMic(_FakeMic):
            def recorder(self, samplerate=48000, channels=None):
                class _Recorder:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

                    def record(self_inner, numframes=1024):
                        time.sleep(30.0)
                        return np.zeros((numframes, 2), dtype=np.float32)

                return _Recorder()

        monitor = ForeverMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        before = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        with patch(
            "meeting.capture.linux_audio.detect_linux_audio_server",
            return_value="pulse",
        ), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._pactl_monitor_for_sink",
            return_value="sink0.monitor",
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=True,
                open_timeout_s=0.15,
            )
        self.assertEqual(result.reason, REASON_MONITOR_OPEN_FAILED)
        after_non_daemon = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        self.assertEqual(after_non_daemon, before)


class TestSoundcardLoopbackSource(unittest.TestCase):
    def test_linux_available_uses_probe(self):
        with patch(
            "meeting.capture.linux_audio.probe_linux_audio",
            return_value=SimpleNamespace(ready=True),
        ), patch("meeting.capture.soundcard_stream.sys.platform", "linux"):
            self.assertTrue(SoundcardLoopbackSource.available())

    def test_start_emits_mono_blocks(self):
        selection = LinuxMonitorSelection(
            sink_id="sink0",
            sink_name="Speakers",
            monitor_id="sink0.monitor",
            monitor_name="Speakers Monitor",
            channels=2,
            server_kind="pulse",
        )
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        # Keep a few blocks flowing.
        monitor._blocks = [
            np.ones((1024, 2), dtype=np.float32) * 0.1 for _ in range(5)
        ]
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        source = SoundcardLoopbackSource(selection=selection)
        blocks = []
        with patch.dict(sys.modules, {"soundcard": sc}), patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ), patch(
            "meeting.capture.linux_audio.resolve_linux_monitor",
            return_value=selection,
        ):
            # Import path uses "import soundcard as sc"; inject module attrs.
            sc_module = types.ModuleType("soundcard")
            sc_module.default_speaker = sc.default_speaker
            sc_module.get_microphone = sc.get_microphone
            sc_module.all_microphones = sc.all_microphones
            with patch.dict(sys.modules, {"soundcard": sc_module}):
                source.start(blocks.append)
                self.assertTrue(source.is_active())
                source.stop()
        self.assertGreaterEqual(len(blocks), 1)
        self.assertEqual(blocks[0].channel, "loopback")
        self.assertEqual(blocks[0].sample_rate, 48000)
        self.assertEqual(blocks[0].frames.ndim, 1)
        self.assertEqual(blocks[0].frames.dtype, np.int16)

    def test_is_default_device_current_fails_closed(self):
        source = SoundcardLoopbackSource()
        source.device_id = "sink0"
        with patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ), patch(
            "meeting.capture.linux_audio.resolve_linux_monitor",
            side_effect=RuntimeError("gone"),
        ):
            self.assertFalse(source.is_default_device_current())

    def test_is_default_device_current_detects_sink_change(self):
        source = SoundcardLoopbackSource()
        source.device_id = "sink0"
        selection = LinuxMonitorSelection(
            sink_id="sink1",
            sink_name="HDMI",
            monitor_id="sink1.monitor",
            monitor_name="HDMI Monitor",
            channels=2,
            server_kind="pulse",
        )
        with patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ), patch(
            "meeting.capture.linux_audio.resolve_linux_monitor",
            return_value=selection,
        ):
            self.assertFalse(source.is_default_device_current())


if __name__ == "__main__":
    unittest.main()
