"""macOS audio activity adapter using Core Audio process objects via ctypes.

Reads ``kAudioHardwarePropertyProcessObjectList`` from CoreAudio.framework
(macOS 14+ Sonoma / Sequoia) to inspect which processes are actively running
input (microphone) or output (playback) streams. Only process metadata is read;
no audio is captured.

When a browser holds the microphone, optionally inspects the active tab URL and
window title via AppleScript (with a short timeout and permission-denial backoff
so Automation prompts never spam the user).
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from typing import Final, Optional

from meeting.call_detection.adapters import AudioActivityAdapter
from meeting.call_detection.catalog import (
    KNOWN_BROWSERS,
    deduplicate_processes,
    is_process_of,
)
from meeting.call_detection.models import ActiveAudioProcess, BrowserContext

logger = logging.getLogger(__name__)


def _fourcc(code: str) -> int:
    b = code.encode("ascii")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]


_K_AUDIO_OBJECT_SYSTEM_OBJECT: Final[int] = 1
_K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL: Final[int] = _fourcc("glob")
_K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN: Final[int] = 0

_K_AUDIO_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST: Final[int] = _fourcc("prs#")
_K_AUDIO_PROCESS_PROPERTY_PID: Final[int] = _fourcc("ppid")
_K_AUDIO_PROCESS_PROPERTY_BUNDLE_ID: Final[int] = _fourcc("pbid")
_K_AUDIO_PROCESS_PROPERTY_IS_RUNNING_INPUT: Final[int] = _fourcc("piri")
_K_AUDIO_PROCESS_PROPERTY_IS_RUNNING_OUTPUT: Final[int] = _fourcc("piro")

_K_CF_STRING_ENCODING_UTF8: Final[int] = 0x08000100

# AppleScript app names for major macOS browsers.
_APPLESCRIPT_BROWSERS: Final[dict[str, tuple[str, str]]] = {
    # canonical_id: (AppleScript application name, dialect: "chromium" | "safari")
    "com.google.Chrome": ("Google Chrome", "chromium"),
    "com.brave.Browser": ("Brave Browser", "chromium"),
    "com.microsoft.edgemac": ("Microsoft Edge", "chromium"),
    "company.thebrowser.Browser": ("Arc", "chromium"),
    "com.vivaldi.Vivaldi": ("Vivaldi", "chromium"),
    "com.apple.Safari": ("Safari", "safari"),
}


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class _CoreAudioBridge:
    """Thin ctypes wrapper around CoreAudio + CoreFoundation process queries."""

    def __init__(self) -> None:
        self._ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        addr_ptr = ctypes.POINTER(_AudioObjectPropertyAddress)
        u32_ptr = ctypes.POINTER(ctypes.c_uint32)

        self._ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32,
            addr_ptr,
            ctypes.c_uint32,
            ctypes.c_void_p,
            u32_ptr,
        ]
        self._ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32

        self._ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            addr_ptr,
            ctypes.c_uint32,
            ctypes.c_void_p,
            u32_ptr,
            ctypes.c_void_p,
        ]
        self._ca.AudioObjectGetPropertyData.restype = ctypes.c_int32

        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

    @staticmethod
    def _addr(selector: int) -> _AudioObjectPropertyAddress:
        return _AudioObjectPropertyAddress(
            mSelector=selector,
            mScope=_K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
            mElement=_K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        )

    def process_objects(self) -> list[int]:
        addr = self._addr(_K_AUDIO_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST)
        size = ctypes.c_uint32(0)
        status = self._ca.AudioObjectGetPropertyDataSize(
            _K_AUDIO_OBJECT_SYSTEM_OBJECT,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
        )
        if status != 0:
            raise OSError(f"CoreAudio process list size failed: OSStatus={status}")
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        if count <= 0:
            return []
        buf = (ctypes.c_uint32 * count)()
        status = self._ca.AudioObjectGetPropertyData(
            _K_AUDIO_OBJECT_SYSTEM_OBJECT,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            buf,
        )
        if status != 0:
            raise OSError(f"CoreAudio process list read failed: OSStatus={status}")
        actual = size.value // ctypes.sizeof(ctypes.c_uint32)
        return [int(buf[i]) for i in range(actual)]

    def _u32_prop(self, obj_id: int, selector: int) -> Optional[int]:
        addr = self._addr(selector)
        val = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(val))
        status = self._ca.AudioObjectGetPropertyData(
            obj_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val)
        )
        return int(val.value) if status == 0 else None

    def _i32_prop(self, obj_id: int, selector: int) -> Optional[int]:
        addr = self._addr(selector)
        val = ctypes.c_int32(0)
        size = ctypes.c_uint32(ctypes.sizeof(val))
        status = self._ca.AudioObjectGetPropertyData(
            obj_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val)
        )
        return int(val.value) if status == 0 else None

    def _cf_string_prop(self, obj_id: int, selector: int) -> Optional[str]:
        addr = self._addr(selector)
        ref = ctypes.c_void_p(0)
        size = ctypes.c_uint32(ctypes.sizeof(ref))
        status = self._ca.AudioObjectGetPropertyData(
            obj_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)
        )
        if status != 0 or not ref.value:
            return None
        try:
            length = self._cf.CFStringGetLength(ref)
            max_bytes = (
                self._cf.CFStringGetMaximumSizeForEncoding(
                    length, _K_CF_STRING_ENCODING_UTF8
                )
                + 1
            )
            buf = ctypes.create_string_buffer(max(16, max_bytes))
            if not self._cf.CFStringGetCString(
                ref, buf, len(buf), _K_CF_STRING_ENCODING_UTF8
            ):
                return None
            return buf.value.decode("utf-8", errors="replace")
        finally:
            self._cf.CFRelease(ref)

    def pid(self, obj_id: int) -> Optional[int]:
        return self._i32_prop(obj_id, _K_AUDIO_PROCESS_PROPERTY_PID)

    def bundle_id(self, obj_id: int) -> Optional[str]:
        return self._cf_string_prop(obj_id, _K_AUDIO_PROCESS_PROPERTY_BUNDLE_ID)

    def is_running_input(self, obj_id: int) -> bool:
        val = self._u32_prop(obj_id, _K_AUDIO_PROCESS_PROPERTY_IS_RUNNING_INPUT)
        return bool(val)

    def is_running_output(self, obj_id: int) -> bool:
        val = self._u32_prop(obj_id, _K_AUDIO_PROCESS_PROPERTY_IS_RUNNING_OUTPUT)
        return bool(val)


def _localized_app_name(pid: int) -> Optional[str]:
    """Best-effort localized app name from NSRunningApplication (if AppKit loaded)."""
    try:
        import AppKit

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            name = app.localizedName()
            if name:
                return str(name)
    except Exception:
        pass
    return None


class MacAudioActivityAdapter(AudioActivityAdapter):
    """Core Audio process-list adapter for macOS."""

    def __init__(self) -> None:
        self._bridge = _CoreAudioBridge()
        self._own_pid = os.getpid()
        # Browsers where AppleScript Automation was denied: stop retrying.
        self._denied_browsers: set[str] = set()

    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        found: list[ActiveAudioProcess] = []
        for obj_id in self._bridge.process_objects():
            if not self._bridge.is_running_input(obj_id):
                continue
            pid = self._bridge.pid(obj_id)
            if pid is None or pid == self._own_pid:
                continue
            bundle_id = self._bridge.bundle_id(obj_id) or ""
            if not bundle_id:
                continue
            name = ""
            if bundle_id.lower() == "com.apple.webkit.gpu":
                name = _localized_app_name(pid) or ""
            found.append(
                ActiveAudioProcess(
                    pid=pid,
                    bundle_id=bundle_id,
                    name=name or bundle_id,
                )
            )
        return deduplicate_processes(found)

    def is_app_playing_audio(self, canonical_id: str) -> bool:
        for obj_id in self._bridge.process_objects():
            if not self._bridge.is_running_output(obj_id):
                continue
            pid = self._bridge.pid(obj_id)
            if pid == self._own_pid:
                continue
            bundle_id = self._bridge.bundle_id(obj_id)
            if not bundle_id:
                continue
            name = None
            if bundle_id.lower() == "com.apple.webkit.gpu" and pid is not None:
                name = _localized_app_name(pid)
            if is_process_of(bundle_id, canonical_id, name):
                return True
        return False

    def browser_context(self, canonical_browser_id: str) -> Optional[BrowserContext]:
        if canonical_browser_id in self._denied_browsers:
            return None
        spec = _APPLESCRIPT_BROWSERS.get(canonical_browser_id)
        if spec is None:
            return None
        app_name, dialect = spec
        if dialect == "safari":
            script = (
                f'tell application "{app_name}"\n'
                '  if (count of windows) > 0 then\n'
                '    set t to current tab of front window\n'
                '    return (URL of t) & "\\n" & (name of t)\n'
                '  end if\n'
                'end tell'
            )
        else:
            script = (
                f'tell application "{app_name}"\n'
                '  if (count of windows) > 0 then\n'
                '    set t to active tab of front window\n'
                '    return (URL of t) & "\\n" & (title of t)\n'
                '  end if\n'
                'end tell'
            )
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
        except Exception:
            return None
        if res.returncode != 0:
            err = (res.stderr or "").lower()
            if "not authorized" in err or "-1743" in err:
                self._denied_browsers.add(canonical_browser_id)
            return None
        lines = (res.stdout or "").splitlines()
        if not lines:
            return None
        url = lines[0].strip() or None
        title = lines[1].strip() if len(lines) > 1 else ""
        return BrowserContext(
            app_name=KNOWN_BROWSERS.get(canonical_browser_id, app_name),
            url=url,
            title=title,
        )
