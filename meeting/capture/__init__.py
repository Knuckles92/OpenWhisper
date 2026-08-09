"""Audio capture for Meeting Mode: device discovery, stream sources, spooling.

Two capture streams (``mic`` and WASAPI ``loopback``) deliver
``CaptureBlock`` values to a per-channel ``SpoolWriter`` that resamples to
16 kHz mono int16, gap-fills silence, cuts chunks at quiet points, and
registers durable WAV files for the ASR engine.
"""
from meeting.capture.devices import find_loopback_device, find_mic_device
from meeting.capture.sd_stream import SdCaptureSource
from meeting.capture.soundcard_stream import SoundcardLoopbackSource
from meeting.capture.spool import SpoolWriter

__all__ = [
    "find_loopback_device",
    "find_mic_device",
    "SdCaptureSource",
    "SoundcardLoopbackSource",
    "SpoolWriter",
]
