"""Windows audio activity adapter using WASAPI ``IAudioSessionManager2``.

Inspects active capture sessions (``eCapture``) to detect processes using the
microphone, and active render sessions (``eRender``) to detect processes still
playing audio (so muting in a call never triggers a false auto-stop).

Uses direct ``ctypes`` COM vtable calls so no extra third-party packages are
required, and inspects browser window titles via ``EnumWindows`` +
``GetWindowTextW`` to distinguish known call sites (Google Meet, Teams web,
Zoom web) from generic browser mic usage.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import os
from ctypes import wintypes
from typing import Final, Optional

from meeting.call_detection.adapters import AudioActivityAdapter
from meeting.call_detection.catalog import (
    KNOWN_BROWSERS,
    canonical_app_id,
    deduplicate_processes,
    is_process_of,
)
from meeting.call_detection.models import ActiveAudioProcess, BrowserContext

logger = logging.getLogger(__name__)

_COINIT_MULTITHREADED: Final[int] = 0x0
_CLSCTX_ALL: Final[int] = 0x17
_DEVICE_STATE_ACTIVE: Final[int] = 0x00000001
_E_RENDER: Final[int] = 0
_E_CAPTURE: Final[int] = 1
_AUDIO_SESSION_STATE_ACTIVE: Final[int] = 1
_PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
_TITLE_CHARS: Final[int] = 512


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text: str) -> "_GUID":
        import uuid

        u = uuid.UUID(text)
        data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return cls(u.time_low, u.time_mid, u.time_hi_version, data4)


_CLSID_MM_DEVICE_ENUMERATOR = _GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMM_DEVICE_ENUMERATOR = _GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAUDIO_SESSION_MANAGER2 = _GUID.from_str("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
_IID_IAUDIO_SESSION_CONTROL2 = _GUID.from_str("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")


def _vtable_entry(com_ptr: ctypes.c_void_p, index: int, prototype):
    vtable = ctypes.cast(com_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return prototype(vtable[index])


def _com_release(com_ptr: ctypes.c_void_p) -> None:
    if not com_ptr or not com_ptr.value:
        return
    try:
        release = _vtable_entry(
            com_ptr, 2, ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        )
        release(com_ptr)
    except Exception:
        pass


class _WinAudioBridge:
    """Private Win32/COM handles for WASAPI session & window inspection."""

    def __init__(self) -> None:
        self._ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = ctypes.c_long

        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        self._user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int

    def process_executable(self, pid: int) -> tuple[str, str]:
        """Return ``(executable_name, stem)`` for ``pid``, or ``("", "")``."""
        if pid <= 0:
            return ("", "")
        handle = self._kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return ("", "")
        try:
            buf = ctypes.create_unicode_buffer(4096)
            size = wintypes.DWORD(len(buf))
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return ("", "")
            full_path = buf.value[: size.value]
            exe = ntpath.basename(full_path)
            stem = ntpath.splitext(exe)[0]
            return (exe, stem)
        finally:
            self._kernel32.CloseHandle(handle)

    def active_session_pids(self, data_flow: int, own_pid: int) -> list[int]:
        """Enumerate PIDs with an active WASAPI audio session for ``data_flow``."""
        self._ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        enumerator = ctypes.c_void_p(0)
        hr = self._ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MM_DEVICE_ENUMERATOR),
            None,
            _CLSCTX_ALL,
            ctypes.byref(_IID_IMM_DEVICE_ENUMERATOR),
            ctypes.byref(enumerator),
        )
        if hr < 0 or not enumerator.value:
            raise OSError(f"CoCreateInstance(MMDeviceEnumerator) failed: hr={hr:#x}")

        pids: list[int] = []
        collection = ctypes.c_void_p(0)
        try:
            # IMMDeviceEnumerator::EnumAudioEndpoints (vtable index 3)
            enum_endpoints = _vtable_entry(
                enumerator,
                3,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
            )
            hr = enum_endpoints(
                enumerator, data_flow, _DEVICE_STATE_ACTIVE, ctypes.byref(collection)
            )
            if hr < 0 or not collection.value:
                raise OSError(f"EnumAudioEndpoints failed: hr={hr:#x}")

            # IMMDeviceCollection::GetCount (index 3) & Item (index 4)
            get_count = _vtable_entry(
                collection,
                3,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
                ),
            )
            get_item = _vtable_entry(
                collection,
                4,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
            )
            dev_count = ctypes.c_uint(0)
            if get_count(collection, ctypes.byref(dev_count)) < 0:
                return pids

            for i in range(dev_count.value):
                device = ctypes.c_void_p(0)
                if get_item(collection, i, ctypes.byref(device)) < 0 or not device.value:
                    continue
                try:
                    self._collect_device_session_pids(device, own_pid, pids)
                finally:
                    _com_release(device)
        finally:
            _com_release(collection)
            _com_release(enumerator)
        return pids

    def _collect_device_session_pids(
        self, device: ctypes.c_void_p, own_pid: int, out_pids: list[int]
    ) -> None:
        # IMMDevice::Activate (index 3)
        activate = _vtable_entry(
            device,
            3,
            ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(_GUID),
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ),
        )
        manager = ctypes.c_void_p(0)
        hr = activate(
            device,
            ctypes.byref(_IID_IAUDIO_SESSION_MANAGER2),
            _CLSCTX_ALL,
            None,
            ctypes.byref(manager),
        )
        if hr < 0 or not manager.value:
            return
        sess_enum = ctypes.c_void_p(0)
        try:
            # IAudioSessionManager2::GetSessionEnumerator (index 5)
            get_sess_enum = _vtable_entry(
                manager,
                5,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
            )
            if get_sess_enum(manager, ctypes.byref(sess_enum)) < 0 or not sess_enum.value:
                return

            # IAudioSessionEnumerator::GetCount (3) & GetSession (4)
            get_count = _vtable_entry(
                sess_enum,
                3,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                ),
            )
            get_session = _vtable_entry(
                sess_enum,
                4,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
            )
            count = ctypes.c_int(0)
            if get_count(sess_enum, ctypes.byref(count)) < 0:
                return

            for idx in range(count.value):
                ctrl = ctypes.c_void_p(0)
                if get_session(sess_enum, idx, ctypes.byref(ctrl)) < 0 or not ctrl.value:
                    continue
                try:
                    # IAudioSessionControl::GetState (index 3)
                    get_state = _vtable_entry(
                        ctrl,
                        3,
                        ctypes.WINFUNCTYPE(
                            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                        ),
                    )
                    state = ctypes.c_int(0)
                    if (
                        get_state(ctrl, ctypes.byref(state)) < 0
                        or state.value != _AUDIO_SESSION_STATE_ACTIVE
                    ):
                        continue

                    # IUnknown::QueryInterface (index 0) -> IAudioSessionControl2
                    qi = _vtable_entry(
                        ctrl,
                        0,
                        ctypes.WINFUNCTYPE(
                            ctypes.c_long,
                            ctypes.c_void_p,
                            ctypes.POINTER(_GUID),
                            ctypes.POINTER(ctypes.c_void_p),
                        ),
                    )
                    ctrl2 = ctypes.c_void_p(0)
                    if (
                        qi(
                            ctrl,
                            ctypes.byref(_IID_IAUDIO_SESSION_CONTROL2),
                            ctypes.byref(ctrl2),
                        )
                        < 0
                        or not ctrl2.value
                    ):
                        continue
                    try:
                        # IAudioSessionControl2::GetProcessId (index 14)
                        get_pid = _vtable_entry(
                            ctrl2,
                            14,
                            ctypes.WINFUNCTYPE(
                                ctypes.c_long,
                                ctypes.c_void_p,
                                ctypes.POINTER(wintypes.DWORD),
                            ),
                        )
                        pid = wintypes.DWORD(0)
                        if (
                            get_pid(ctrl2, ctypes.byref(pid)) >= 0
                            and pid.value != 0
                            and int(pid.value) != own_pid
                        ):
                            out_pids.append(int(pid.value))
                    finally:
                        _com_release(ctrl2)
                finally:
                    _com_release(ctrl)
        finally:
            _com_release(sess_enum)
            _com_release(manager)

    def browser_window_titles(self, canonical_browser_id: str) -> list[str]:
        """Return visible top-level window titles owned by ``canonical_browser_id``."""
        titles: list[str] = []

        def _visit(hwnd, _lparam):
            if not self._user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            exe, stem = self.process_executable(int(pid.value))
            if canonical_app_id(exe, stem) != canonical_browser_id:
                return True
            buf = ctypes.create_unicode_buffer(_TITLE_CHARS + 1)
            length = self._user32.GetWindowTextW(hwnd, buf, len(buf))
            if length > 0:
                title = buf.value[:length].strip()
                if title:
                    titles.append(title)
            return True

        self._user32.EnumWindows(self._enum_proc(_visit), 0)
        return titles


class WindowsAudioActivityAdapter(AudioActivityAdapter):
    """WASAPI ``IAudioSessionManager2`` adapter for Windows."""

    def __init__(self) -> None:
        self._bridge = _WinAudioBridge()
        self._own_pid = os.getpid()

    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        pids = self._bridge.active_session_pids(_E_CAPTURE, self._own_pid)
        found: list[ActiveAudioProcess] = []
        for pid in pids:
            exe, stem = self._bridge.process_executable(pid)
            if not exe:
                continue
            found.append(
                ActiveAudioProcess(
                    pid=pid,
                    bundle_id=exe,
                    name=stem or exe,
                )
            )
        return deduplicate_processes(found)

    def is_app_playing_audio(self, canonical_id: str) -> bool:
        pids = self._bridge.active_session_pids(_E_RENDER, self._own_pid)
        for pid in pids:
            exe, stem = self._bridge.process_executable(pid)
            if exe and is_process_of(exe, canonical_id, stem):
                return True
        return False

    def browser_context(self, canonical_browser_id: str) -> Optional[BrowserContext]:
        titles = self._bridge.browser_window_titles(canonical_browser_id)
        if not titles:
            return None
        app_name = KNOWN_BROWSERS.get(canonical_browser_id, "")
        # Prefer any window title that looks like a known web call.
        for title in titles:
            lower = title.lower()
            if any(
                hint in lower
                for hint in ("meet -", "meet –", "microsoft teams", "zoom", "webex", "jitsi")
            ):
                return BrowserContext(app_name=app_name, url=None, title=title)
        return BrowserContext(app_name=app_name, url=None, title=titles[0])
