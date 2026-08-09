"""WASAPI capture-device discovery for Meeting Mode.

sounddevice >= 0.5.0 bundles a PortAudio build that exposes WASAPI loopback
capture as additional *input* devices whose names contain ``[Loopback]``.
This module locates the loopback device mirroring the default render device
(system audio, the "Others" channel) and the microphone (the "Me" channel).

All functions degrade gracefully: any sounddevice import or query failure
returns ``None`` so the engine can fall back (e.g. to the ``soundcard``
backend) instead of crashing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LOOPBACK_MARKER = "[Loopback]"


def _sounddevice():
    """Import sounddevice lazily; return the module or None on failure."""
    try:
        import sounddevice as sd
        return sd
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("sounddevice unavailable: %s", exc)
        return None


def _device_dict(index: int, dev: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": index,
        "name": dev["name"],
        "samplerate": int(dev["default_samplerate"]),
        "channels": int(dev["max_input_channels"]),
    }


def _wasapi_hostapi_index(sd) -> Optional[int]:
    for i, hostapi in enumerate(sd.query_hostapis()):
        if "wasapi" in str(hostapi.get("name", "")).lower():
            return i
    return None


def find_loopback_device() -> Optional[Dict[str, Any]]:
    """Find the WASAPI loopback input device for the default render device.

    Enumerates WASAPI input devices whose names contain ``[Loopback]`` and
    prefers the one mirroring the default output (render) device; falls back
    to the first loopback device found.

    Returns:
        ``{'index': int, 'name': str, 'samplerate': int, 'channels': int}``
        or None when sounddevice is unavailable, WASAPI is absent, or no
        loopback input device exists.
    """
    sd = _sounddevice()
    if sd is None:
        return None
    try:
        wasapi = _wasapi_hostapi_index(sd)
        if wasapi is None:
            logger.warning("No WASAPI host API found; loopback capture unavailable")
            return None
        devices = list(sd.query_devices())
        candidates = [
            (i, dev) for i, dev in enumerate(devices)
            if dev["hostapi"] == wasapi
            and dev["max_input_channels"] > 0
            and LOOPBACK_MARKER in dev["name"]
        ]
        if not candidates:
            logger.warning("No '%s' WASAPI input devices found", LOOPBACK_MARKER)
            return None

        default_output_name = _default_output_name(sd, devices, wasapi)
        if default_output_name:
            for i, dev in candidates:
                if default_output_name in dev["name"]:
                    logger.info("Loopback device matches default render device: %s",
                                dev["name"])
                    return _device_dict(i, dev)
            logger.info(
                "No loopback device matches default render device '%s'; "
                "using first loopback device", default_output_name,
            )
        i, dev = candidates[0]
        return _device_dict(i, dev)
    except Exception:
        logger.exception("Loopback device discovery failed")
        return None


def _default_output_name(sd, devices, wasapi_index: int) -> Optional[str]:
    """Name of the default render device (WASAPI-preferred), or None."""
    try:
        hostapi = sd.query_hostapis(wasapi_index)
        out_index = hostapi.get("default_output_device", -1)
        if not isinstance(out_index, int) or out_index < 0:
            default = sd.default.device
            out_index = default[1] if isinstance(default, (tuple, list)) else -1
        if isinstance(out_index, int) and 0 <= out_index < len(devices):
            return str(devices[out_index]["name"])
    except Exception:
        logger.debug("Could not resolve default output device", exc_info=True)
    return None


def find_mic_device(preferred_index: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Find the microphone input device for the ``mic`` channel.

    Args:
        preferred_index: A sounddevice device index chosen by the user; used
            when it still names a valid input device, otherwise ignored with
            a warning.

    Returns:
        ``{'index': int, 'name': str, 'samplerate': int, 'channels': int}``
        or None when sounddevice is unavailable or no input device exists.
    """
    sd = _sounddevice()
    if sd is None:
        return None
    try:
        devices = list(sd.query_devices())

        if preferred_index is not None:
            if (0 <= preferred_index < len(devices)
                    and devices[preferred_index]["max_input_channels"] > 0):
                return _device_dict(preferred_index, devices[preferred_index])
            logger.warning(
                "Preferred mic device index %s is not a valid input device; "
                "falling back to default", preferred_index,
            )

        try:
            default = sd.default.device
            in_index = default[0] if isinstance(default, (tuple, list)) else -1
        except Exception:
            in_index = -1
        if (isinstance(in_index, int) and 0 <= in_index < len(devices)
                and devices[in_index]["max_input_channels"] > 0
                and LOOPBACK_MARKER not in devices[in_index]["name"]):
            return _device_dict(in_index, devices[in_index])

        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and LOOPBACK_MARKER not in dev["name"]:
                return _device_dict(i, dev)
        logger.warning("No microphone input device found")
        return None
    except Exception:
        logger.exception("Microphone device discovery failed")
        return None
