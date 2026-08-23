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
import sys
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


def _as_device_index(value: Any) -> Optional[int]:
    """Coerce a sounddevice device selector to a non-negative index."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _is_usable_input(dev: Dict[str, Any]) -> bool:
    """True when ``dev`` can back the microphone channel."""
    return (
        int(dev.get("max_input_channels") or 0) > 0
        and LOOPBACK_MARKER not in str(dev.get("name") or "")
    )


def _default_io_indexes(sd) -> tuple[Optional[int], Optional[int]]:
    """Return ``(input_index, output_index)`` from ``sd.default.device``.

    sounddevice exposes this as ``_InputOutputPair``. It is indexable but is
    neither a list nor a tuple, so ``isinstance(..., (tuple, list))`` misses
    the real default and Meeting Mode used to fall through to device 0.
    """
    try:
        default = sd.default.device
        return _as_device_index(default[0]), _as_device_index(default[1])
    except Exception:
        return None, None


def _hostapi_default_input_index(sd) -> Optional[int]:
    """Default input index advertised by the current host API, if any."""
    try:
        host_index = getattr(sd.default, "hostapi", None)
        if host_index is None:
            hostapis = list(sd.query_hostapis())
            host = hostapis[0] if hostapis else None
        else:
            host = sd.query_hostapis(int(host_index))
        if host is None:
            return None
        return _as_device_index(host.get("default_input_device", -1))
    except Exception:
        return None


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
            # Expected on macOS/Linux; the watchdog polls this every few
            # seconds, so a warning here drowns the log on every meeting.
            log = (logger.warning if sys.platform.startswith("win")
                   else logger.debug)
            log("No WASAPI host API found; loopback capture unavailable")
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
        out_index = _as_device_index(hostapi.get("default_output_device", -1))
        if out_index is None:
            _, out_index = _default_io_indexes(sd)
        if out_index is not None and out_index < len(devices):
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

        def _choose(index: int) -> Optional[Dict[str, Any]]:
            if not (0 <= index < len(devices) and _is_usable_input(devices[index])):
                return None
            chosen = _device_dict(index, devices[index])
            logger.info(
                "Microphone device: %s (index %d, %d Hz, %d ch)",
                chosen["name"], chosen["index"],
                chosen["samplerate"], chosen["channels"],
            )
            return chosen

        if preferred_index is not None:
            chosen = _choose(preferred_index)
            if chosen is not None:
                return chosen
            logger.warning(
                "Preferred mic device index %s is not a valid input device; "
                "falling back to default", preferred_index,
            )

        default_in, _ = _default_io_indexes(sd)
        hostapi_in = _hostapi_default_input_index(sd)
        for in_index in (default_in, hostapi_in):
            if in_index is None:
                continue
            chosen = _choose(in_index)
            if chosen is not None:
                return chosen

        for i, _dev in enumerate(devices):
            chosen = _choose(i)
            if chosen is not None:
                return chosen
        logger.warning("No microphone input device found")
        return None
    except Exception:
        logger.exception("Microphone device discovery failed")
        return None
