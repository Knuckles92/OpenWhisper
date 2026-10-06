"""Side mouse buttons as shortcuts on Windows, through a WH_MOUSE_LL hook.

Windows calls a low-level mouse hook for every mouse move, on the thread that
installed it, and silently removes a hook whose callback is too slow. So the
hook lives on its own message-loop thread, returns for anything but a side
button before doing any work, and hands matched presses to a second thread.
It is independent of the keyboard hook, so a failure here never affects
keyboard shortcuts.

Imported on every platform by tests; the Win32 libraries load only in start().
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
LLMHF_INJECTED = 0x01
PM_NOREMOVE = 0x0000

_BUTTONS = {1: "mouse4", 2: "mouse5"}
_MODIFIER_KEYS = (
    ("ctrl", (0x11,)),
    ("alt", (0x12,)),
    ("shift", (0x10,)),
    ("win", (0x5B, 0x5C)),
)

LRESULT = wintypes.LPARAM


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def _load_libraries():
    """Private WinDLL instances, so these argtypes never clash with ``keyboard``'s."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hookproc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, kernel32, hookproc


class _Run:
    """One installation: its hook thread, dispatch queue and callback."""

    def __init__(self):
        self.ready = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.thread_id = 0
        self.installed = False
        # ctypes frees a callback trampoline with its last Python reference,
        # and Windows would then call into freed memory.
        self.proc = None


class MouseButtonHook:
    """Suppress claimed side-button presses and report them in order.

    Args:
        claims: ``claims(button, modifiers) -> bool``, asked inside the hook
            for each side-button press. True swallows the press (and later
            its release) so the focused app never sees Back or Forward.
        on_button: ``on_button(button, pressed, modifiers, at)``, called on the
            dispatch thread, never inside the hook. ``at`` is the hook's
            ``time.monotonic()``.
    """

    def __init__(
        self,
        claims: Callable[[str, frozenset], bool],
        on_button: Callable[[str, bool, frozenset, float], None],
        *,
        libraries=None,
    ):
        self._claims = claims
        self._on_button = on_button
        self._libraries = libraries
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None
        # Kept across restarts: a release whose press was swallowed before a
        # rehook must be swallowed too, or the app sees a lone XBUTTONUP.
        self._suppressed: set = set()
        self.worst_callback_ms = 0.0
        self._reported_ms = 0.0

    @property
    def running(self) -> bool:
        run = self._run
        return run is not None and run.thread is not None and run.thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._run is not None:
                return
            if self._libraries is None:
                self._libraries = _load_libraries()
            run = _Run()
            self._run = run
            threading.Thread(
                target=self._dispatch, args=(run.events,),
                name="mouse-hook-dispatch", daemon=True,
            ).start()
            run.thread = threading.Thread(
                target=self._hook_loop, args=(run,), name="mouse-hook", daemon=True
            )
            run.thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            run, self._run = self._run, None
        if run is None:
            return
        run.events.put(None)
        if run.ready.wait(timeout) and run.thread_id:
            self._libraries[0].PostThreadMessageW(run.thread_id, WM_QUIT, 0, 0)
        run.thread.join(timeout)

    def _hook_loop(self, run: _Run) -> None:
        user32, kernel32, hookproc = self._libraries
        message = wintypes.MSG()
        # A thread has no message queue until it asks for a message, and
        # PostThreadMessageW(WM_QUIT) fails without one.
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
        run.thread_id = kernel32.GetCurrentThreadId()
        run.proc = hookproc(self._make_callback(run.events))
        handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, run.proc, kernel32.GetModuleHandleW(None), 0
        )
        run.installed = bool(handle)
        run.ready.set()
        if not handle:
            logger.warning(
                "Mouse button shortcuts unavailable (error %s)", ctypes.get_last_error()
            )
            return
        logger.info("Mouse button hook installed")
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(handle)
            logger.info("Mouse button hook removed")

    def _make_callback(self, events: queue.Queue):
        user32 = self._libraries[0]
        call_next = user32.CallNextHookEx
        key_state = user32.GetAsyncKeyState
        suppressed = self._suppressed
        claims = self._claims
        clock = time.monotonic
        timer = time.perf_counter
        from_address = MSLLHOOKSTRUCT.from_address

        def modifiers() -> frozenset:
            return frozenset(
                name for name, keys in _MODIFIER_KEYS
                if any(key_state(key) & 0x8000 for key in keys)
            )

        def callback(code, wparam, lparam):
            if wparam != WM_XBUTTONDOWN and wparam != WM_XBUTTONUP:
                return call_next(None, code, wparam, lparam)
            started = timer()
            try:
                if code < 0:
                    return call_next(None, code, wparam, lparam)
                info = from_address(lparam)
                if info.flags & LLMHF_INJECTED:
                    return call_next(None, code, wparam, lparam)
                button = _BUTTONS.get((info.mouseData >> 16) & 0xFFFF)
                if button is None:
                    return call_next(None, code, wparam, lparam)
                if wparam == WM_XBUTTONDOWN:
                    held = modifiers()
                    if not claims(button, held):
                        return call_next(None, code, wparam, lparam)
                    suppressed.add(button)
                    events.put((button, True, held, clock()))
                    return 1
                if button not in suppressed:
                    return call_next(None, code, wparam, lparam)
                suppressed.discard(button)
                events.put((button, False, frozenset(), clock()))
                return 1
            except Exception:
                return call_next(None, code, wparam, lparam)
            finally:
                elapsed_ms = (timer() - started) * 1000
                if elapsed_ms > self.worst_callback_ms:
                    self.worst_callback_ms = elapsed_ms

        return callback

    def _dispatch(self, events: queue.Queue) -> None:
        while True:
            event = events.get()
            if event is None:
                return
            try:
                self._on_button(*event)
            except Exception:
                logger.exception("Mouse button shortcut failed")
            self._report_latency()

    def _report_latency(self) -> None:
        worst = self.worst_callback_ms
        if worst <= self._reported_ms:
            return
        self._reported_ms = worst
        from services.diagnostics import record_metrics

        record_metrics(mouse_hook_callback_ms=round(worst, 3))
