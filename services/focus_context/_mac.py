"""The frontmost app on macOS, from NSWorkspace (no Accessibility permission).

AppKit comes with pynput's pyobjc frameworks. Importing it costs far more
than an identity read may, so a first call starts the import in the
background and reports no app until it is ready.

Text near the cursor through the Accessibility API is written but stays off
(``_AX_TEXT_ENABLED``) until it has been tested on a Mac.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Optional

from config import config
from services.focus_context import MAX_SELECTION_CHARS, AppIdentity, TextContext, catalog

logger = logging.getLogger(__name__)

_AX_TEXT_ENABLED = False

_import_lock = threading.Lock()
_import_started = False


def _appkit():
    """AppKit and objc once imported, else None (and start importing them)."""
    global _import_started
    appkit, objc = sys.modules.get("AppKit"), sys.modules.get("objc")
    if appkit is not None and objc is not None:
        return appkit, objc
    with _import_lock:
        if not _import_started:
            _import_started = True
            threading.Thread(target=_import_appkit, name="focus-context-appkit",
                             daemon=True).start()
    return None


def _import_appkit() -> None:
    try:
        import AppKit  # noqa: F401  (loads the framework for later identity reads)
        import objc  # noqa: F401
    except Exception:
        logger.info("App awareness is unavailable: AppKit could not be imported")


def frontmost_identity() -> Optional[AppIdentity]:
    modules = _appkit()
    if modules is None:
        return None
    appkit, objc = modules
    # Off the main thread, Cocoa objects need a pool to be released.
    with objc.autorelease_pool():
        app = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        bundle = str(app.bundleIdentifier() or "")
        name = str(app.localizedName() or "")
        pid = int(app.processIdentifier())
    if pid == os.getpid():
        return AppIdentity("openwhisper", "OpenWhisper", pid=pid, is_self=True,
                           platform="macos")
    app_id = bundle.lower() or name.lower()
    if not app_id:
        return None
    return AppIdentity(app_id, catalog.app_name(app_id, name), pid=pid, platform="macos")


class AxTextReader:
    """Text around the caret through the Accessibility API, on one thread."""

    source = "ax"

    def __init__(self):
        import HIServices

        self._hi = HIServices
        self._system = HIServices.AXUIElementCreateSystemWide()
        # The default is 6 s; a hung app must not hold the capture thread.
        HIServices.AXUIElementSetMessagingTimeout(self._system, 0.3)

    def close(self) -> None:
        self._system = None

    def _attribute(self, element, name):
        error, value = self._hi.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == 0 else None

    def _string_for(self, element, location: int, length: int) -> str:
        if length <= 0:
            return ""
        hi = self._hi
        span = hi.AXValueCreate(hi.kAXValueCFRangeType, (location, length))
        error, value = hi.AXUIElementCopyParameterizedAttributeValue(
            element, hi.kAXStringForRangeParameterizedAttribute, span, None)
        return str(value) if error == 0 and value is not None else ""

    def read(self, identity, *, include_text: bool, include_selection: bool) -> Optional[TextContext]:
        if not _AX_TEXT_ENABLED or not self._hi.AXIsProcessTrusted():
            return None
        if _secure_input_enabled():
            return None
        hi = self._hi
        import objc

        with objc.autorelease_pool():
            focused = self._attribute(self._system, hi.kAXFocusedUIElementAttribute)
            if focused is None:
                return None
            error, pid = hi.AXUIElementGetPid(focused, None)
            if error != 0 or pid == os.getpid() or (identity.pid and pid != identity.pid):
                return None
            roles = {self._attribute(focused, hi.kAXRoleAttribute),
                     self._attribute(focused, hi.kAXSubroleAttribute)}
            if "AXSecureTextField" in roles:
                return TextContext(source=self.source, blocked=True)
            span = self._attribute(focused, hi.kAXSelectedTextRangeAttribute)
            if span is None:
                return None
            ok, selection = hi.AXValueGetValue(span, hi.kAXValueCFRangeType, None)
            if not ok:
                return None
            start, length = int(selection.location), int(selection.length)
            selected = ""
            if include_selection or include_text:
                if length > MAX_SELECTION_CHARS:
                    return TextContext(source=self.source)
                selected = self._string_for(focused, start, length)
            before = after = ""
            if include_text:
                first = max(0, start - config.CONTEXT_BEFORE_CHARS)
                before = self._string_for(focused, first, start - first)
                total = self._attribute(focused, hi.kAXNumberOfCharactersAttribute)
                end = start + length
                if total is not None:
                    after = self._string_for(
                        focused, end, min(config.CONTEXT_AFTER_CHARS, int(total) - end))
            return TextContext(before, selected, after, caret_known=include_text,
                               selection_known=True, source=self.source)



def _secure_input_enabled() -> bool:
    """Whether a password prompt anywhere has turned on secure keyboard entry."""
    try:
        import ctypes

        carbon = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Carbon.framework/Carbon")
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        return True
