"""Linux Meeting Mode capture: capability probe and SoundCard source."""
from __future__ import annotations

import math
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
        self.recorder_kwargs = []

    def recorder(self, samplerate=48000, channels=None, blocksize=None):
        self.recorder_kwargs.append(
            {"samplerate": samplerate, "channels": channels,
             "blocksize": blocksize}
        )
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
            def recorder(self, samplerate=48000, channels=None, blocksize=None):
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
            def recorder(self, samplerate=48000, channels=None, blocksize=None):

                class _Recorder:
                    def __enter__(self_inner):
                        import time
                        time.sleep(2.0)  # the server never readies the stream
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

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
            def recorder(self, samplerate=48000, channels=None, blocksize=None):
                class _Recorder:
                    def __enter__(self_inner):
                        time.sleep(30.0)
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

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
            "meeting.capture.linux_audio.default_sink_id",
            side_effect=RuntimeError("gone"),
        ):
            self.assertFalse(source.is_default_device_current())

    def test_is_default_device_current_detects_sink_change(self):
        source = SoundcardLoopbackSource()
        source.device_id = "sink0"
        with patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ), patch(
            "meeting.capture.linux_audio.default_sink_id",
            return_value="sink1",
        ):
            self.assertFalse(source.is_default_device_current())

    def test_is_default_device_current_skips_monitor_resolution(self):
        """The 1s watchdog poll must not spawn pactl/systemctl each tick."""
        source = SoundcardLoopbackSource()
        source.device_id = "sink0"
        with patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ), patch(
            "meeting.capture.linux_audio.default_sink_id",
            return_value="sink0",
        ), patch(
            "meeting.capture.linux_audio.resolve_linux_monitor",
            side_effect=AssertionError("watchdog must not resolve monitors"),
        ), patch(
            "meeting.capture.linux_audio.subprocess.run",
            side_effect=AssertionError("watchdog must not spawn processes"),
        ):
            self.assertTrue(source.is_default_device_current())

    def test_linux_recorder_requests_bounded_fragments(self):
        """Server-chosen fragments arrive ~1s at a time; ask for 100 ms."""
        from meeting.capture.linux_audio import MONITOR_BLOCKSIZE

        selection = LinuxMonitorSelection(
            sink_id="sink0",
            sink_name="Speakers",
            monitor_id="sink0.monitor",
            monitor_name="Speakers Monitor",
            channels=2,
            server_kind="pulse",
        )
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        sc_module = types.ModuleType("soundcard")
        sc_module.default_speaker = sc.default_speaker
        sc_module.get_microphone = sc.get_microphone
        sc_module.all_microphones = sc.all_microphones
        source = SoundcardLoopbackSource(selection=selection)
        with patch.dict(sys.modules, {"soundcard": sc_module}), patch(
            "meeting.capture.soundcard_stream.sys.platform", "linux"
        ):
            source.start(lambda block: None)
            source.stop()
        self.assertEqual(monitor.recorder_kwargs[0]["blocksize"],
                         MONITOR_BLOCKSIZE)


# SoundCard's PulseAudio backend, as seen by linux_audio: a connection object
# (``_pulse``), the cffi lib (``_pa``) and ffi (``_ffi``).

_PA_CONTEXT_READY = 4
_PA_CONTEXT_FAILED = 5
_NULL = object()


class _FakePulseConnection:
    def __init__(self, *, state=_PA_CONTEXT_READY, server_name="pulseaudio"):
        self.context = object()
        self.state = state
        self.server_name = server_name
        self.unref_calls = []

    def _pa_context_get_state(self, context):
        return self.state

    @property
    def server_info(self):
        return {"server name": self.server_name} if self.server_name else {}

    def _pa_operation_unref(self, operation):
        self.unref_calls.append(operation)


def _soundcard_with_backend(connection, base=None):
    backend = types.ModuleType("soundcard.pulseaudio")
    backend._pulse = connection
    backend._pa = SimpleNamespace(
        PA_CONTEXT_READY=_PA_CONTEXT_READY,
        PA_STREAM_ADJUST_LATENCY=0x2000,
        PA_STREAM_DONT_MOVE=0x0200,
    )
    backend._ffi = SimpleNamespace(NULL=_NULL, callback=lambda sig: (lambda fn: fn))
    module = base if base is not None else types.ModuleType("soundcard")
    module.pulseaudio = backend
    return module


class TestSoundcardImportClassification(unittest.TestCase):
    """SoundCard connects at import, so an import failure is not always a
    missing package."""

    def _probe_with_import_error(self, error, **units):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "soundcard" or name.startswith("soundcard."):
                raise error
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import), patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._unit_active",
            side_effect=lambda unit: units.get(unit.replace("-", "_")),
        ), patch("meeting.capture.linux_audio._forget_soundcard"):
            return probe_linux_audio(platform="linux", machine="x86_64")

    def test_no_server_is_not_reported_as_a_missing_package(self):
        result = self._probe_with_import_error(
            AssertionError(), pipewire=False, pulseaudio=False
        )
        self.assertEqual(result.reason, REASON_AUDIO_SERVER_UNAVAILABLE)

    def test_pipewire_without_pulse_layer_at_import(self):
        result = self._probe_with_import_error(
            AssertionError(), pipewire=True, pipewire_pulse=False
        )
        self.assertEqual(result.reason, REASON_PIPEWIRE_PULSE_MISSING)

    def test_unloadable_pulse_library_at_import(self):
        result = self._probe_with_import_error(OSError("cannot load library"))
        self.assertEqual(result.reason, REASON_LIBPULSE_MISSING)

    def test_unexpected_import_failure_is_unknown(self):
        from meeting.capture.linux_audio import REASON_UNKNOWN_FAILURE

        result = self._probe_with_import_error(IndexError("argv"))
        self.assertEqual(result.reason, REASON_UNKNOWN_FAILURE)
        self.assertEqual(result.detail, "IndexError")


class TestServerDetectionFromConnection(unittest.TestCase):
    def _detect(self, connection):
        from meeting.capture.linux_audio import detect_linux_audio_server

        module = _soundcard_with_backend(connection)
        with patch(
            "meeting.capture.linux_audio._run_text",
            side_effect=AssertionError("pactl must not be needed"),
        ), patch(
            "meeting.capture.linux_audio._unit_active",
            side_effect=AssertionError("systemd must not be needed"),
        ):
            return detect_linux_audio_server(module)

    def test_pipewire_pulse_named_by_the_connection(self):
        connection = _FakePulseConnection(
            server_name="PulseAudio (on PipeWire 1.0.5)"
        )
        self.assertEqual(self._detect(connection), "pipewire-pulse")

    def test_native_pulse_named_by_the_connection(self):
        self.assertEqual(self._detect(_FakePulseConnection()), "pulse")

    def test_dead_connection_falls_back_to_heuristics(self):
        from meeting.capture.linux_audio import (
            SERVER_UNKNOWN,
            detect_linux_audio_server,
        )

        module = _soundcard_with_backend(
            _FakePulseConnection(state=_PA_CONTEXT_FAILED)
        )
        with patch(
            "meeting.capture.linux_audio._run_text", return_value=""
        ), patch(
            "meeting.capture.linux_audio._unit_active", return_value=None
        ):
            self.assertEqual(detect_linux_audio_server(module), SERVER_UNKNOWN)

    def test_ready_without_pactl_or_systemd_user_units(self):
        """Autospawned Pulse / WSLg: server connected, nothing else to ask."""
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        _soundcard_with_backend(_FakePulseConnection(), base=sc)
        with patch(
            "meeting.capture.linux_audio._libpulse_available", return_value=True
        ), patch(
            "meeting.capture.linux_audio._run_text", return_value=""
        ), patch(
            "meeting.capture.linux_audio._unit_active", return_value=False
        ), patch(
            "meeting.capture.linux_audio.shutil.which", return_value=None
        ):
            result = probe_linux_audio(
                platform="linux",
                machine="x86_64",
                soundcard_module=sc,
                verify_open=True,
            )
        self.assertTrue(result.ready, result)
        self.assertEqual(result.server_kind, "pulse")


class TestLoadSoundcard(unittest.TestCase):
    def test_dead_connection_is_replaced(self):
        from meeting.capture.linux_audio import load_soundcard

        dead = _soundcard_with_backend(
            _FakePulseConnection(state=_PA_CONTEXT_FAILED)
        )
        live = _soundcard_with_backend(_FakePulseConnection())
        with patch(
            "meeting.capture.linux_audio._import_soundcard",
            side_effect=[dead, live],
        ), patch("meeting.capture.linux_audio._forget_soundcard") as forget:
            self.assertIs(load_soundcard(), live)
        forget.assert_called_once()

    def test_server_still_down_after_reconnect(self):
        from meeting.capture.linux_audio import load_soundcard

        dead = _soundcard_with_backend(
            _FakePulseConnection(state=_PA_CONTEXT_FAILED)
        )
        with patch(
            "meeting.capture.linux_audio._import_soundcard",
            side_effect=[dead, RuntimeError(REASON_AUDIO_SERVER_UNAVAILABLE)],
        ), patch("meeting.capture.linux_audio._forget_soundcard"):
            with self.assertRaises(RuntimeError) as raised:
                load_soundcard()
        self.assertEqual(str(raised.exception), REASON_AUDIO_SERVER_UNAVAILABLE)

    def test_queries_on_a_dead_connection_skip_null_unref(self):
        """libpulse aborts the process on pa_operation_unref(NULL)."""
        from meeting.capture.linux_audio import load_soundcard

        connection = _FakePulseConnection()
        module = _soundcard_with_backend(connection)
        with patch(
            "meeting.capture.linux_audio._import_soundcard", return_value=module
        ):
            load_soundcard()
            load_soundcard()  # hardening is applied once
        connection._pa_operation_unref(_NULL)
        connection._pa_operation_unref("operation")
        self.assertEqual(connection.unref_calls, ["operation"])

    def test_default_sink_id_raises_stable_reason(self):
        from meeting.capture.linux_audio import default_sink_id

        with self.assertRaises(RuntimeError) as raised:
            default_sink_id(_FakeSoundcard(None, []))
        self.assertEqual(str(raised.exception), REASON_DEFAULT_SINK_MISSING)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [])
        self.assertEqual(default_sink_id(sc), "sink0")


class TestPinnedMonitorRecorder(unittest.TestCase):
    def test_monitor_stream_may_not_be_moved_to_another_source(self):
        """Unplugging the output must fail the stream, not re-route it to a mic."""
        import threading

        from meeting.capture.linux_audio import (
            MONITOR_BLOCKSIZE,
            open_monitor_recorder,
        )

        connects = []

        class _StockRecorder:
            def __init__(self, id, samplerate, channels, blocksize=None):
                self._id = id
                self.stream = "stream"
                self.args = (samplerate, channels, blocksize)
                self._record_event = threading.Event()

            def _connect_stream(self, bufattr):
                raise AssertionError("the stock connect must not run")

        connection = _FakePulseConnection()
        connection._pa_stream_connect_record = (
            lambda stream, device, bufattr, flags:
            connects.append((device, flags))
        )
        connection._pa_stream_set_read_callback = lambda *args: None
        module = _soundcard_with_backend(connection)
        module.pulseaudio._Recorder = _StockRecorder
        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)

        recorder = open_monitor_recorder(
            module, monitor, samplerate=48000, channels=2
        )
        recorder._connect_stream(object())

        self.assertEqual(recorder.args, (48000, 2, MONITOR_BLOCKSIZE))
        device, flags = connects[0]
        self.assertEqual(device, b"sink0.monitor")
        self.assertTrue(flags & 0x0200, "PA_STREAM_DONT_MOVE")
        self.assertTrue(flags & 0x2000, "PA_STREAM_ADJUST_LATENCY")
        self.assertEqual(monitor.recorder_kwargs, [])

    def test_unfamiliar_soundcard_falls_back_to_stock_recorder(self):
        from meeting.capture.linux_audio import (
            MONITOR_BLOCKSIZE,
            open_monitor_recorder,
        )

        monitor = _FakeMic("sink0.monitor", "Speakers Monitor", loopback=True)
        sc = _FakeSoundcard(SimpleNamespace(id="sink0", name="Speakers"), [monitor])
        open_monitor_recorder(sc, monitor, samplerate=48000, channels=2)
        self.assertEqual(
            monitor.recorder_kwargs,
            [{"samplerate": 48000, "channels": 2,
              "blocksize": MONITOR_BLOCKSIZE}],
        )


_RATE = 48000
_N = 1024
_BLOCK_S = _N / _RATE


def _deliveries(seconds, burst_s, *, t0=100.0, lost=(), stalled=()):
    """``(block_index, capture_start, delivered_at)`` for a monitor stream.

    Audio is captured in real time from ``t0`` and handed over in bursts every
    ``burst_s``. ``lost`` spans are captured but never delivered (an upstream
    overrun); ``stalled`` spans are held back and delivered when they end.
    """
    out = []
    for k in range(int(seconds / _BLOCK_S)):
        start = t0 + k * _BLOCK_S
        end = start + _BLOCK_S
        if any(a <= start < b for a, b in lost):
            continue
        delivered = t0 + math.ceil((end - t0) / burst_s) * burst_s + 0.005
        for a, b in stalled:
            if a <= end < b:
                delivered = max(delivered, b + 0.005)
        out.append((k, start, delivered))
    return out


class TestSampleClock(unittest.TestCase):
    def _stamps(self, deliveries):
        from meeting.capture.soundcard_stream import _SampleClock

        clock = _SampleClock(_RATE)
        return [clock.stamp(_N, now) for _k, _start, now in deliveries]

    # The origin keeps tightening by a few ms as bursts land at different
    # block alignments; the spool tolerates 120 ms.
    STEP_TOLERANCE_S = 0.005

    def test_bursty_delivery_stays_contiguous(self):
        deliveries = _deliveries(8.0, 1.0)
        stamps = self._stamps(deliveries)
        first_burst = sum(1 for d in deliveries if d[2] == deliveries[0][2])
        steps = np.diff(stamps[first_burst:])
        np.testing.assert_allclose(steps, _BLOCK_S, atol=self.STEP_TOLERANCE_S)
        # Stamped at capture time, not up to a second late at delivery.
        error = max(
            abs(s - d[1])
            for s, d in zip(stamps[first_burst:], deliveries[first_burst:])
        )
        self.assertLess(error, 0.03)

    def test_backlog_after_a_stall_is_not_a_gap(self):
        deliveries = _deliveries(8.0, 0.1, stalled=[(103.0, 104.0)])
        stamps = self._stamps(deliveries)
        settled = next(i for i, d in enumerate(deliveries) if d[2] > 101.0)
        steps = np.diff(stamps[settled:])
        np.testing.assert_allclose(steps, _BLOCK_S, atol=self.STEP_TOLERANCE_S)
        for stamp, (_k, start, _now) in zip(stamps[settled:],
                                            deliveries[settled:]):
            self.assertAlmostEqual(stamp, start, delta=0.03)

    def test_lost_frames_skip_ahead(self):
        from meeting.capture.soundcard_stream import _CLOCK_WINDOW_S

        deliveries = _deliveries(10.0, 0.1, lost=[(103.0, 104.0)])
        stamps = self._stamps(deliveries)
        # The window open when frames went missing still holds a punctual
        # block, so the skip lands by the end of the next one.
        settle = 104.0 + 2 * _CLOCK_WINDOW_S + 0.2
        late = [(s, d) for s, d in zip(stamps, deliveries) if d[2] > settle]
        self.assertTrue(late)
        for stamp, (_k, start, _now) in late:
            self.assertAlmostEqual(stamp, start, delta=0.03)

    def test_long_dropout_skips_ahead_at_once(self):
        deliveries = _deliveries(9.0, 0.1, lost=[(103.0, 106.0)])
        stamps = self._stamps(deliveries)
        resumed = next(i for i, d in enumerate(deliveries) if d[1] >= 106.0)
        for stamp, (_k, start, _now) in list(zip(stamps, deliveries))[
            resumed:resumed + 5
        ]:
            self.assertAlmostEqual(stamp, start, delta=0.12)


class TestSpoolKeepsMonitorAudio(unittest.TestCase):
    """End to end through the real SpoolWriter: what reaches the WAV chunks."""

    def _kept_fraction(self, burst_s, stamp):
        """Seconds of tone in the chunks over seconds of tone captured."""
        import tempfile
        import time
        import wave

        from meeting.capture.spool import SpoolWriter
        from meeting.clock import MeetingClock
        from meeting.interfaces import CaptureBlock

        class _Repo:
            def __init__(self):
                self.next_id = 1

            def register_chunk(self, **fields):
                self.next_id += 1
                return self.next_id - 1

        chunks = []
        clock = MeetingClock()
        clock.start()
        t0 = time.monotonic()
        tone = (8000 * np.sin(2 * np.pi * 440 * np.arange(_N) / _RATE)
                ).astype(np.int16)
        with tempfile.TemporaryDirectory() as spool_dir:
            writer = SpoolWriter(
                "m_linux", "loopback", spool_dir, clock, _Repo(),
                on_chunk=chunks.append, queue_size=100000,
            )
            deliveries = _deliveries(6.0, burst_s, t0=t0)
            for t_mono in stamp(deliveries):
                writer.feed(CaptureBlock(channel="loopback", frames=tone,
                                         sample_rate=_RATE, t_mono=t_mono))
            writer.flush()
            samples = []
            for chunk in chunks:
                with wave.open(chunk.file_path, "rb") as wav:
                    samples.append(np.frombuffer(
                        wav.readframes(wav.getnframes()), dtype=np.int16
                    ))
        pcm = np.concatenate(samples)
        # Gap fills are digital zeros and trimmed audio is simply absent; a
        # 440 Hz tone at 8000 spends under 1% of samples below 200.
        tone_s = np.count_nonzero(np.abs(pcm) > 200) / 16000.0
        return tone_s / (len(deliveries) * _BLOCK_S)

    @staticmethod
    def _sample_clock(deliveries):
        from meeting.capture.soundcard_stream import _SampleClock

        clock = _SampleClock(_RATE)
        return [clock.stamp(_N, now) for _k, _start, now in deliveries]

    @staticmethod
    def _delivery_time(deliveries):
        return [now - _BLOCK_S for _k, _start, now in deliveries]

    def test_delivery_time_stamps_lose_bursty_audio(self):
        # The pre-fix behavior, measured at ~6% kept on PulseAudio 17.
        self.assertLess(self._kept_fraction(1.0, self._delivery_time), 0.3)

    def test_sample_clock_keeps_bursty_audio(self):
        # Only the first burst, before the origin settles, can be trimmed.
        self.assertGreater(self._kept_fraction(1.0, self._sample_clock), 0.8)

    def test_sample_clock_keeps_monitor_blocksize_audio(self):
        self.assertGreater(self._kept_fraction(0.1, self._sample_clock), 0.97)


if __name__ == "__main__":
    unittest.main()
