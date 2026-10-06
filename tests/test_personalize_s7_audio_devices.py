"""Microphone identity, ranking and migration against a fake sounddevice."""
import time
from types import SimpleNamespace

import pytest

from services import audio_devices
from services.audio_devices import (
    InputDevice,
    display_name,
    list_input_devices,
    load_priority,
    match_entry,
    normalize_priority,
    ranked_candidates,
    short_name,
)
from services.settings import SettingsKey, SettingsManager
from tests.test_meeting_engine import (  # noqa: F401  (fixtures)
    fakes,
    make_engine,
)

MME, DSOUND, WASAPI = "MME", "Windows DirectSound", "Windows WASAPI"


class FakeSd:
    """Enough of sounddevice for discovery: queries and format checks only."""

    class WasapiSettings:
        def __init__(self, auto_convert=False):
            self.auto_convert = auto_convert

    def __init__(self, devices, hostapis, *, default=1, default_api=0, refuse=()):
        self.devices = devices
        self.hostapis = hostapis
        self.default = SimpleNamespace(device=(default, None), hostapi=default_api)
        self.refuse = set(refuse)
        self.checks = []
        self.lifecycle = []

    def query_devices(self):
        return list(self.devices)

    def query_hostapis(self, index=None):
        return list(self.hostapis) if index is None else self.hostapis[index]

    def check_input_settings(self, device=None, channels=None, dtype=None,
                             extra_settings=None, samplerate=None):
        self.checks.append((device, extra_settings))
        api = self.hostapis[self.devices[device]["hostapi"]]["name"]
        converts = getattr(extra_settings, "auto_convert", False)
        if device in self.refuse or (api == WASAPI and not converts):
            raise Exception("Invalid sample rate [-9997]")

    def _terminate(self):
        self.lifecycle.append("terminate")

    def _initialize(self):
        self.lifecycle.append("initialize")


def _dev(name, hostapi, channels=1, rate=44100):
    return {"name": name, "hostapi": hostapi, "max_input_channels": channels,
            "max_output_channels": 0, "default_samplerate": rate}


def windows_sd(**kwargs):
    """This machine's layout, measured with sounddevice 0.5.6 (trimmed)."""
    devices = [
        _dev("Microsoft Sound Mapper - Input", 0, 2),
        _dev("Microphone (Blue Snowball )", 0),
        _dev("Microphone (2- USB Audio Device", 0),
        _dev("Primary Sound Capture Driver", 1, 2),
        _dev("Microphone (2- USB Audio Device)", 1),
        _dev("Microphone (2- USB Audio Device)", 2, 2, 48000),
        _dev("Speakers (Realtek) [Loopback]", 2, 2, 48000),
        _dev("Headset (soundcore V20i)", 1, 1, 8000),
    ]
    hostapis = [
        {"name": MME, "default_input_device": 1},
        {"name": DSOUND, "default_input_device": 3},
        {"name": WASAPI, "default_input_device": 5},
    ]
    return FakeSd(devices, hostapis, **kwargs)


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(audio_devices.sys, "platform", "win32")


def test_listing_skips_loopback_and_restores_names_mme_cut():
    devices = list_input_devices(windows_sd())

    assert [device.index for device in devices] == [0, 1, 2, 3, 4, 5, 7]
    usb = devices[2]
    assert usb.key == {"name": "Microphone (2- USB Audio Device", "hostapi": MME}
    assert usb.label == "Microphone (2- USB Audio Device)"
    assert usb.display == "Microphone (USB Audio Device)"
    assert usb.default_api and not devices[4].default_api


@pytest.mark.parametrize("saved,expected", [
    ({"name": "Microphone (Blue Snowball )", "hostapi": MME}, 1),
    # Replugged into another port: Windows renumbered the duplicate name.
    ({"name": "Microphone (3- USB Audio Device)", "hostapi": WASAPI}, 5),
    # MME's 31-character cut of a name that has since lost its number.
    ({"name": "Microphone (4- USB Audio Device", "hostapi": MME}, 2),
    # Saved on a host API this machine lacks: the default API's cut name
    # wins over the exact name on DirectSound and WASAPI.
    ({"name": "Microphone (2- USB Audio Device)", "hostapi": "Windows WDM-KS"}, 2),
])
def test_matching_ladder(saved, expected):
    devices = list_input_devices(windows_sd())
    assert match_entry(saved, devices).index == expected


def test_matching_strips_alsa_hardware_numbers():
    sd = FakeSd([_dev("default", 0, 2), _dev("USB PnP Sound Device: Audio (hw:3,0)", 0)],
                [{"name": "ALSA", "default_input_device": 0}], default=0)
    devices = list_input_devices(sd)
    saved = {"name": "USB PnP Sound Device: Audio (hw:2,0)", "hostapi": "ALSA"}
    assert match_entry(saved, devices).index == 1


@pytest.mark.parametrize("name", [
    "Microphone (Webcam)",
    "Microphone (USB",  # a prefix, but not MME's 31-character cut
    "",
])
def test_matching_is_conservative(name):
    devices = list_input_devices(windows_sd())
    assert match_entry({"name": name, "hostapi": MME}, devices) is None


def test_candidates_follow_the_order_then_default_then_sound_mapper(on_windows):
    sd = windows_sd()
    priority = [
        {"name": "Microphone (2- USB Audio Device)", "hostapi": WASAPI},
        {"name": "Microphone (Gone)", "hostapi": MME},
        {"name": "Microphone (Blue Snowball )", "hostapi": MME},
    ]

    ranked = ranked_candidates(priority, sd=sd)

    assert [(device.index, device.role) for device in ranked] == [
        (5, ""), (1, ""), (0, audio_devices.ROLE_MAPPER),
    ]
    wasapi_check = dict(sd.checks)[5]
    assert wasapi_check.auto_convert is True
    assert dict(sd.checks)[1] is None


def test_candidates_skip_devices_that_refuse_the_format_and_excluded_keys(on_windows):
    sd = windows_sd(refuse={7})
    priority = [
        {"name": "Headset (soundcore V20i)", "hostapi": DSOUND},
        {"name": "Microphone (2- USB Audio Device", "hostapi": MME},
    ]
    excluded = [{"name": "Microphone (2- USB Audio Device", "hostapi": MME}]

    ranked = ranked_candidates(priority, sd=sd, exclude=excluded)

    assert [(device.index, device.role) for device in ranked] == [
        (1, audio_devices.ROLE_DEFAULT), (0, audio_devices.ROLE_MAPPER),
    ]


def test_sound_mapper_is_windows_only(monkeypatch):
    monkeypatch.setattr(audio_devices.sys, "platform", "darwin")
    ranked = ranked_candidates([], sd=windows_sd())
    assert [device.index for device in ranked] == [1]


def test_meeting_lookup_skips_the_format_check_and_excluded_devices(on_windows, monkeypatch):
    from meeting.capture import devices as meeting_devices

    sd = windows_sd()
    monkeypatch.setattr(meeting_devices, "_sounddevice", lambda: sd)
    priority = [{"name": "Microphone (2- USB Audio Device)", "hostapi": WASAPI}]

    chosen = meeting_devices.find_mic_device(priority)
    assert chosen["index"] == 5 and chosen["samplerate"] == 48000
    dead = {"name": chosen["name"], "hostapi": chosen["hostapi"]}
    assert meeting_devices.find_mic_device(priority, exclude=[dead])["index"] == 1
    assert sd.checks == []


def test_legacy_index_migrates_once_and_keeps_the_old_key(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    store.save_all_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})

    first = load_priority(manager=store, sd=windows_sd())
    again = load_priority(manager=store, sd=FakeSd([], []))

    expected = [{"name": "Microphone (2- USB Audio Device", "hostapi": MME}]
    assert first == again == expected
    saved = store.load_all_settings()
    assert saved[SettingsKey.AUDIO_INPUT_PRIORITY] == expected
    assert saved[SettingsKey.AUDIO_INPUT_DEVICE] == 2


def test_a_stale_legacy_index_becomes_the_system_default(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    store.save_all_settings({SettingsKey.AUDIO_INPUT_DEVICE: 6})  # now a loopback device

    assert load_priority(manager=store, sd=windows_sd()) == []
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == []


def test_migration_waits_when_portaudio_is_unavailable(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    store.save_all_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})
    broken = SimpleNamespace(query_devices=lambda: (_ for _ in ()).throw(OSError("no PortAudio")))

    assert load_priority(manager=store, sd=broken) == []
    assert SettingsKey.AUDIO_INPUT_PRIORITY not in store.load_all_settings()


def test_saved_order_is_cleaned_without_writing(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    good = {"name": "USB mic", "hostapi": MME}
    store.save_all_settings({SettingsKey.AUDIO_INPUT_PRIORITY: [
        good, good, {"name": ""}, "junk", {"name": "Desk", "hostapi": 3},
    ], SettingsKey.AUDIO_INPUT_DEVICE: 2})
    stamp = (tmp_path / "settings.json").stat().st_mtime_ns

    assert load_priority(manager=store, sd=windows_sd()) == [good]
    assert (tmp_path / "settings.json").stat().st_mtime_ns == stamp
    assert normalize_priority(None) == []


def test_refresh_waits_for_every_stream_to_close():
    sd = windows_sd()
    stream = object()
    audio_devices.register_stream(stream)
    try:
        assert audio_devices.refresh_portaudio(sd) is False
        assert sd.lifecycle == []
    finally:
        audio_devices.unregister_stream(stream)
    assert audio_devices.open_stream_count() == 0
    assert audio_devices.refresh_portaudio(sd) is True
    assert sd.lifecycle == ["terminate", "initialize"]


@pytest.mark.parametrize("name,shown,short", [
    ("Microphone (2- USB Audio Device", "Microphone (USB Audio Device)", "USB Audio Device"),
    ("Microphone (Blue Snowball )", "Microphone (Blue Snowball)", "Blue Snowball"),
    ("Microphone Array (Realtek(R) Audio)", "Microphone Array (Realtek(R) Audio)", "Realtek(R) Audio"),
    ("MacBook Pro Microphone", "MacBook Pro Microphone", "MacBook Pro Microphone"),
    ("HDA Intel PCH: ALC892 Analog (hw:0,0)", "HDA Intel PCH: ALC892 Analog", "HDA Intel PCH: ALC892 Analog"),
])
def test_names_read_well(name, shown, short):
    assert display_name(name) == shown
    assert short_name(name) == short


def test_input_device_key_is_name_and_host_api_name():
    device = InputDevice(index=4, name="Desk", hostapi=MME)
    assert audio_devices.device_key(device) == {"name": "Desk", "hostapi": MME}
    assert audio_devices.device_key({"name": "Desk", "hostapi": MME, "index": 4}) == device.key


class TestMeetingFailover:
    def test_a_dead_microphone_is_excluded_before_reprobing(self, make_engine, fakes, monkeypatch):  # noqa: F811 (pytest fixtures)
        import meeting.engine as engine_module

        devices = {
            "usb": {"index": 2, "name": "USB mic", "hostapi": MME, "samplerate": 48000, "channels": 1},
            "laptop": {"index": 1, "name": "Laptop mic", "hostapi": MME, "samplerate": 44100, "channels": 1},
        }
        calls = []

        def find_mic_device(priority=None, exclude=()):
            calls.append((priority, list(exclude)))
            excluded = {entry["name"] for entry in exclude}
            for key in ("usb", "laptop"):
                if devices[key]["name"] not in excluded:
                    return dict(devices[key])
            return None

        fakes.modules["meeting.capture.devices"].find_mic_device = find_mic_device
        monkeypatch.setattr(engine_module, "CAPTURE_WATCHDOG_INTERVAL_S", 0.01)
        monkeypatch.setattr(engine_module, "CAPTURE_RETRY_INTERVAL_S", 0.0)
        priority = ({"name": "USB mic", "hostapi": MME},)
        engine = make_engine(cloud_enabled=False, mic_priority=priority)
        engine.start()
        original = engine._capture_source("mic")
        assert original.device_id == 2
        assert calls[0] == (list(priority), [])

        original.stopped = True  # unplugged: the stream went inactive
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and engine._capture_source("mic") in (None, original):
            time.sleep(0.01)

        replacement = engine._capture_source("mic")
        assert replacement is not original and replacement.device_id == 1
        assert engine._mic_excluded == [{"name": "USB mic", "hostapi": MME}]
        assert calls[-1] == (list(priority), [{"name": "USB mic", "hostapi": MME}])

    def test_a_microphone_that_will_not_open_falls_to_the_next(self, make_engine, fakes):  # noqa: F811 (pytest fixtures)
        sources = fakes.modules["meeting.capture.sd_stream"]
        real_source = sources.SdCaptureSource

        class RefusesUsb(real_source):
            def start(self, callback):
                if self.device_id == 2:
                    raise OSError("Device unavailable")
                super().start(callback)

        sources.SdCaptureSource = RefusesUsb
        ranked = [
            {"index": 2, "name": "USB mic", "hostapi": MME, "samplerate": 48000, "channels": 1},
            {"index": 1, "name": "Laptop mic", "hostapi": MME, "samplerate": 44100, "channels": 1},
        ]

        def find_mic_device(priority=None, exclude=()):
            names = {entry["name"] for entry in exclude}
            return next((dict(d) for d in ranked if d["name"] not in names), None)

        fakes.modules["meeting.capture.devices"].find_mic_device = find_mic_device
        engine = make_engine(cloud_enabled=False, mic_priority=({"name": "USB mic", "hostapi": MME},))
        engine.start()

        assert engine._capture_source("mic").device_id == 1
