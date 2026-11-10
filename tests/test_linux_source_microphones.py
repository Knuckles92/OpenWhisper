"""Linux: PulseAudio and PipeWire sources are microphones PortAudio cannot list.

PortAudio only sees ALSA there, which has no entry for a Bluetooth headset.
The sources are listed beside ALSA's own devices and opened through the ALSA
"pipewire" or "pulse" device, routed to the source by the environment.
"""
import os
import sys
from types import SimpleNamespace

import pytest

from meeting.capture import sd_stream
from meeting.capture.devices import find_mic_device
from services import audio_devices
from services import recorder as recorder_module
from services.audio_devices import SOURCE_HOSTAPI, InputDevice, list_input_devices, ranked_candidates
from services.recorder import AudioRecorder
from services.settings import SettingsKey
from tests import test_personalize_s7_settings as s7
from tests.test_personalize_s7_audio_devices import FakeSd, _dev
from tests.test_personalize_s7_recorder import StreamFactory

_qapp = s7._qapp
make_dialog = s7.make_dialog

ROUTE = ("PULSE_SOURCE", "PIPEWIRE_NODE")
HEADSET = {"name": "WH-1000XM4", "hostapi": SOURCE_HOSTAPI}
LAPTOP = {"name": "Built-in Audio Analog Stereo", "hostapi": SOURCE_HOSTAPI}

# `pactl list sources` on PipeWire 1.0.5, trimmed.
PIPEWIRE_LISTING = """Source #45
\tState: RUNNING
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of Built-in Audio Analog Stereo
\tDriver: PipeWire
\tSample Specification: s32le 2ch 48000Hz
\tMonitor of Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
\tProperties:
\t\tdevice.class = "monitor"
\t\tnode.name = "not a field: ignored"

Source #46
\tState: SUSPENDED
\tName: alsa_input.pci-0000_00_1f.3.analog-stereo
\tDescription: Built-in Audio Analog Stereo
\tDriver: PipeWire
\tSample Specification: s32le 2ch 48000Hz
\tMonitor of Sink: n/a
\tProperties:
\t\talsa.card = "0"

Source #171
\tState: SUSPENDED
\tName: bluez_input.AC_80_0A_11_22_33.0
\tDescription: WH-1000XM4
\tDriver: PipeWire
\tSample Specification: float32le 1ch 48000Hz
\tMonitor of Sink: n/a
"""
PULSE_LISTING = PIPEWIRE_LISTING.replace("Driver: PipeWire", "Driver: module-alsa-card.c")


def linux_sd(names=("HDA Intel PCH: ALC257 Analog (hw:0,0)", "pipewire", "pulse", "default"), **kwargs):
    devices = [_dev(name, 0, 2 if "hw:" in name else 32) for name in names]
    hostapis = [{"name": "ALSA", "default_input_device": len(devices) - 1}]
    return FakeSd(devices, hostapis, default=len(devices) - 1, **kwargs)


@pytest.fixture
def sources(monkeypatch):
    """Stand in for `pactl`; returns the listing holder to change mid-test."""
    holder = SimpleNamespace(listing=PIPEWIRE_LISTING, reads=0)

    def run(command, **kwargs):
        holder.reads += 1
        assert command == ["pactl", "list", "sources"]
        assert kwargs["env"]["LC_ALL"] == "C"
        return SimpleNamespace(stdout=holder.listing)

    monkeypatch.setattr(audio_devices.sys, "platform", "linux")
    monkeypatch.setattr(audio_devices.subprocess, "run", run)
    monkeypatch.setattr(audio_devices, "_source_cache", (0.0, []))
    for name in ROUTE:
        monkeypatch.delenv(name, raising=False)
    return holder


def _sources(devices):
    return [device for device in devices if device.source]


def test_sources_are_listed_first_without_output_monitors(sources):
    devices = list_input_devices(linux_sd())

    assert [(d.name, d.source) for d in devices[:2]] == [
        ("Built-in Audio Analog Stereo", "alsa_input.pci-0000_00_1f.3.analog-stereo"),
        ("WH-1000XM4", "bluez_input.AC_80_0A_11_22_33.0"),
    ]
    assert [d.name for d in devices[2:]] == [
        "HDA Intel PCH: ALC257 Analog (hw:0,0)", "pipewire", "pulse", "default",
    ]
    headset = devices[1]
    assert headset.key == HEADSET
    assert headset.default_api and headset.channels == 1
    # Not a PortAudio index, and stable while the source exists.
    assert headset.index == audio_devices._SOURCE_INDEX_BASE + 171
    assert headset.stream_index == headset.carrier == 1


@pytest.mark.parametrize("listing,names,carrier", [
    (PIPEWIRE_LISTING, ("pipewire", "pulse", "default"), "pipewire"),
    (PIPEWIRE_LISTING, ("pulse", "default"), "pulse"),
    (PULSE_LISTING, ("pipewire", "pulse", "default"), "pulse"),
    # ALSA's pipewire device does not reach a PulseAudio server's sources.
    (PULSE_LISTING, ("pipewire", "default"), None),
    (PIPEWIRE_LISTING, ("default",), None),
])
def test_the_carrier_is_the_alsa_device_that_reaches_the_sound_server(sources, listing, names, carrier):
    sources.listing = listing
    found = _sources(list_input_devices(linux_sd(names)))

    if carrier is None:
        assert found == []
    else:
        assert {device.carrier for device in found} == {names.index(carrier)}


def test_no_sources_without_a_sound_server_or_off_linux(sources, monkeypatch):
    sources.listing = ""
    assert _sources(list_input_devices(linux_sd())) == []

    sources.listing = PIPEWIRE_LISTING
    monkeypatch.setattr(audio_devices, "_source_cache", (0.0, []))
    monkeypatch.setattr(audio_devices.sys, "platform", "win32")
    assert _sources(list_input_devices(linux_sd())) == []


def test_a_missing_pactl_leaves_the_alsa_devices(sources, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("pactl")

    monkeypatch.setattr(audio_devices.subprocess, "run", missing)

    assert [d.name for d in list_input_devices(linux_sd(("pulse", "default")))] == ["pulse", "default"]


def test_the_listing_is_reused_briefly_and_reread_after_a_refresh(sources):
    sd = linux_sd()
    list_input_devices(sd)
    list_input_devices(sd)
    assert sources.reads == 1

    assert audio_devices.refresh_portaudio(sd)
    list_input_devices(sd)
    assert sources.reads == 2


def test_the_format_check_asks_the_carrier(sources):
    sd = linux_sd(refuse={0})

    ranked = ranked_candidates([HEADSET, LAPTOP], sd=sd)

    assert [device.name for device in ranked[:2]] == ["WH-1000XM4", "Built-in Audio Analog Stereo"]
    assert [device for device, _ in sd.checks[:2]] == [1, 1]


def test_the_route_is_set_only_while_the_stream_opens(sources, monkeypatch):
    monkeypatch.setenv("PULSE_SOURCE", "the-users-own")
    headset = InputDevice(index=5, name="WH-1000XM4", hostapi=SOURCE_HOSTAPI, source="bluez_input.X", carrier=1)

    with audio_devices.source_route(headset):
        assert [os.environ[name] for name in ROUTE] == ["bluez_input.X"] * 2
    assert os.environ["PULSE_SOURCE"] == "the-users-own"
    assert "PIPEWIRE_NODE" not in os.environ

    with audio_devices.source_route(InputDevice(index=0, name="pulse", hostapi="ALSA")):
        assert os.environ["PULSE_SOURCE"] == "the-users-own"
    with audio_devices.source_route(None):
        assert "PIPEWIRE_NODE" not in os.environ


@pytest.fixture
def recording_sd(sources, monkeypatch):
    sd = linux_sd()
    sd.InputStream = StreamFactory()
    sd.routes = []
    sd.InputStream.before_open = lambda device: sd.routes.append(("open", os.environ.get("PIPEWIRE_NODE")))
    sd.InputStream.on_start = lambda stream: sd.routes.append(("start", os.environ.get("PULSE_SOURCE")))
    monkeypatch.setattr(recorder_module, "sd", sd)
    return sd


@pytest.fixture
def make_recorder(tmp_path):
    made = []

    def build(priority):
        recorder = AudioRecorder(output_file=str(tmp_path / "dictation.wav"), device_priority=list(priority))
        made.append(recorder)
        return recorder

    yield build
    for recorder in made:
        recorder.cleanup()
    assert audio_devices.open_stream_count() == 0


def test_dictation_opens_the_carrier_routed_to_the_headset(recording_sd, make_recorder):
    recorder = make_recorder([HEADSET])

    assert recorder.start_recording()

    assert [stream.device for stream in recording_sd.InputStream.opened] == [1]
    source = "bluez_input.AC_80_0A_11_22_33.0"
    assert recording_sd.routes == [("open", source), ("start", source)]
    assert not any(name in os.environ for name in ROUTE)
    assert recorder.active_device.key == HEADSET
    assert recorder.start_fallback is None


def test_a_recording_without_the_preferred_microphone_says_so_once(recording_sd, sources, make_recorder):
    recorder = make_recorder([HEADSET, LAPTOP])
    sources.listing = PIPEWIRE_LISTING.split("Source #171")[0]

    assert recorder.start_recording()
    assert recorder.active_device.key == LAPTOP
    assert recorder.start_fallback == ("WH-1000XM4", "Built-in Audio Analog Stereo")
    recorder.cancel_recording()

    assert recorder.start_recording()
    assert recorder.start_fallback is None
    recorder.cancel_recording()

    # Back on the headset, then lost again: news once more.
    sources.listing = PIPEWIRE_LISTING
    audio_devices._source_cache = (0.0, [])
    assert recorder.start_recording()
    assert recorder.active_device.key == HEADSET and recorder.start_fallback is None
    recorder.cancel_recording()
    sources.listing = PIPEWIRE_LISTING.split("Source #171")[0]
    audio_devices._source_cache = (0.0, [])
    assert recorder.start_recording()
    assert recorder.start_fallback == ("WH-1000XM4", "Built-in Audio Analog Stereo")


def test_falling_back_to_the_system_default_names_no_device(recording_sd, make_recorder):
    recorder = make_recorder([{"name": "Unplugged", "hostapi": SOURCE_HOSTAPI}])

    assert recorder.start_recording()

    assert recorder.active_device.role == audio_devices.ROLE_DEFAULT
    assert recorder.start_fallback == ("Unplugged", "")


def test_a_meeting_opens_the_carrier_routed_to_the_headset(sources, monkeypatch):
    sd = linux_sd()
    opened = []

    class Stream:
        def __init__(self, **kwargs):
            opened.append((kwargs["device"], kwargs["channels"], os.environ.get("PIPEWIRE_NODE")))

        def start(self):
            opened.append(("start", os.environ.get("PULSE_SOURCE")))

    sd.InputStream = Stream
    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    device = find_mic_device([HEADSET])
    assert device["index"] == audio_devices._SOURCE_INDEX_BASE + 171
    source = sd_stream.SdCaptureSource(
        "mic", device["index"], device["samplerate"], device["channels"],
        open_index=device["open_index"], source=device["source"],
    )
    source.start(lambda block: None)

    name = "bluez_input.AC_80_0A_11_22_33.0"
    assert opened == [(1, 1, name), ("start", name)]
    assert source.device_id == device["index"]
    assert not any(route in os.environ for route in ROUTE)
    assert not audio_devices.portaudio_lock._is_owned()


def test_settings_offers_the_headset_and_saves_it_by_name(sources, make_dialog):
    make_dialog.devices[:] = list_input_devices(linux_sd())
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_DEVICE: 0})
    combo = dialog.audio_device_combo

    assert [combo.itemText(i) for i in range(combo.count())] == [
        "System default", "Built-in Audio Analog Stereo", "WH-1000XM4",
        "HDA Intel PCH: ALC257 Analog", "pipewire", "pulse", "default",
    ]
    combo.setCurrentIndex(2)
    dialog._on_audio_device_changed()

    saved = store.load_all_settings()
    assert saved[SettingsKey.AUDIO_INPUT_PRIORITY] == [HEADSET]
    # Its index is no PortAudio index: an older version must not read it.
    assert SettingsKey.AUDIO_INPUT_DEVICE not in saved
