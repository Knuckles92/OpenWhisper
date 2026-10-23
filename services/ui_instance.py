"""One desktop UI process per data directory on Linux.

The startup data lease is shared so the History API can run beside the app.
Without a separate UI lock, a second ``openwhisper`` started a full copy:
two windows, two tray icons, and one hotkey recorded by both into the same
file, which produced two garbled History entries. Windows frozen builds
already gate on a named mutex; macOS activates the running app from the Dock.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SHOW = b"show\n"
_HANDOFF_SECONDS = 2.0
_PROCESS_INSTANCE: Optional["UiInstance"] = None


def _data_key(data_dir: str) -> str:
    root = os.path.normcase(os.path.realpath(os.path.abspath(data_dir)))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def runtime_paths(data_dir: str) -> tuple[Path, str]:
    """Lock file and abstract socket name for this data directory's UI process.

    The socket uses Linux's abstract namespace so the address stays short:
    AF_UNIX filesystem paths are capped at about 108 bytes.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        folder = Path(runtime) / "openwhisper"
    else:
        folder = Path(tempfile.gettempdir()) / f"openwhisper-{os.getuid()}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = _data_key(data_dir)
    return folder / f"ui-{key}.lock", f"\0openwhisper-ui-{os.getuid()}-{key}"


def request_show(sock_name: str, timeout: float = 0.4) -> bool:
    """Ask a listening UI to restore its window. True when the request was sent."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(timeout)
            conn.connect(sock_name)
            conn.sendall(SHOW)
        finally:
            conn.close()
    except OSError:
        return False
    return True


class UiInstance:
    """Holds the exclusive UI lock and the socket that later launches write to."""

    def __init__(self, lock=None, sock: Optional[socket.socket] = None) -> None:
        self._lock = lock
        self._sock = sock
        self._on_activate: Optional[Callable[[], None]] = None
        self._pending = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        if sock is not None:
            self._thread = threading.Thread(
                target=self._accept, name="ui-instance", daemon=True
            )
            self._thread.start()

    def attach_activate(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` when a later launch asks to show the window.

        The callback may run on the accept thread; the UI hops to Qt itself.
        """
        self._on_activate = callback
        if self._pending:
            self._invoke()

    def _invoke(self) -> None:
        callback = self._on_activate
        if callback is not None:
            callback()

    def _accept(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._closed:
            try:
                conn, _unused = sock.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(64)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 64:
                        break
                if data.startswith(b"show"):
                    self._pending = True
                    self._invoke()
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.close()


def acquire_ui_instance(data_dir: str) -> Optional[UiInstance]:
    """Take the UI lock, or ask the existing window forward and return None.

    On Windows and macOS this is a no-op primary so those launch paths stay
    unchanged. A later Linux launch that cannot reach the lock holder also
    returns None so a second copy cannot start.
    """
    global _PROCESS_INSTANCE
    if _PROCESS_INSTANCE is not None:
        return _PROCESS_INSTANCE
    if not sys.platform.startswith("linux"):
        _PROCESS_INSTANCE = UiInstance()
        return _PROCESS_INSTANCE

    import fcntl

    lock_path, sock_name = runtime_paths(data_dir)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if os.path.islink(lock_path):
        raise OSError("The UI instance lock is a symbolic link.")
    descriptor = os.open(lock_path, flags, 0o600)
    lock = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"\0")
            lock.flush()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            deadline = time.monotonic() + _HANDOFF_SECONDS
            while time.monotonic() < deadline:
                if request_show(sock_name):
                    logger.info(
                        "OpenWhisper is already running; bringing the existing window forward"
                    )
                    return None
                time.sleep(0.05)
            logger.warning(
                "OpenWhisper is already running but did not accept the show request"
            )
            return None

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(sock_name)
            sock.listen(4)
        except OSError:
            sock.close()
            raise
    except Exception:
        lock.close()
        raise

    _PROCESS_INSTANCE = UiInstance(lock, sock)
    return _PROCESS_INSTANCE


def release_ui_instance() -> None:
    global _PROCESS_INSTANCE
    instance, _PROCESS_INSTANCE = _PROCESS_INSTANCE, None
    if instance is not None:
        instance.close()
