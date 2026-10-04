"""Keep a user-data root stable while OpenWhisper is running or restoring.

Normal processes hold a shared byte-range lock. A pending restore needs the
exclusive lock before it can replace any files, so another live copy cannot
observe a half-restored database or keep Windows file handles open.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(_Overlapped),
    )
    _kernel32.UnlockFileEx.restype = wintypes.BOOL


RESTART_FOR_RESTORE_EXIT_CODE = 73
LOCK_FILENAME = ".openwhisper-data.lock"


class DataLeaseBusy(RuntimeError):
    """Another process holds an incompatible lease for this data root."""


class StartupRestoreError(RuntimeError):
    """A pending restore could not be completed before application startup."""


class DataLease:
    """An OS-managed lock released on close or process exit."""

    def __init__(self, root: str, handle, *, exclusive: bool, overlapped=None) -> None:
        self.root = root
        self._handle = handle
        self.exclusive = exclusive
        self._overlapped = overlapped

    @classmethod
    def acquire(cls, data_dir: str, *, exclusive: bool = False) -> "DataLease":
        root = os.path.normcase(os.path.realpath(os.path.abspath(data_dir)))
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, LOCK_FILENAME)
        if os.path.islink(path):
            raise StartupRestoreError("The application data lock is a symbolic link.")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StartupRestoreError("The application data lock is not a file.")
            if os.fstat(descriptor).st_size == 0:
                handle.write(b"\0")
            handle.seek(0)
            try:
                if os.name == "nt":
                    overlapped = _Overlapped()
                    flags = 0x1 | (0x2 if exclusive else 0)
                    os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
                    if not _kernel32.LockFileEx(
                        os_handle, flags, 0, 1, 0, ctypes.byref(overlapped)
                    ):
                        error = ctypes.get_last_error()
                        raise OSError(error, ctypes.FormatError(error))
                else:
                    import fcntl

                    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise DataLeaseBusy("The application data is in use by another OpenWhisper process.") from exc
            return cls(
                root, handle, exclusive=exclusive,
                overlapped=overlapped if os.name == "nt" else None,
            )
        except Exception:
            handle.close()
            raise

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
                if not _kernel32.UnlockFileEx(
                    os_handle, 0, 1, 0, ctypes.byref(self._overlapped)
                ):
                    error = ctypes.get_last_error()
                    raise OSError(error, ctypes.FormatError(error))
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "DataLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


_PROCESS_LEASE: Optional[DataLease] = None


def acquire_startup_data_lease(data_dir: str) -> DataLease:
    """Apply a pending restore offline, then hold a shared lease until exit.

    The second pending check happens under the shared lease. If staging races
    with startup, we release it and retry exclusively rather than starting on
    data that another process is trying to replace.
    """
    global _PROCESS_LEASE
    root = os.path.normcase(os.path.realpath(os.path.abspath(data_dir)))
    if _PROCESS_LEASE is not None:
        if _PROCESS_LEASE.root != root:
            raise StartupRestoreError("OpenWhisper already holds a different data directory.")
        return _PROCESS_LEASE

    from services.backup import apply_pending_restore, pending_restore_exists

    for _ in range(4):
        try:
            lease = DataLease.acquire(root)
        except DataLeaseBusy as exc:
            raise StartupRestoreError(
                "Another OpenWhisper process is restoring this data. Wait for it to finish, then launch again."
            ) from exc
        try:
            pending = pending_restore_exists(root)
        except Exception:
            lease.close()
            raise
        if not pending:
            _PROCESS_LEASE = lease
            return lease
        lease.close()

        try:
            exclusive = DataLease.acquire(root, exclusive=True)
        except DataLeaseBusy as exc:
            raise StartupRestoreError(
                "A backup restore is pending, but another OpenWhisper process is still using this data. "
                "Close every OpenWhisper window and History API process, then launch again to finish the restore."
            ) from exc
        with exclusive:
            if pending_restore_exists(root) and not apply_pending_restore(data_dir=root):
                raise StartupRestoreError(
                    "The pending backup restore was not applied. The existing data was left in place; "
                    "launch again after resolving the restore error."
                )
    raise StartupRestoreError("The backup restore changed repeatedly during startup; launch again.")


def release_startup_data_lease() -> None:
    global _PROCESS_LEASE
    lease, _PROCESS_LEASE = _PROCESS_LEASE, None
    if lease is not None:
        lease.close()


def restart_command(main_path: str) -> list[str]:
    """Relaunch the installed app or the absolute source entry point, no args."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(main_path).resolve())]


def _restart_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("_PYI_") and key.upper() != "_MEIPASS2"
    }
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        runtime = getattr(sys, "_MEIPASS", None)
        if runtime:
            runtime = os.path.normcase(os.path.realpath(runtime))
            for key in list(environment):
                if key.upper() != "PATH":
                    continue
                entries = []
                for entry in environment[key].split(os.pathsep):
                    if not entry:
                        continue
                    absolute = os.path.normcase(os.path.realpath(entry))
                    try:
                        inside = os.path.commonpath((absolute, runtime)) == runtime
                    except ValueError:
                        inside = False
                    if not inside:
                        entries.append(entry)
                environment[key] = os.pathsep.join(entries)
    return environment


def spawn_restore_restart(main_path: str) -> None:
    """Start a clean copy after callers close the app and release the lease."""
    options = {"close_fds": True, "env": _restart_environment()}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(restart_command(main_path), **options)
