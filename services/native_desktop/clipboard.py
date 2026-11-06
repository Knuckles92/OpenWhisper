"""Desktop clipboard ownership without a GUI toolkit.

The speech service keeps its established paste/key-release fencing. This module
only owns clipboard data: writes must succeed before a paste can be sent, and a
delayed restore never replaces a copy made by the user in the meantime.
"""

from __future__ import annotations

import ctypes
import base64
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)
MARKER = "application/x-openwhisper-auto-paste"
MAX_CAPTURE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Snapshot:
    formats: dict[str | int, bytes]
    complete: bool = True


@dataclass(frozen=True)
class Lease:
    snapshot: Snapshot
    token: bytes
    html: str = ""


@dataclass(frozen=True)
class ClipboardStageResult:
    written: bool
    lease: Lease | None = None
    restore_unavailable: bool = False


class _MacClipboard:
    def __init__(self):
        import AppKit
        import objc

        self.appkit, self.objc = AppKit, objc

    def read(self) -> Snapshot:
        with self.objc.autorelease_pool():
            board = self.appkit.NSPasteboard.generalPasteboard()
            result = {}
            for kind in board.types() or []:
                data = board.dataForType_(kind)
                if data is not None:
                    if len(data) > MAX_CAPTURE_BYTES:
                        raise ValueError("Clipboard item exceeds capture limit")
                    result[str(kind)] = bytes(data)
            return Snapshot(result)

    def write(self, data: dict) -> None:
        from Foundation import NSData

        with self.objc.autorelease_pool():
            board = self.appkit.NSPasteboard.generalPasteboard()
            board.clearContents()
            for kind, content in data.items():
                if not board.setData_forType_(NSData.dataWithBytes_length_(content, len(content)), kind):
                    raise OSError("The pasteboard refused a clipboard format")

    def text_formats(self, text: str, html: str = "") -> dict:
        result = {"public.utf8-plain-text": text.encode("utf-8")}
        if html:
            result["public.html"] = html.encode("utf-8")
        return result

    def text(self, snapshot: Snapshot) -> str:
        return snapshot.formats.get("public.utf8-plain-text", b"").decode("utf-8", "replace")

    def sequence(self):
        with self.objc.autorelease_pool():
            return int(self.appkit.NSPasteboard.generalPasteboard().changeCount())


class _WindowsClipboard:
    """Rendered clipboard data; no delayed message-loop rendering."""

    def __init__(self):
        from ctypes import wintypes as w

        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("OpenClipboard", [w.HWND], w.BOOL),
            ("CloseClipboard", [], w.BOOL),
            ("EmptyClipboard", [], w.BOOL),
            ("EnumClipboardFormats", [w.UINT], w.UINT),
            ("GetClipboardData", [w.UINT], w.HANDLE),
            ("SetClipboardData", [w.UINT, w.HANDLE], w.HANDLE),
            ("RegisterClipboardFormatW", [w.LPCWSTR], w.UINT),
            ("GetClipboardSequenceNumber", [], w.DWORD),
        ):
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ("GlobalSize", [w.HANDLE], ctypes.c_size_t),
            ("GlobalLock", [w.HANDLE], ctypes.c_void_p),
            ("GlobalUnlock", [w.HANDLE], w.BOOL),
            ("GlobalAlloc", [w.UINT, ctypes.c_size_t], w.HANDLE),
            ("GlobalFree", [w.HANDLE], w.HANDLE),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        create = self.user.CreateWindowExW
        create.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
        create.restype = w.HWND
        self._create_window = lambda: create(0, "STATIC", "OpenWhisper Clipboard", 0, 0, 0, 0, 0, w.HWND(-3), None, None, None)
        self.user.IsWindow.argtypes, self.user.IsWindow.restype = [w.HWND], w.BOOL
        self.window = self._create_window()
        if not self.window:
            raise OSError("Could not create the clipboard owner window")

    def _open(self):
        if not self.user.IsWindow(self.window):
            self.window = self._create_window()
        for _ in range(20):
            if self.user.OpenClipboard(self.window):
                return
            time.sleep(0.005)
        raise OSError("The clipboard is busy")

    def _format(self, key):
        return key if isinstance(key, int) else self.user.RegisterClipboardFormatW(key)

    def read(self) -> Snapshot:
        self._open()
        try:
            result, complete, kind = {}, True, 0
            while (kind := self.user.EnumClipboardFormats(kind)):
                handle = self.user.GetClipboardData(kind)
                size = self.kernel.GlobalSize(handle) if handle else 0
                # Bitmap, palette and metafile handles aren't global memory.
                if not size or size > MAX_CAPTURE_BYTES:
                    complete = False
                    continue
                pointer = self.kernel.GlobalLock(handle)
                if not pointer:
                    complete = False
                    continue
                try:
                    result[kind] = ctypes.string_at(pointer, size)
                finally:
                    self.kernel.GlobalUnlock(handle)
            return Snapshot(result, complete)
        finally:
            self.user.CloseClipboard()

    def write(self, data: dict) -> None:
        # Allocate everything first, so allocation failure preserves the clipboard.
        allocated = []
        try:
            for kind, content in data.items():
                handle = self.kernel.GlobalAlloc(0x0002, max(1, len(content)))
                if not handle:
                    raise OSError("Clipboard allocation failed")
                pointer = self.kernel.GlobalLock(handle)
                if not pointer:
                    self.kernel.GlobalFree(handle)
                    raise OSError("Clipboard allocation could not be locked")
                ctypes.memmove(pointer, content, len(content))
                self.kernel.GlobalUnlock(handle)
                allocated.append((self._format(kind), handle))
            self._open()
            try:
                if not self.user.EmptyClipboard():
                    raise OSError("The clipboard could not be emptied")
                for index, (kind, handle) in enumerate(allocated):
                    if not self.user.SetClipboardData(kind, handle):
                        raise OSError("The clipboard refused the data")
                    allocated[index] = (kind, None)  # Ownership transferred to Windows.
            finally:
                self.user.CloseClipboard()
        finally:
            for _, handle in allocated:
                if handle:
                    self.kernel.GlobalFree(handle)

    def text_formats(self, text: str, html: str = "") -> dict:
        result = {13: (text + "\0").encode("utf-16-le")}
        if html:
            fragment = html.encode("utf-8")
            before = b"<html><head><meta charset=\"utf-8\"></head><body><!--StartFragment-->"
            after = b"<!--EndFragment--></body></html>"
            header = "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
            start = len(header.format(0, 0, 0, 0).encode("ascii"))
            result["HTML Format"] = header.format(start, start + len(before + fragment + after), start + len(before), start + len(before + fragment)).encode("ascii") + before + fragment + after + b"\0"
        return result

    def text(self, snapshot: Snapshot) -> str:
        return snapshot.formats.get(13, b"").decode("utf-16-le", "replace").rstrip("\0")

    def sequence(self):
        return int(self.user.GetClipboardSequenceNumber())


class _WaylandClipboard:
    """Use the compositor's data-control clipboard, without focusing a window."""

    def __init__(self):
        if not shutil.which("wl-copy") or not shutil.which("wl-paste"):
            raise RuntimeError("Install wl-clipboard for background clipboard access on Wayland")
        self.owner = None
        self.token = None

    def read(self) -> Snapshot:
        listed = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=2)
        if listed.returncode:
            return Snapshot({})
        kinds = listed.stdout.decode("utf-8", "replace").splitlines()
        preferred = next((kind for kind in ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "text/uri-list", "image/png") if kind in kinds), None)
        if preferred is None:
            return Snapshot({}, complete=not kinds)
        result = subprocess.run(["wl-paste", "--no-newline", "--type", preferred], capture_output=True, timeout=2)
        result.check_returncode()
        if len(result.stdout) > MAX_CAPTURE_BYTES:
            raise ValueError("Clipboard item exceeds capture limit")
        # wl-copy serves a single format. Never promise a rich/multi-item restore.
        complete = all(kind.startswith("text/plain") or kind in ("UTF8_STRING", "STRING", "TEXT") for kind in kinds) or len(kinds) == 1
        return Snapshot({preferred: result.stdout}, complete)

    def write(self, data: dict) -> None:
        data = dict(data)
        self.token = data.pop(MARKER, None)
        if not data:
            subprocess.run(["wl-copy", "--clear"], check=True, timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        kind = next((key for key in ("text/uri-list", "text/html", "text/plain;charset=utf-8") if key in data), next(iter(data)))
        previous = self.owner
        self.owner = subprocess.Popen(["wl-copy", "--foreground", "--type", kind], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owner.stdin.write(data[kind])
        self.owner.stdin.close()
        # Reading back is an ownership handshake, never a focus request.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if self.owner.poll() is not None:
                raise OSError("The compositor refused the clipboard")
            if self.read().formats.get(kind) == data[kind]:
                if previous is not None:
                    try:
                        previous.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        previous.terminate()
                return
            time.sleep(0.01)
        self.owner.terminate()
        raise TimeoutError("The compositor did not accept the clipboard")

    def text_formats(self, text: str, html: str = "") -> dict:
        # Plain text remains universally pasteable; wl-copy cannot advertise two forms.
        return {"text/plain;charset=utf-8": text.encode("utf-8")}

    def text(self, snapshot: Snapshot) -> str:
        return next(iter(snapshot.formats.values()), b"").decode("utf-8", "replace")

    def sequence(self):
        return None

    def owns(self, token):
        return self.token == token and self.owner is not None and self.owner.poll() is None

    def close(self):
        # wl-copy normally survives its caller so a committed copy remains
        # available when OpenWhisper closes. The compositor owns its lifetime.
        pass


class _WaylandNativeClipboard:
    """Native data-control source; all MIME formats share one ownership lease."""

    def __init__(self, executable):
        self.process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def _request(self, request):
        import json

        if self.process.poll() is not None:
            raise OSError("The native clipboard service stopped")
        self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline(MAX_CAPTURE_BYTES * 2 + 1)
        if not line or len(line) > MAX_CAPTURE_BYTES * 2:
            raise OSError("The native clipboard service sent an invalid reply")
        response = json.loads(line)
        if "error" in response:
            raise OSError(response["error"])
        return response

    def read(self):
        response = self._request({"method": "read"})
        return Snapshot({kind: base64.b64decode(data, validate=True) for kind, data in response["formats"].items()}, bool(response["complete"]))

    def write(self, data):
        self._request({"method": "write", "formats": {kind: base64.b64encode(content).decode("ascii") for kind, content in data.items()}})

    def text_formats(self, text, html=""):
        result = {"text/plain;charset=utf-8": text.encode("utf-8"), "text/plain": text.encode("utf-8")}
        if html:
            result["text/html"] = html.encode("utf-8")
        return result

    def text(self, snapshot):
        for kind in ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING"):
            if kind in snapshot.formats:
                return snapshot.formats[kind].decode("utf-8", "replace")
        return ""

    def sequence(self):
        return None

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        # EOF leaves only the native owner thread until another app copies.
        # Keeping rendered data alive is part of a successful clipboard copy.
        self.process.stdout.close()


def _wayland_clipboard():
    roots = [Path(os.environ.get("OPENWHISPER_ROOT", Path(__file__).resolve().parents[2]))]
    explicit = os.environ.get("OPENWHISPER_CLIPBOARD")
    candidates = [Path(explicit)] if explicit else []
    for root in roots:
        candidates += [root / "native/bin/openwhisper-clipboard", root / "native/target/debug/openwhisper-clipboard", root / "native/target/release/openwhisper-clipboard"]
    target = os.environ.get("CARGO_TARGET_DIR")
    if target:
        candidates += [Path(target) / profile / "openwhisper-clipboard" for profile in ("debug", "release")]
    for executable in candidates:
        if executable.is_file():
            return _WaylandNativeClipboard(executable)
    return _WaylandClipboard()


class _X11Clipboard:
    """An ICCCM clipboard owner serving all captured MIME targets."""

    def __init__(self):
        from Xlib import X, display

        self.X = X
        self.display = display.Display()
        self.window = self.display.screen().root.create_window(0, 0, 1, 1, 0, 0, X.InputOnly, X.CopyFromParent)
        self.selection = self.display.intern_atom("CLIPBOARD")
        self.formats = {}
        self.transfers = {}
        self.lock = threading.RLock()
        self.running = True
        self.thread = threading.Thread(target=self._serve, name="native-clipboard-x11", daemon=True)
        self.thread.start()

    def _serve(self):
        import select
        from Xlib.protocol import event

        while self.running:
            try:
                if not select.select([self.display.fileno()], [], [], 0.1)[0] and not self.display.pending_events():
                    continue
                with self.lock:
                    request = self.display.next_event()
                    if request.type == self.X.PropertyNotify and request.state == self.X.PropertyDelete:
                        transfer_key = (request.window.id, request.atom)
                        transfer = self.transfers.get(transfer_key)
                        if transfer:
                            target, data, offset = transfer
                            chunk = data[offset:offset + 60_000]
                            request.window.change_property(request.atom, target, 8, chunk)
                            if chunk:
                                self.transfers[transfer_key] = (target, data, offset + len(chunk))
                            else:
                                self.transfers.pop(transfer_key, None)
                            self.display.flush()
                        continue
                    if request.type != self.X.SelectionRequest:
                        continue
                    target = self.display.get_atom_name(request.target)
                    prop = request.property or request.target
                    if target == "TARGETS":
                        atoms = [self.display.intern_atom(kind) for kind in self.formats]
                        atoms += [self.display.intern_atom("TARGETS")]
                        request.requestor.change_property(prop, self.display.intern_atom("ATOM"), 32, atoms)
                    elif target in self.formats:
                        data = self.formats[target]
                        if len(data) > 60_000:
                            request.requestor.change_attributes(event_mask=self.X.PropertyChangeMask)
                            request.requestor.change_property(prop, self.display.intern_atom("INCR"), 32, [len(data)])
                            self.transfers[(request.requestor.id, prop)] = (request.target, data, 0)
                        else:
                            request.requestor.change_property(prop, request.target, 8, data)
                    else:
                        prop = self.X.NONE
                    request.requestor.send_event(event.SelectionNotify(time=request.time, requestor=request.requestor, selection=request.selection, target=request.target, property=prop), propagate=False)
                    self.display.flush()
            except Exception:
                if self.running:
                    logger.debug("X11 clipboard request failed", exc_info=True)

    def _request(self, connection, window, target):
        import select

        prop = connection.intern_atom("_OPENWHISPER_CLIPBOARD_READ")
        atom = connection.intern_atom(target)
        window.convert_selection(connection.intern_atom("CLIPBOARD"), atom, prop, self.X.CurrentTime)
        connection.flush()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not connection.pending_events():
                select.select([connection.fileno()], [], [], max(0, deadline - time.monotonic()))
                if not connection.pending_events():
                    break
            reply = connection.next_event()
            if reply.type == self.X.SelectionNotify:
                if reply.property == self.X.NONE:
                    return None
                result = window.get_full_property(prop, self.X.AnyPropertyType)
                window.delete_property(prop)
                connection.flush()
                if result is None:
                    return None
                if connection.get_atom_name(result.property_type) == "INCR":
                    if result.value and result.value[0] > MAX_CAPTURE_BYTES:
                        raise ValueError("Clipboard item exceeds capture limit")
                    chunks, size = [], 0
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if not connection.pending_events():
                            select.select([connection.fileno()], [], [], max(0, deadline - time.monotonic()))
                            if not connection.pending_events():
                                break
                        update = connection.next_event()
                        if update.type != self.X.PropertyNotify or update.atom != prop or update.state != self.X.PropertyNewValue:
                            continue
                        piece = window.get_full_property(prop, self.X.AnyPropertyType)
                        window.delete_property(prop)
                        connection.flush()
                        if piece is None:
                            continue
                        content = bytes(piece.value)
                        if not content:
                            return b"".join(chunks)
                        size += len(content)
                        if size > MAX_CAPTURE_BYTES:
                            raise ValueError("Clipboard item exceeds capture limit")
                        chunks.append(content)
                        deadline = time.monotonic() + 3
                    raise TimeoutError("Clipboard incremental transfer stalled")
                if len(result.value) > MAX_CAPTURE_BYTES:
                    raise ValueError("Clipboard item exceeds capture limit")
                return result.value
        raise TimeoutError("The clipboard owner did not reply")

    def read(self) -> Snapshot:
        from Xlib import display

        connection = display.Display()
        window = connection.screen().root.create_window(0, 0, 1, 1, 0, 0, self.X.InputOnly, self.X.CopyFromParent, event_mask=self.X.PropertyChangeMask)
        try:
            if connection.get_selection_owner(connection.intern_atom("CLIPBOARD")) == self.X.NONE:
                return Snapshot({})
            targets = self._request(connection, window, "TARGETS")
            result, complete, captured_bytes = {}, True, 0
            for target in targets or []:
                kind = connection.get_atom_name(target)
                if kind in ("TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS", "DELETE", "INSERT_SELECTION", "INSERT_PROPERTY"):
                    continue
                try:
                    data = self._request(connection, window, kind)
                    if data is not None:
                        captured_bytes += len(data)
                        if captured_bytes > MAX_CAPTURE_BYTES:
                            raise ValueError("Clipboard contents exceed capture limit")
                        result[kind] = bytes(data)
                except (ValueError, TimeoutError):
                    complete = False
            return Snapshot(result, complete)
        finally:
            window.destroy()
            connection.close()

    def write(self, data: dict) -> None:
        with self.lock:
            self.formats = dict(data)
            self.window.set_selection_owner(self.selection, self.X.CurrentTime)
            self.display.sync()
            if self.display.get_selection_owner(self.selection).id != self.window.id:
                raise OSError("Could not acquire clipboard ownership")

    def text_formats(self, text: str, html: str = "") -> dict:
        encoded = text.encode("utf-8")
        result = {"UTF8_STRING": encoded, "text/plain;charset=utf-8": encoded, "text/plain": encoded, "TEXT": encoded, "STRING": text.encode("latin-1", "replace")}
        if html:
            result["text/html"] = html.encode("utf-8")
        return result

    def text(self, snapshot: Snapshot) -> str:
        for key in ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "TEXT", "STRING"):
            if key in snapshot.formats:
                return snapshot.formats[key].decode("latin-1" if key == "STRING" else "utf-8", "replace")
        return ""

    def sequence(self):
        with self.lock:
            owner = self.display.get_selection_owner(self.selection)
            return getattr(owner, "id", 0)

    def close(self):
        if not self.running:
            return
        # Ask the desktop clipboard manager to retain our rendered MIME data
        # before the owner window disappears, as prescribed by ICCCM.
        try:
            import select
            from Xlib import display

            connection = display.Display()
            try:
                manager = connection.intern_atom("CLIPBOARD_MANAGER")
                if connection.get_selection_owner(manager) != self.X.NONE:
                    window = connection.screen().root.create_window(0, 0, 1, 1, 0, 0, self.X.InputOnly, self.X.CopyFromParent)
                    window.convert_selection(manager, connection.intern_atom("SAVE_TARGETS"), connection.intern_atom("_OPENWHISPER_SAVE"), self.X.CurrentTime)
                    connection.flush()
                    deadline = time.monotonic() + 0.5
                    while time.monotonic() < deadline:
                        if not connection.pending_events():
                            select.select([connection.fileno()], [], [], max(0, deadline-time.monotonic()))
                        if connection.pending_events() and connection.next_event().type == self.X.SelectionNotify:
                            break
                    window.destroy()
            finally:
                connection.close()
        except Exception:
            logger.debug("Clipboard manager persistence unavailable", exc_info=True)
        self.running = False
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=0.3)
        with self.lock:
            self.display.close()


class NativeClipboard:
    def __init__(self, backend=None):
        self._backend = backend
        self._lock = threading.RLock()
        self._pending: Lease | None = None
        self._restore_timer: threading.Timer | None = None
        self._prefetch = None
        self._prefetch_timer: threading.Timer | None = None
        self._selection_generation = 0

    def _os(self):
        if self._backend is None:
            self._backend = (_WindowsClipboard() if sys.platform == "win32" else _MacClipboard() if sys.platform == "darwin" else _wayland_clipboard() if os.environ.get("WAYLAND_DISPLAY") else _X11Clipboard())
        return self._backend

    def _write(self, text: str, html: str = "", token: bytes | None = None) -> bool:
        try:
            backend = self._os()
            data = backend.text_formats(text or "", html or "")
            if token is not None:
                data[MARKER] = token
            backend.write(data)
            if isinstance(backend, _WaylandClipboard):
                return backend.text(backend.read()) == text
            return self._owns_token(token) if token else backend.text(backend.read()) == text
        except Exception:
            logger.warning("Native clipboard write failed", exc_info=True)
            return False

    def _owns_token(self, token: bytes) -> bool:
        backend = self._os()
        if isinstance(backend, _WaylandClipboard):
            return backend.owns(token)
        key = backend._format(MARKER) if isinstance(backend, _WindowsClipboard) else MARKER
        return backend.read().formats.get(key) == token

    def _owns(self, lease: Lease) -> bool:
        try:
            return self._pending is lease and self._owns_token(lease.token)
        except Exception:
            return False

    def copy_to_clipboard(self, text: str, html: str = "") -> bool:
        with self._lock:
            self._selection_generation += 1
            self._pending = None
            return self._write(text, html)

    write_text = copy_to_clipboard

    def request_prefetch(self, delay_ms: int = 100) -> None:
        with self._lock:
            self.discard_prefetch()
            def capture():
                try:
                    with self._lock:
                        backend = self._os()
                        sequence = backend.sequence()
                        if sequence is not None:
                            snapshot = backend.read()
                            if sequence == backend.sequence():
                                self._prefetch = (sequence, snapshot)
                except Exception:
                    logger.debug("Clipboard prefetch unavailable", exc_info=True)
            self._prefetch_timer = threading.Timer(max(0, delay_ms) / 1000, capture)
            self._prefetch_timer.daemon = True
            self._prefetch_timer.start()

    prefetch_clipboard_snapshot = request_prefetch

    def discard_prefetch(self) -> None:
        with self._lock:
            if self._prefetch_timer:
                self._prefetch_timer.cancel()
            self._prefetch = None

    discard_clipboard_prefetch = discard_prefetch

    def _snapshot(self):
        backend = self._os()
        prefetched, self._prefetch = self._prefetch, None
        # An X11 owner can change its offer without changing its window id.
        if prefetched and not isinstance(backend, _X11Clipboard) and prefetched[0] == backend.sequence():
            return prefetched[1]
        return backend.read()

    def stage_transcript_for_paste(self, text: str, html: str | None = None, *, restore: bool = True):
        with self._lock:
            self._selection_generation += 1
            self._restore_pending()
            html = html or ""
            if not restore:
                self.discard_prefetch()
                return ClipboardStageResult(self._write(text, html))
            try:
                snapshot = self._snapshot()
                if not snapshot.complete:
                    return ClipboardStageResult(self._write(text, html), restore_unavailable=bool(snapshot.formats) or not snapshot.complete)
                lease = Lease(snapshot, secrets.token_bytes(16), html)
                if not self._write(text, html, lease.token):
                    return ClipboardStageResult(False)
                self._pending = lease
                return ClipboardStageResult(True, lease)
            except Exception:
                logger.debug("Clipboard snapshot unavailable", exc_info=True)
                return ClipboardStageResult(self._write(text, html), restore_unavailable=True)

    stage_text = stage_transcript_for_paste

    def _restore_pending(self):
        if self._restore_timer:
            self._restore_timer.cancel()
        lease = self._pending
        if lease is not None and self._owns(lease):
            try:
                self._os().write(lease.snapshot.formats)
            except Exception:
                logger.warning("Clipboard restore failed", exc_info=True)
        self._pending = None

    def schedule_restore(self, lease: Lease, delay_ms: int) -> bool:
        with self._lock:
            if not self._owns(lease):
                return False
            if self._restore_timer:
                self._restore_timer.cancel()
            def restore():
                with self._lock:
                    if self._pending is lease:
                        self._restore_pending()
            self._restore_timer = threading.Timer(max(0, delay_ms) / 1000, restore)
            self._restore_timer.daemon = True
            self._restore_timer.start()
            return True

    def schedule_clipboard_restore(self, stage: ClipboardStageResult) -> bool:
        from config import config

        return bool(stage.lease and self.schedule_restore(stage.lease, config.AUTO_PASTE_CLIPBOARD_RESTORE_DELAY_MS))

    def commit_text(self, lease: Lease, text: str) -> bool:
        with self._lock:
            if not self._owns(lease):
                return False
            self._pending = None
            return self._write(text, lease.html)

    def commit_transcript_clipboard(self, stage: ClipboardStageResult, text: str) -> bool:
        return self.commit_text(stage.lease, text) if stage.lease else stage.written

    def capture_selection(self, callback: Callable[[str | None], None], *, timeout_ms: int | None = None, send_copy=None) -> None:
        from services import synthetic_keys

        with self._lock:
            self._selection_generation += 1
            generation = self._selection_generation
        def capture():
            result = ""
            try:
                with self._lock:
                    self._restore_pending()
                    backend = self._os()
                    snapshot = self._snapshot()
                    if not snapshot.complete:
                        callback(None)
                        return
                    before = backend.sequence()
                (send_copy or synthetic_keys.send_copy)()
                deadline = time.monotonic() + (timeout_ms or 800) / 1000
                while time.monotonic() < deadline:
                    with self._lock:
                        if generation != self._selection_generation:
                            callback("")
                            return
                        current = backend.read()
                        if backend.sequence() != before or current.formats != snapshot.formats:
                            result = backend.text(current)
                            # Only restore if this is still the captured copy.
                            copied_sequence = backend.sequence()
                            if copied_sequence == backend.sequence():
                                backend.write(snapshot.formats)
                            break
                    time.sleep(0.02)
            except Exception:
                logger.debug("Selected text capture unavailable", exc_info=True)
            callback(result)
        threading.Thread(target=capture, name="native-selection", daemon=True).start()

    def copy_files_to_clipboard(self, paths, text: str = "", *, image=None) -> bool:
        with self._lock:
            try:
                backend = self._os()
                resolved = [Path(path).absolute() for path in paths]
                data = backend.text_formats(text)
                if isinstance(backend, _WindowsClipboard):
                    import struct

                    data[15] = struct.pack("IiiII", 20, 0, 0, 0, 1) + ("\0".join(str(path) for path in resolved) + "\0\0").encode("utf-16-le")
                elif isinstance(backend, _MacClipboard):
                    import plistlib

                    data["NSFilenamesPboardType"] = plistlib.dumps([str(path) for path in resolved])
                else:
                    data["text/uri-list"] = ("\r\n".join(path.as_uri() for path in resolved) + "\r\n").encode("utf-8")
                self._pending = None
                backend.write(data)
                return True
            except Exception:
                logger.warning("Could not copy files", exc_info=True)
                return False

    write_files = copy_files_to_clipboard

    def cleanup(self):
        with self._lock:
            self._selection_generation += 1
            self.discard_prefetch()
            self._restore_pending()
            if self._backend and hasattr(self._backend, "close"):
                self._backend.close()

    close = cleanup
