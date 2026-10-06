"""Text around the caret in other apps, through Windows UI Automation.

Pure ctypes COM: UIAutomationCore's interfaces derive from IUnknown only,
which pywin32 cannot call, and comtypes is not a dependency. A reader
belongs to the thread that created it. That thread joins the multithreaded
apartment for itself, and no interface pointer ever leaves it.

The vtable slots below were verified against the type library embedded in
UIAutomationCore.dll.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import POINTER, byref, c_int, c_void_p, wintypes
from typing import Optional

from config import config
from services.focus_context import MAX_SELECTION_CHARS, TextContext

_CLSID_CUIAUTOMATION8 = "{E22AD333-B25F-460C-83D0-0581107395C9}"
_CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_IUIAUTOMATION2 = "{34723AFF-0C9D-49D0-9896-7AB52DF8CD8A}"
_IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
_IID_TEXT_PATTERN = "{32EBA289-3583-42C9-9C59-3B6D9A1E9B6A}"
_IID_TEXT_PATTERN2 = "{506A921A-FCC9-409F-B23B-37EB74106872}"
_TEXT_PATTERN_ID = 10014
_TEXT_PATTERN2_ID = 10024

_RELEASE = 2
# IUIAutomation / IUIAutomation2
_ELEMENT_FROM_HANDLE = 6
_GET_FOCUSED_ELEMENT = 8
_PUT_CONNECTION_TIMEOUT = 61
_PUT_TRANSACTION_TIMEOUT = 63
# IUIAutomationElement
_GET_CURRENT_PATTERN_AS = 14
_GET_CURRENT_PROCESS_ID = 20
_GET_CURRENT_IS_PASSWORD = 35
# IUIAutomationTextPattern / IUIAutomationTextPattern2
_GET_SELECTION = 5
_GET_CARET_RANGE = 10
# IUIAutomationTextRangeArray
_GET_LENGTH = 3
_GET_ELEMENT = 4
# IUIAutomationTextRange
_CLONE = 3
_GET_TEXT = 12
_MOVE_ENDPOINT_BY_UNIT = 14
_MOVE_ENDPOINT_BY_RANGE = 15

_ENDPOINT_START = 0
_ENDPOINT_END = 1
_UNIT_CHARACTER = 0

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = -2147417850

#: UI Automation's own bounds: how long to reach an app's provider and how
#: long one call may take. The capture deadline is the outer bound.
CONNECTION_TIMEOUT_MS = 500
TRANSACTION_TIMEOUT_MS = 1000


class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_PROTOTYPES: dict = {}


def _call(pointer: c_void_p, slot: int, *args, argtypes=()):
    """Call vtable ``slot`` of a COM interface; a failed HRESULT raises OSError."""
    prototype = _PROTOTYPES.get(argtypes)
    if prototype is None:
        prototype = _PROTOTYPES[argtypes] = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, c_void_p, *argtypes)
    table = ctypes.cast(pointer, POINTER(c_void_p))[0]
    function = ctypes.cast(table, POINTER(c_void_p))[slot]
    return prototype(function)(pointer, *args)


_RELEASE_PROTOTYPE = None


def _release(pointer: Optional[c_void_p]) -> None:
    global _RELEASE_PROTOTYPE
    if pointer is None or not pointer.value:
        return
    if _RELEASE_PROTOTYPE is None:
        _RELEASE_PROTOTYPE = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
    table = ctypes.cast(pointer, POINTER(c_void_p))[0]
    _RELEASE_PROTOTYPE(ctypes.cast(table, POINTER(c_void_p))[_RELEASE])(pointer)
    pointer.value = None


class UiaTextReader:
    """A UI Automation client for one thread. Create, use and close it there."""

    source = "uia"

    def __init__(self):
        self._ole32 = ctypes.WinDLL("ole32")
        self._oleaut32 = ctypes.WinDLL("oleaut32")
        self._ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = [
            POINTER(_GUID), c_void_p, wintypes.DWORD, POINTER(_GUID), POINTER(c_void_p)]
        self._ole32.CoCreateInstance.restype = ctypes.HRESULT
        self._oleaut32.SysStringLen.argtypes = [c_void_p]
        self._oleaut32.SysStringLen.restype = ctypes.c_uint
        self._oleaut32.SysFreeString.argtypes = [c_void_p]
        self._oleaut32.SysFreeString.restype = None
        result = self._ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        if result < 0 and result != _RPC_E_CHANGED_MODE:
            raise OSError(result, "CoInitializeEx failed")
        self._uninitialize = result >= 0
        self._automation = c_void_p()
        try:
            self._create_automation()
        except Exception:
            self.close()
            raise

    def _create_automation(self) -> None:
        try:
            self._ole32.CoCreateInstance(
                byref(_guid(_CLSID_CUIAUTOMATION8)), None, _CLSCTX_INPROC_SERVER,
                byref(_guid(_IID_IUIAUTOMATION2)), byref(self._automation))
        except OSError:
            self._automation = c_void_p()
        if self._automation.value:
            _call(self._automation, _PUT_CONNECTION_TIMEOUT, CONNECTION_TIMEOUT_MS,
                  argtypes=(wintypes.DWORD,))
            _call(self._automation, _PUT_TRANSACTION_TIMEOUT, TRANSACTION_TIMEOUT_MS,
                  argtypes=(wintypes.DWORD,))
            return
        # Before Windows 8 there is no IUIAutomation2 and so no timeouts;
        # the capture deadline still bounds the wait.
        self._ole32.CoCreateInstance(
            byref(_guid(_CLSID_CUIAUTOMATION)), None, _CLSCTX_INPROC_SERVER,
            byref(_guid(_IID_IUIAUTOMATION)), byref(self._automation))

    def close(self) -> None:
        _release(self._automation)
        if self._uninitialize:
            self._uninitialize = False
            self._ole32.CoUninitialize()

    def read(self, identity, *, include_text: bool, include_selection: bool) -> Optional[TextContext]:
        """The focused element's text, or None for unknown, a password or another app."""
        element = c_void_p()
        try:
            _call(self._automation, _GET_FOCUSED_ELEMENT, byref(element),
                  argtypes=(POINTER(c_void_p),))
            if not element.value:
                return None
            pid = c_int()
            _call(element, _GET_CURRENT_PROCESS_ID, byref(pid), argtypes=(POINTER(c_int),))
            expected = getattr(identity, "pid", None)
            # Our own windows answer UIA on the Qt thread, and focus that
            # moved to another app since identity was taken is not its text.
            if pid.value == os.getpid() or (expected and pid.value != expected):
                return None
            return self._read_element(element, include_text, include_selection)
        finally:
            _release(element)

    def _read_window(self, hwnd: int, *, include_text: bool, include_selection: bool):
        """Read one window's element instead of the focused one (tests)."""
        element = c_void_p()
        try:
            _call(self._automation, _ELEMENT_FROM_HANDLE, c_void_p(hwnd), byref(element),
                  argtypes=(c_void_p, POINTER(c_void_p)))
            return self._read_element(element, include_text, include_selection)
        finally:
            _release(element)

    def _read_element(self, element: c_void_p, include_text: bool,
                      include_selection: bool) -> Optional[TextContext]:
        password = c_int()
        _call(element, _GET_CURRENT_IS_PASSWORD, byref(password), argtypes=(POINTER(c_int),))
        if password.value:
            return None
        pattern = self._pattern(element, _TEXT_PATTERN2_ID, _IID_TEXT_PATTERN2)
        has_caret_range = pattern is not None
        if pattern is None:
            pattern = self._pattern(element, _TEXT_PATTERN_ID, _IID_TEXT_PATTERN)
        if pattern is None:
            # Chromium builds its tree lazily: no TextPattern means unknown,
            # never "empty".
            return None
        anchor = None
        try:
            anchor, selection_known = self._selection(pattern)
            if anchor is None and has_caret_range:
                anchor = self._caret(pattern)
            if anchor is None:
                return TextContext(selection_known=selection_known, source=self.source)
            selected = ""
            if selection_known:
                selected = self._text(anchor, MAX_SELECTION_CHARS + 1)
                if len(selected) > MAX_SELECTION_CHARS:
                    selected, selection_known = "", False
            before = after = ""
            caret_known = False
            if include_text:
                try:
                    before = self._before(anchor, config.CONTEXT_BEFORE_CHARS)
                    after = self._after(anchor, config.CONTEXT_AFTER_CHARS)
                    caret_known = True
                except OSError:
                    before = after = ""
            return TextContext(before, selected, after, caret_known=caret_known,
                               selection_known=selection_known, source=self.source)
        finally:
            _release(anchor)
            _release(pattern)

    def _pattern(self, element: c_void_p, pattern_id: int, iid: str) -> Optional[c_void_p]:
        pattern = c_void_p()
        try:
            _call(element, _GET_CURRENT_PATTERN_AS, pattern_id, byref(_guid(iid)), byref(pattern),
                  argtypes=(c_int, POINTER(_GUID), POINTER(c_void_p)))
        except OSError:
            _release(pattern)
            return None
        return pattern if pattern.value else None

    def _selection(self, pattern: c_void_p) -> tuple[Optional[c_void_p], bool]:
        """The first selected range, and whether the selection could be read."""
        ranges = c_void_p()
        try:
            _call(pattern, _GET_SELECTION, byref(ranges), argtypes=(POINTER(c_void_p),))
            if not ranges.value:
                return None, False
            count = c_int()
            _call(ranges, _GET_LENGTH, byref(count), argtypes=(POINTER(c_int),))
            if count.value < 1:
                return None, True
            first = c_void_p()
            _call(ranges, _GET_ELEMENT, 0, byref(first), argtypes=(c_int, POINTER(c_void_p)))
            return (first if first.value else None), True
        except OSError:
            return None, False
        finally:
            _release(ranges)

    def _caret(self, pattern: c_void_p) -> Optional[c_void_p]:
        active = c_int()
        caret = c_void_p()
        try:
            _call(pattern, _GET_CARET_RANGE, byref(active), byref(caret),
                  argtypes=(POINTER(c_int), POINTER(c_void_p)))
        except OSError:
            _release(caret)
            return None
        return caret if caret.value else None

    def _text(self, text_range: c_void_p, limit: int) -> str:
        value = c_void_p()
        try:
            _call(text_range, _GET_TEXT, limit, byref(value), argtypes=(c_int, POINTER(c_void_p)))
            if not value.value:
                return ""
            return ctypes.wstring_at(value.value, self._oleaut32.SysStringLen(value))
        finally:
            if value.value:
                self._oleaut32.SysFreeString(value)

    def _clone(self, text_range: c_void_p) -> c_void_p:
        clone = c_void_p()
        _call(text_range, _CLONE, byref(clone), argtypes=(POINTER(c_void_p),))
        if not clone.value:
            raise OSError("Clone returned no range")
        return clone

    def _before(self, anchor: c_void_p, limit: int) -> str:
        span = self._clone(anchor)
        try:
            _call(span, _MOVE_ENDPOINT_BY_RANGE, _ENDPOINT_END, anchor, _ENDPOINT_START,
                  argtypes=(c_int, c_void_p, c_int))
            moved = c_int()
            _call(span, _MOVE_ENDPOINT_BY_UNIT, _ENDPOINT_START, _UNIT_CHARACTER, -limit,
                  byref(moved), argtypes=(c_int, c_int, c_int, POINTER(c_int)))
            # Some providers count a line break as one unit and return two
            # characters for it, so read a little more and keep the tail.
            return self._text(span, limit * 2)[-limit:]
        finally:
            _release(span)

    def _after(self, anchor: c_void_p, limit: int) -> str:
        span = self._clone(anchor)
        try:
            _call(span, _MOVE_ENDPOINT_BY_RANGE, _ENDPOINT_START, anchor, _ENDPOINT_END,
                  argtypes=(c_int, c_void_p, c_int))
            moved = c_int()
            _call(span, _MOVE_ENDPOINT_BY_UNIT, _ENDPOINT_END, _UNIT_CHARACTER, limit,
                  byref(moved), argtypes=(c_int, c_int, c_int, POINTER(c_int)))
            return self._text(span, limit)
        finally:
            _release(span)
