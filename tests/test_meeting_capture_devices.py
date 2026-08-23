"""Meeting Mode capture-device discovery.

The microphone picker must honour sounddevice's default input even when
that object is the real ``_InputOutputPair`` (indexable, but not a list or
tuple). Treating only list/tuple as valid made every Mac meeting open
whatever sat at index 0 instead of the system default mic.
"""
from types import SimpleNamespace
from unittest.mock import patch

from meeting.capture.devices import (
    find_loopback_device,
    find_mic_device,
    _default_io_indexes,
    _default_output_name,
)


class _InputOutputPair:
    """Minimal stand-in for ``sounddevice._InputOutputPair``."""

    def __init__(self, inp, out):
        self._pair = (inp, out)

    def __getitem__(self, index):
        return self._pair[index]


def _mic(name, channels=1, hostapi=0, rate=48000):
    return {
        "name": name,
        "max_input_channels": channels,
        "max_output_channels": 0,
        "hostapi": hostapi,
        "default_samplerate": rate,
    }


def _fake_sd(devices, default=(1, 2), hostapi=0, hostapis=None):
    apis = list(hostapis) if hostapis is not None else [{
        "name": "Core Audio",
        "default_input_device": default[0],
        "default_output_device": default[1],
    }]

    def query_hostapis(index=None):
        if index is None:
            return list(apis)
        return apis[int(index)]

    return SimpleNamespace(
        default=SimpleNamespace(
            device=_InputOutputPair(*default),
            hostapi=hostapi,
        ),
        query_devices=lambda: list(devices),
        query_hostapis=query_hostapis,
    )


class TestFindMicDevice:
    def test_input_output_pair_uses_system_default_not_index_zero(self):
        devices = [
            _mic("Virus Probe Microphone"),
            _mic("MacBook Air Microphone"),
        ]
        with patch("meeting.capture.devices._sounddevice",
                   return_value=_fake_sd(devices, default=(1, 2))):
            chosen = find_mic_device()
        assert chosen is not None
        assert chosen["index"] == 1
        assert chosen["name"] == "MacBook Air Microphone"

    def test_list_default_device_still_works(self):
        devices = [_mic("Probe"), _mic("Built-in Microphone")]
        sd = _fake_sd(devices, default=(1, 2))
        sd.default.device = [1, 2]
        with patch("meeting.capture.devices._sounddevice", return_value=sd):
            chosen = find_mic_device()
        assert chosen["index"] == 1

    def test_tuple_default_device_still_works(self):
        devices = [_mic("Probe"), _mic("Built-in Microphone")]
        sd = _fake_sd(devices, default=(1, 2))
        sd.default.device = (1, 2)
        with patch("meeting.capture.devices._sounddevice", return_value=sd):
            chosen = find_mic_device()
        assert chosen["index"] == 1

    def test_preferred_index_wins_when_valid(self):
        devices = [_mic("USB Mic"), _mic("Built-in Microphone")]
        with patch("meeting.capture.devices._sounddevice",
                   return_value=_fake_sd(devices, default=(1, 2))):
            chosen = find_mic_device(0)
        assert chosen["index"] == 0
        assert chosen["name"] == "USB Mic"

    def test_invalid_preferred_falls_back_to_default(self):
        devices = [_mic("Probe"), _mic("Built-in Microphone")]
        with patch("meeting.capture.devices._sounddevice",
                   return_value=_fake_sd(devices, default=(1, 2))):
            chosen = find_mic_device(99)
        assert chosen["index"] == 1

    def test_hostapi_default_used_when_pair_unreadable(self):
        devices = [_mic("Probe"), _mic("Built-in Microphone")]
        sd = _fake_sd(devices, default=(1, 2))
        sd.default.device = object()
        with patch("meeting.capture.devices._sounddevice", return_value=sd):
            chosen = find_mic_device()
        assert chosen["index"] == 1

    def test_skips_loopback_named_inputs(self):
        devices = [
            {**_mic("Speakers [Loopback]"), "max_input_channels": 2},
            _mic("Built-in Microphone"),
        ]
        with patch("meeting.capture.devices._sounddevice",
                   return_value=_fake_sd(devices, default=(0, 2))):
            chosen = find_mic_device()
        assert chosen["index"] == 1


class TestDefaultDeviceHelpers:
    def test_pair_indexes_are_read_without_type_check(self):
        sd = SimpleNamespace(default=SimpleNamespace(device=_InputOutputPair(3, 4)))
        assert _default_io_indexes(sd) == (3, 4)

    def test_unreadable_pair_returns_nones(self):
        sd = SimpleNamespace(default=SimpleNamespace(device=object()))
        assert _default_io_indexes(sd) == (None, None)

    def test_default_output_name_accepts_pair(self):
        devices = [
            {"name": "Mic"},
            {"name": "Unused"},
            {"name": "Speakers"},
        ]
        sd = SimpleNamespace(
            default=SimpleNamespace(device=_InputOutputPair(0, 2)),
            query_hostapis=lambda index=None: {
                "default_output_device": -1,
            },
        )
        assert _default_output_name(sd, devices, 0) == "Speakers"


class TestFindLoopbackDevice:
    def test_missing_wasapi_is_not_an_error_off_windows(self):
        sd = _fake_sd([_mic("Mic")], hostapis=[{"name": "Core Audio"}])
        with patch("meeting.capture.devices._sounddevice", return_value=sd), \
                patch("meeting.capture.devices.sys.platform", "darwin"):
            assert find_loopback_device() is None
