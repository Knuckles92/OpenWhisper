"""Audio capture for Meeting Mode: device discovery, stream sources, spooling.

Two capture streams (``mic`` and ``loopback``) deliver ``CaptureBlock`` values
to a per-channel ``SpoolWriter`` that resamples to 16 kHz mono int16,
gap-fills silence, cuts chunks at quiet points, and registers durable WAV
files for the ASR engine.

The ``mic`` channel is PortAudio everywhere. The ``loopback`` channel has no
portable backend, so it is whichever of three sources this OS supports: a
WASAPI ``[Loopback]`` input device or the ``soundcard`` library on Windows,
ScreenCaptureKit on macOS.
"""
from meeting.capture.devices import find_loopback_device, find_mic_device
from meeting.capture.sck_stream import ScreenCaptureKitLoopbackSource
from meeting.capture.sd_stream import SdCaptureSource
from meeting.capture.soundcard_stream import SoundcardLoopbackSource
from meeting.capture.spool import SpoolWriter

__all__ = [
    "find_loopback_device",
    "find_mic_device",
    "ScreenCaptureKitLoopbackSource",
    "SdCaptureSource",
    "SoundcardLoopbackSource",
    "SpoolWriter",
]
