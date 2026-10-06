"""Microphone identity and ranking, shared by dictation and Meeting Mode.

PortAudio enumerates devices once, when it initializes, so the device list
is a snapshot: a microphone plugged in later is invisible and an unplugged
one keeps enumerating until ``refresh_portaudio``. Indexes also shift between
launches, so a saved choice is a ``{name, hostapi}`` key matched back to the
current list, never an index.
"""
from __future__ import annotations

import dataclasses
import logging
import operator
import re
import sys
import threading
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

LOOPBACK_MARKER = "[Loopback]"
SOUND_MAPPER_NAME = "Microsoft Sound Mapper - Input"
# MME copies device names into a 32-byte buffer, so longer names arrive cut
# to 31 characters ("Microphone (2- USB Audio Device").
MME_NAME_LIMIT = 31
# Windows' own aliases for "whatever the default input is"; Settings shows
# them as the fixed System default row instead of as microphones.
_SYSTEM_ALIASES = frozenset({SOUND_MAPPER_NAME.lower(), "primary sound capture driver"})
# Windows numbers same-named endpoints "2- USB Audio Device", and the number
# can change on replug or another USB port.
_DUPLICATE_PREFIX = re.compile(r"(^|\()\d+- ")
_ALSA_HW_SUFFIX = re.compile(r"\s*\(hw:\d+,\d+\)\s*$")
_GENERIC_OUTER = frozenset({
    "microphone", "microphone array", "mic", "headset", "headset microphone",
    "external microphone", "internal microphone", "line", "line in",
})

ROLE_DEFAULT = "default"
ROLE_MAPPER = "mapper"

# Serializes device queries, stream opens and the PortAudio re-initialization
# in refresh_portaudio: re-initializing invalidates every device index and
# every open stream in the process.
portaudio_lock = threading.RLock()
_open_streams: dict = {}


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    channels: int = 1
    samplerate: int = 44100
    # The untruncated name when MME cut it and another host API has it whole.
    label: str = ""
    # On the platform's default host API (MME, Core Audio, ALSA).
    default_api: bool = False
    # ROLE_DEFAULT or ROLE_MAPPER when a candidate stands in for the system
    # default rather than a ranked choice.
    role: str = ""

    @property
    def key(self) -> dict:
        return {"name": self.name, "hostapi": self.hostapi}

    @property
    def display(self) -> str:
        return display_name(self.label or self.name)


def _sounddevice():
    import sounddevice

    return sounddevice


def _as_device_index(value: Any) -> Optional[int]:
    """Coerce a sounddevice device selector to a non-negative index."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _is_usable_input(dev: Mapping[str, Any]) -> bool:
    """True when ``dev`` can back a microphone (WASAPI loopback cannot)."""
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


def _default_hostapi_index(sd) -> int:
    try:
        index = getattr(sd.default, "hostapi", None)
        return 0 if index is None else int(index)
    except Exception:
        return 0


def _key_tuple(entry: Mapping[str, Any]) -> tuple[str, str]:
    return str(entry.get("hostapi") or ""), str(entry.get("name") or "")


def device_key(device: InputDevice | Mapping[str, Any]) -> dict:
    """The saved identity of a device: its name and host API name."""
    if isinstance(device, InputDevice):
        return device.key
    hostapi, name = _key_tuple(device)
    return {"name": name, "hostapi": hostapi}


def normalize_priority(value: Any) -> List[dict]:
    """A clean ``audio_input_priority`` list: ``{name, hostapi}`` dicts, no repeats."""
    result: List[dict] = []
    seen = set()
    for entry in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(entry, Mapping):
            continue
        name, hostapi = entry.get("name"), entry.get("hostapi", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(hostapi, str):
            continue
        key = (hostapi, name)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "hostapi": hostapi})
    return result


def list_input_devices(sd=None) -> List[InputDevice]:
    """Every input PortAudio knows about, except WASAPI loopback devices."""
    sd = sd or _sounddevice()
    with portaudio_lock:
        raw = list(sd.query_devices())
        apis = list(sd.query_hostapis())
        default_api = _default_hostapi_index(sd)
    devices = []
    for index, dev in enumerate(raw):
        if not _is_usable_input(dev):
            continue
        api_index = int(dev.get("hostapi", -1))
        api_name = str(apis[api_index].get("name", "")) if 0 <= api_index < len(apis) else ""
        devices.append(InputDevice(
            index=index,
            name=str(dev.get("name") or ""),
            hostapi=api_name,
            channels=int(dev.get("max_input_channels") or 1),
            samplerate=int(dev.get("default_samplerate") or 44100),
            default_api=api_index == default_api,
        ))
    return [
        dataclasses.replace(device, label=_untruncated(device, devices))
        if len(device.name) == MME_NAME_LIMIT else device
        for device in devices
    ]


def _untruncated(device: InputDevice, devices: Sequence[InputDevice]) -> str:
    for other in devices:
        if len(other.name) > len(device.name) and other.name.startswith(device.name):
            return other.name
    return ""


def is_system_alias(device: InputDevice) -> bool:
    return device.name.lower() in _SYSTEM_ALIASES


def _strip_duplicate_prefix(name: str) -> str:
    return _DUPLICATE_PREFIX.sub(r"\1", name).strip()


def _strip_hw_suffix(name: str) -> str:
    return _ALSA_HW_SUFFIX.sub("", name).strip()


def _same_without_duplicate_prefix(a: str, b: str) -> bool:
    return _strip_duplicate_prefix(a) == _strip_duplicate_prefix(b)


def _same_but_truncated(a: str, b: str) -> bool:
    """One name is MME's 31-character cut of the other (numbering aside)."""
    for cut, whole in ((a, b), (b, a)):
        if len(cut) != MME_NAME_LIMIT or cut == whole:
            continue
        short = _strip_duplicate_prefix(cut)
        if short and _strip_duplicate_prefix(whole).startswith(short):
            return True
    return False


def _same_without_hw_suffix(a: str, b: str) -> bool:
    return _strip_duplicate_prefix(_strip_hw_suffix(a)) == _strip_duplicate_prefix(_strip_hw_suffix(b))


_LADDER = (
    operator.eq,
    _same_without_duplicate_prefix,
    _same_but_truncated,
    _same_without_hw_suffix,
)


def match_entry(entry: Mapping[str, Any], devices: Sequence[InputDevice]) -> Optional[InputDevice]:
    """The current device a saved entry names, or None.

    Conservative and deterministic: the entry's own host API goes through
    every rung of the ladder first, then the platform's default host API,
    then the rest; within a rung the lowest index wins. The default API comes
    before an exact name elsewhere because it opens every microphone at the
    dictation format without host-API quirks.
    """
    hostapi, name = _key_tuple(entry)
    if not name:
        return None
    groups = (
        [device for device in devices if device.hostapi == hostapi],
        [device for device in devices if device.hostapi != hostapi and device.default_api],
        [device for device in devices if device.hostapi != hostapi and not device.default_api],
    )
    for group in groups:
        for rung in _LADDER:
            for device in group:
                if rung(name, device.name):
                    return device
    return None


def extra_settings(device: Optional[InputDevice], sd=None):
    """WASAPI refuses a rate other than its mix format unless asked to convert."""
    if device is None or "wasapi" not in device.hostapi.lower():
        return None
    sd = sd or _sounddevice()
    settings = getattr(sd, "WasapiSettings", None)
    return settings(auto_convert=True) if settings is not None else None


def can_open(
    device: InputDevice,
    *,
    sd=None,
    samplerate: int = 44100,
    channels: int = 1,
    dtype: str = "int16",
) -> bool:
    """Whether PortAudio accepts this format on ``device``, without opening it."""
    sd = sd or _sounddevice()
    try:
        with portaudio_lock:
            sd.check_input_settings(
                device=device.index,
                channels=channels,
                dtype=dtype,
                samplerate=samplerate,
                extra_settings=extra_settings(device, sd),
            )
        return True
    except Exception:
        return False


def default_input(sd=None, devices: Optional[Sequence[InputDevice]] = None) -> Optional[InputDevice]:
    """PortAudio's default input, as of its last initialization."""
    sd = sd or _sounddevice()
    if devices is None:
        devices = list_input_devices(sd)
    by_index = {device.index: device for device in devices}
    for index in (_default_io_indexes(sd)[0], _hostapi_default_input_index(sd)):
        if index is not None and index in by_index:
            return by_index[index]
    return None


def ranked_candidates(
    priority: Iterable[Mapping[str, Any]],
    *,
    exclude: Iterable[Mapping[str, Any]] = (),
    sd=None,
    devices: Optional[Sequence[InputDevice]] = None,
    check: bool = True,
    samplerate: int = 44100,
    channels: int = 1,
    dtype: str = "int16",
) -> List[InputDevice]:
    """Devices to try, best first.

    The ranked entries that match a current device come first, in order, then
    PortAudio's default input. On Windows the MME Sound Mapper comes last: it
    follows the default the OS has now, while PortAudio's default was fixed
    when PortAudio started. With ``check`` each one must accept the format
    (dictation's 44.1 kHz mono int16); Meeting Mode opens native rates and
    passes False.
    """
    sd = sd or _sounddevice()
    if devices is None:
        devices = list_input_devices(sd)
    excluded = {_key_tuple(entry) for entry in exclude}
    ranked: List[InputDevice] = []
    seen = set()

    def consider(device: Optional[InputDevice], role: str = "") -> None:
        if device is None or device.index in seen or _key_tuple(device.key) in excluded:
            return
        seen.add(device.index)
        if check and not can_open(device, sd=sd, samplerate=samplerate, channels=channels, dtype=dtype):
            return
        ranked.append(dataclasses.replace(device, role=role) if role else device)

    for entry in normalize_priority(list(priority or ())):
        if _key_tuple(entry) not in excluded:
            consider(match_entry(entry, devices))
    consider(default_input(sd, devices), ROLE_DEFAULT)
    if sys.platform == "win32":
        consider(sound_mapper(devices), ROLE_MAPPER)
    return ranked


def sound_mapper(devices: Sequence[InputDevice]) -> Optional[InputDevice]:
    return next(
        (device for device in devices if device.hostapi == "MME" and device.name == SOUND_MAPPER_NAME),
        None,
    )


def load_priority(settings: Optional[Mapping[str, Any]] = None, *, manager=None, sd=None) -> List[dict]:
    """The saved microphone order, migrating the legacy index once.

    Before ``audio_input_priority`` existed the choice was a raw PortAudio
    index under ``audio_input_device``. The first load turns that index into
    the first entry, best effort (the index may already point elsewhere), and
    keeps the old key so an older version still finds it after a downgrade.
    """
    from services.settings import SettingsKey

    if manager is None:
        from services.settings import settings_manager as manager
    if settings is None:
        settings = manager.load_all_settings()
    if SettingsKey.AUDIO_INPUT_PRIORITY in settings:
        return normalize_priority(settings[SettingsKey.AUDIO_INPUT_PRIORITY])
    legacy = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
    if not isinstance(legacy, int) or isinstance(legacy, bool):
        return []
    try:
        device = next((d for d in list_input_devices(sd) if d.index == legacy), None)
    except Exception:
        # PortAudio unavailable right now; migrate on a later load instead
        # of recording "no microphone" for good.
        logger.warning("Couldn't read audio inputs to migrate the saved microphone", exc_info=True)
        return []
    migrated = [device.key] if device is not None else []

    def migrate(current: dict) -> List[dict]:
        if SettingsKey.AUDIO_INPUT_PRIORITY in current:
            return normalize_priority(current[SettingsKey.AUDIO_INPUT_PRIORITY])
        current[SettingsKey.AUDIO_INPUT_PRIORITY] = migrated
        return migrated

    try:
        result = manager.mutate_settings(migrate)
    except Exception:
        logger.warning("Couldn't save the migrated microphone choice", exc_info=True)
        return migrated
    logger.info("Migrated the saved microphone index to %d ranked entr%s",
                len(result), "y" if len(result) == 1 else "ies")
    return result


def register_stream(stream: Any) -> None:
    """Count an open PortAudio stream; refresh_portaudio waits for zero."""
    with portaudio_lock:
        _open_streams[id(stream)] = stream


def unregister_stream(stream: Any) -> None:
    with portaudio_lock:
        _open_streams.pop(id(stream), None)


def open_stream_count() -> int:
    with portaudio_lock:
        return len(_open_streams)


def refresh_portaudio(sd=None) -> bool:
    """Re-read the device list so newly plugged microphones appear.

    Only while no stream is open: re-initializing PortAudio invalidates every
    open stream and device index in the process. Never call it on the way to
    opening a stream, which would put about 25 ms (more with Bluetooth
    devices) on the recording start path. Returns False when it was refused.
    """
    sd = sd or _sounddevice()
    with portaudio_lock:
        if _open_streams:
            return False
        sd._terminate()
        try:
            sd._initialize()
        except Exception:
            # Leaving PortAudio terminated would fail every later recording.
            logger.exception("PortAudio didn't restart after a device refresh; retrying")
            sd._initialize()
    logger.info("Re-read the audio device list")
    return True


def display_name(name: str) -> str:
    """A device name as people should read it.

    Drops Windows' duplicate numbering and ALSA's ``(hw:X,Y)`` suffix, and
    closes a parenthesis MME cut off.
    """
    shown = _strip_hw_suffix(_strip_duplicate_prefix(name or ""))
    if shown.count("(") > shown.count(")"):
        shown = shown.rstrip() + ")"
    return re.sub(r"\s+\)", ")", shown)


def short_name(name: str) -> str:
    """The distinctive part: "Microphone (Blue Snowball)" becomes "Blue Snowball"."""
    shown = display_name(name)
    match = re.fullmatch(r"(.*?)\s*\((.+)\)", shown)
    if match and match.group(1).strip().lower() in _GENERIC_OUTER:
        return match.group(2).strip()
    return shown


def hostapi_label(hostapi: str) -> str:
    """"Windows DirectSound" reads as "DirectSound"."""
    return hostapi[len("Windows "):] if hostapi.startswith("Windows ") else hostapi
