"""Linux system-audio capability probe for Meeting Mode.

Resolves the default PulseAudio / PipeWire-Pulse monitor source through
SoundCard and classifies failures into stable remediation keys. Never raises
into callers; returns a frozen capability result usable by the engine, the
pre-start dialog, and diagnostic scripts.
"""
from __future__ import annotations

import logging
import platform as platform_module
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

REASON_READY = "ready"
REASON_SOUNDCARD_MISSING = "soundcard_missing"
REASON_LIBPULSE_MISSING = "libpulse_missing"
REASON_AUDIO_SERVER_UNAVAILABLE = "audio_server_unavailable"
REASON_PIPEWIRE_PULSE_MISSING = "pipewire_pulse_missing"
REASON_DEFAULT_SINK_MISSING = "default_sink_missing"
REASON_PACTL_MISSING = "pactl_missing"
REASON_MONITOR_SOURCE_MISSING = "monitor_source_missing"
REASON_MONITOR_OPEN_FAILED = "monitor_open_failed"
REASON_UNSUPPORTED_ARCHITECTURE = "unsupported_architecture"
REASON_UNKNOWN_FAILURE = "unknown_failure"

SERVER_PULSE = "pulse"
SERVER_PIPEWIRE_PULSE = "pipewire-pulse"
SERVER_UNAVAILABLE = "unavailable"
SERVER_UNKNOWN = "unknown"

_LIBPULSE_SONAMES = ("libpulse.so.0", "libpulse.so", "pulse")
_PROBE_SAMPLERATE = 48000
_PROBE_FRAMES = 512
_PROBE_TIMEOUT_S = 1.5
_COMMAND_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class LinuxAudioCapability:
    """Structured result of a Linux system-audio probe."""

    ready: bool
    reason: str
    server_kind: str = SERVER_UNKNOWN
    default_sink: str = ""
    monitor_source: str = ""
    package_family: str = "unknown"
    remediation_key: str = REASON_UNKNOWN_FAILURE
    detail: str = ""

    @property
    def sink_id(self) -> str:
        """Stable default-sink identifier used by capture sources."""
        return self.default_sink

    @property
    def monitor_id(self) -> str:
        """Stable monitor-source identifier used by capture sources."""
        return self.monitor_source


@dataclass(frozen=True)
class LinuxMonitorSelection:
    """Resolved default sink and validated loopback monitor."""

    sink_id: str
    sink_name: str
    monitor_id: str
    monitor_name: str
    channels: int
    server_kind: str
    #: Device id accepted by SoundCard ``get_microphone`` (may equal sink_id).
    soundcard_id: str = ""


def _package_family() -> str:
    try:
        from services.linux_deps import detect_package_family
        return detect_package_family(fallback="unknown")
    except Exception:
        return "unknown"


def _capability(
    reason: str,
    *,
    ready: bool = False,
    server_kind: str = SERVER_UNKNOWN,
    default_sink: str = "",
    monitor_source: str = "",
    package_family: Optional[str] = None,
    detail: str = "",
) -> LinuxAudioCapability:
    family = package_family if package_family is not None else _package_family()
    return LinuxAudioCapability(
        ready=ready,
        reason=reason,
        server_kind=server_kind,
        default_sink=default_sink,
        monitor_source=monitor_source,
        package_family=family,
        remediation_key=reason if not ready else REASON_READY,
        detail=detail,
    )


def _libpulse_available(
    loader: Optional[Callable[[str], Any]] = None,
) -> bool:
    load = loader
    if load is None:
        import ctypes

        def load(name: str) -> Any:
            return ctypes.CDLL(name)

    for soname in _LIBPULSE_SONAMES:
        try:
            load(soname)
            return True
        except OSError:
            continue
        except Exception:
            continue
    return False


def _run_text(command: Sequence[str], timeout: float = _COMMAND_TIMEOUT_S) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _unit_active(unit: str) -> Optional[bool]:
    if not shutil.which("systemctl"):
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    status = (completed.stdout or "").strip().lower()
    if status == "active":
        return True
    if status in {"inactive", "failed", "dead"}:
        return False
    return None


def detect_linux_audio_server() -> str:
    """Best-effort classification of the active Pulse-compatible server."""
    info = _run_text(["pactl", "info"])
    lowered = info.lower()
    if "pipewire" in lowered:
        return SERVER_PIPEWIRE_PULSE
    if "pulseaudio" in lowered or "server name" in lowered:
        return SERVER_PULSE

    pipewire = _unit_active("pipewire")
    pipewire_pulse = _unit_active("pipewire-pulse")
    pulse = _unit_active("pulseaudio")
    if pipewire is True and pipewire_pulse is False:
        return SERVER_UNAVAILABLE
    if pipewire_pulse is True:
        return SERVER_PIPEWIRE_PULSE
    if pulse is True:
        return SERVER_PULSE
    if pipewire is True:
        return SERVER_UNAVAILABLE
    if pipewire is False and pulse is False:
        return SERVER_UNAVAILABLE
    return SERVER_UNKNOWN


def _device_id(device: Any) -> str:
    for attr in ("id", "name"):
        value = getattr(device, attr, None)
        if value is not None and str(value).strip():
            return str(value)
    return str(device)


def _device_name(device: Any) -> str:
    value = getattr(device, "name", None)
    if value is not None and str(value).strip():
        return str(value)
    return _device_id(device)


def _device_channels(device: Any) -> int:
    try:
        channels = int(getattr(device, "channels", 2) or 2)
    except Exception:
        channels = 2
    return max(1, channels)


def _positive_loopback_flag(device: Any) -> bool:
    """True only when the backend exposes positive loopback metadata."""
    try:
        flag = getattr(device, "isloopback", None)
        if callable(flag):
            return bool(flag())
        if flag is None:
            return False
        return bool(flag)
    except Exception:
        return False


def _pactl_monitor_for_sink(sink_id: str) -> Optional[str]:
    """Return the proven Pulse/PipeWire monitor source for ``sink_id``.

    Returns None when pactl cannot prove a monitor exists for the sink. Never
    fabricates ``<sink>.monitor`` without list evidence.
    """
    if not sink_id:
        return None
    candidate = f"{sink_id}.monitor"
    sources = _run_text(["pactl", "list", "short", "sources"])
    for line in sources.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name == candidate:
            return name
    # Some servers expose the monitor under a different name; require an exact
    # "Monitor of: <sink>" association from the long listing.
    detail = _run_text(["pactl", "list", "sources"])
    current_name = ""
    monitor_of = ""
    for raw in detail.splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            if current_name and monitor_of == sink_id:
                return current_name
            current_name = line.split(":", 1)[1].strip()
            monitor_of = ""
        elif line.lower().startswith("monitor of:"):
            monitor_of = line.split(":", 1)[1].strip()
    if current_name and monitor_of == sink_id:
        return current_name
    return None


def _pick_exact_loopback(
    soundcard_module: Any,
    *,
    sink_id: str,
    expected_monitor: str,
) -> Any:
    """Return the SoundCard loopback whose ID exactly matches the monitor.

    SoundCard may also expose the default-sink loopback under the sink ID
    itself. That representation is accepted only when the device is a positive
    loopback and no other device claims the expected monitor ID.
    """
    try:
        loopbacks = list(soundcard_module.all_microphones(include_loopback=True))
    except Exception:
        loopbacks = []

    exact = None
    sink_keyed = None
    for item in loopbacks:
        if not _positive_loopback_flag(item):
            continue
        item_id = _device_id(item)
        if item_id == expected_monitor:
            exact = item
        elif item_id == sink_id:
            sink_keyed = item

    if exact is not None:
        return exact

    # Fall back to an explicit lookup by the proven monitor id.
    try:
        by_monitor = soundcard_module.get_microphone(
            id=expected_monitor, include_loopback=True
        )
    except Exception:
        by_monitor = None
    if (
        by_monitor is not None
        and _positive_loopback_flag(by_monitor)
        and _device_id(by_monitor) == expected_monitor
    ):
        return by_monitor

    # SoundCard sink-id keying: only when the returned device is the sink id
    # itself (not some other sink's monitor).
    if sink_keyed is not None:
        return sink_keyed
    try:
        by_sink = soundcard_module.get_microphone(
            id=sink_id, include_loopback=True
        )
    except Exception:
        by_sink = None
    if (
        by_sink is not None
        and _positive_loopback_flag(by_sink)
        and _device_id(by_sink) in {expected_monitor, sink_id}
    ):
        return by_sink
    return None


def resolve_linux_monitor(
    soundcard_module: Any = None,
    *,
    server_kind: Optional[str] = None,
) -> LinuxMonitorSelection:
    """Resolve the default sink's validated loopback monitor.

    Raises:
        RuntimeError: With a stable reason code as the message.
    """
    if soundcard_module is None:
        try:
            import soundcard as soundcard_module
        except Exception as exc:
            raise RuntimeError(REASON_SOUNDCARD_MISSING) from exc

    try:
        speaker = soundcard_module.default_speaker()
    except Exception as exc:
        raise RuntimeError(REASON_DEFAULT_SINK_MISSING) from exc
    if speaker is None:
        raise RuntimeError(REASON_DEFAULT_SINK_MISSING)

    sink_id = _device_id(speaker)
    sink_name = _device_name(speaker)
    if not sink_id:
        raise RuntimeError(REASON_DEFAULT_SINK_MISSING)

    # SoundCard normally exposes the default sink's loopback either under the
    # canonical Pulse monitor id or under the sink id itself. Prove those
    # representations first so pactl remains optional on standard desktops.
    expected_monitor = f"{sink_id}.monitor"
    monitor = _pick_exact_loopback(
        soundcard_module,
        sink_id=sink_id,
        expected_monitor=expected_monitor,
    )

    # Some Pulse-compatible servers use a nonstandard monitor name. In that
    # case only, let pactl prove the exact "Monitor of" association. Never use
    # fuzzy SoundCard matches or a monitor belonging to another sink.
    if monitor is None:
        pactl_monitor = _pactl_monitor_for_sink(sink_id)
        if not pactl_monitor:
            if not shutil.which("pactl"):
                raise RuntimeError(REASON_PACTL_MISSING)
            raise RuntimeError(REASON_MONITOR_SOURCE_MISSING)
        expected_monitor = pactl_monitor
        monitor = _pick_exact_loopback(
            soundcard_module,
            sink_id=sink_id,
            expected_monitor=expected_monitor,
        )
    if monitor is None:
        raise RuntimeError(REASON_MONITOR_SOURCE_MISSING)

    sc_id = _device_id(monitor)
    # Record the authoritative pactl monitor id even when SoundCard keys the
    # loopback by sink id; never accept a different sink's monitor.
    if sc_id not in {expected_monitor, sink_id}:
        raise RuntimeError(REASON_MONITOR_SOURCE_MISSING)
    stable_monitor_id = expected_monitor

    kind = server_kind or detect_linux_audio_server()
    if kind == SERVER_UNKNOWN:
        kind = SERVER_PULSE
    return LinuxMonitorSelection(
        sink_id=sink_id,
        sink_name=sink_name,
        monitor_id=stable_monitor_id,
        monitor_name=_device_name(monitor),
        channels=_device_channels(monitor),
        server_kind=kind,
        soundcard_id=sc_id or stable_monitor_id,
    )


def _soundcard_lookup_id(selection: LinuxMonitorSelection) -> str:
    return selection.soundcard_id or selection.monitor_id or selection.sink_id


def _verify_monitor_open_once(
    soundcard_module: Any,
    selection: LinuxMonitorSelection,
) -> None:
    lookup_id = _soundcard_lookup_id(selection)
    try:
        monitor = soundcard_module.get_microphone(
            id=lookup_id, include_loopback=True
        )
    except Exception:
        monitor = None
    if monitor is None and lookup_id != selection.monitor_id:
        try:
            monitor = soundcard_module.get_microphone(
                id=selection.monitor_id, include_loopback=True
            )
        except Exception:
            monitor = None
    if monitor is None or not _positive_loopback_flag(monitor):
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED)
    if _device_id(monitor) not in {
        selection.monitor_id,
        selection.sink_id,
        lookup_id,
    }:
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED)
    recorder = monitor.recorder(
        samplerate=_PROBE_SAMPLERATE,
        channels=min(2, max(1, selection.channels)),
    )
    try:
        entered = recorder.__enter__()
        try:
            data = entered.record(numframes=_PROBE_FRAMES)
        finally:
            recorder.__exit__(None, None, None)
    except Exception as exc:
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED) from exc
    if data is None:
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED)
    import numpy as np

    arr = np.asarray(data)
    if arr.size <= 0:
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED)


def _verify_monitor_open(
    soundcard_module: Any,
    selection: LinuxMonitorSelection,
    *,
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> None:
    """Open/read the monitor with a hard timeout; classify stalls as open failure.

    Uses a daemon worker so a wedged SoundCard/libpulse call cannot keep the
    process alive at exit. ``future.cancel()`` cannot stop a running callable,
    so ThreadPoolExecutor is intentionally avoided here.
    """
    import threading

    error_box: list[BaseException] = []
    done = threading.Event()

    def _worker() -> None:
        try:
            _verify_monitor_open_once(soundcard_module, selection)
        except BaseException as exc:  # noqa: BLE001 - surface to waiter
            error_box.append(exc)
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker,
        name="linux-monitor-open-probe",
        daemon=True,
    )
    thread.start()
    if not done.wait(timeout=timeout_s):
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED)
    if error_box:
        exc = error_box[0]
        if isinstance(exc, RuntimeError):
            raise exc
        raise RuntimeError(REASON_MONITOR_OPEN_FAILED) from exc


def probe_linux_audio(
    *,
    verify_open: bool = True,
    platform: Optional[str] = None,
    machine: Optional[str] = None,
    soundcard_module: Any = None,
    libpulse_loader: Optional[Callable[[str], Any]] = None,
    open_timeout_s: float = _PROBE_TIMEOUT_S,
) -> LinuxAudioCapability:
    """Probe Linux system-audio readiness. Never raises."""
    try:
        from meeting.platform import normalize_linux_machine

        host = platform or sys.platform
        if not str(host).startswith("linux"):
            return _capability(
                REASON_UNSUPPORTED_ARCHITECTURE,
                detail=f"platform={host}",
            )
        if normalize_linux_machine(
            machine if machine is not None else platform_module.machine()
        ) is None:
            return _capability(
                REASON_UNSUPPORTED_ARCHITECTURE,
                detail=f"machine={machine or platform_module.machine()}",
            )

        # Classify missing libpulse before importing SoundCard's Pulse backend.
        if not _libpulse_available(libpulse_loader):
            return _capability(REASON_LIBPULSE_MISSING)

        if soundcard_module is None:
            try:
                import soundcard as soundcard_module
            except Exception as exc:
                return _capability(
                    REASON_SOUNDCARD_MISSING,
                    detail=type(exc).__name__,
                )

        server_kind = detect_linux_audio_server()
        if server_kind == SERVER_UNAVAILABLE:
            pipewire = _unit_active("pipewire")
            pipewire_pulse = _unit_active("pipewire-pulse")
            if pipewire is True and pipewire_pulse is not True:
                return _capability(
                    REASON_PIPEWIRE_PULSE_MISSING,
                    server_kind=SERVER_UNAVAILABLE,
                )
            return _capability(
                REASON_AUDIO_SERVER_UNAVAILABLE,
                server_kind=SERVER_UNAVAILABLE,
            )

        try:
            selection = resolve_linux_monitor(
                soundcard_module, server_kind=server_kind
            )
        except RuntimeError as exc:
            reason = str(exc) or REASON_UNKNOWN_FAILURE
            if reason not in {
                REASON_SOUNDCARD_MISSING,
                REASON_DEFAULT_SINK_MISSING,
                REASON_PACTL_MISSING,
                REASON_MONITOR_SOURCE_MISSING,
                REASON_MONITOR_OPEN_FAILED,
            }:
                reason = REASON_UNKNOWN_FAILURE
            if reason in {
                REASON_DEFAULT_SINK_MISSING,
                REASON_MONITOR_SOURCE_MISSING,
                REASON_MONITOR_OPEN_FAILED,
            } and server_kind == SERVER_UNKNOWN:
                pipewire = _unit_active("pipewire")
                pipewire_pulse = _unit_active("pipewire-pulse")
                if pipewire is True and pipewire_pulse is not True:
                    return _capability(
                        REASON_PIPEWIRE_PULSE_MISSING,
                        server_kind=SERVER_UNAVAILABLE,
                    )
                if pipewire is not True and _unit_active("pulseaudio") is not True:
                    return _capability(
                        REASON_AUDIO_SERVER_UNAVAILABLE,
                        server_kind=SERVER_UNAVAILABLE,
                    )
            return _capability(reason, server_kind=server_kind)

        if verify_open:
            try:
                _verify_monitor_open(
                    soundcard_module,
                    selection,
                    timeout_s=open_timeout_s,
                )
            except RuntimeError:
                return _capability(
                    REASON_MONITOR_OPEN_FAILED,
                    server_kind=selection.server_kind,
                    default_sink=selection.sink_id,
                    monitor_source=selection.monitor_id,
                )

        return _capability(
            REASON_READY,
            ready=True,
            server_kind=selection.server_kind,
            default_sink=selection.sink_id,
            monitor_source=selection.monitor_id,
        )
    except Exception as exc:
        logger.exception("Linux system-audio probe failed unexpectedly")
        return _capability(
            REASON_UNKNOWN_FAILURE,
            detail=type(exc).__name__,
        )
