"""Own an installed agent's descendants until its request ends."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading


class _WindowsJob:
    """A kill-on-close job, assigned before the agent can start helpers."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                        ("flags", w.DWORD), ("min_working_set", c.c_size_t),
                        ("max_working_set", c.c_size_t), ("active_processes", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class Limits(c.Structure):
            _fields_ = [("basic", Basic), ("io", c.c_uint64 * 6),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]

        self._lock = threading.Lock()
        self._api = c.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [c.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            fn = getattr(self._api, name)
            fn.argtypes, fn.restype = args, result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise c.WinError(c.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(self._handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, proc):
        import ctypes as c
        from ctypes import wintypes as w

        if not self._api.AssignProcessToJobObject(self._handle, int(proc._handle)):
            raise c.WinError(c.get_last_error())

        class ThreadEntry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                        ("pid", w.DWORD), ("priority", w.LONG), ("delta", w.LONG),
                        ("flags", w.DWORD)]

        for name, args, result in (
            ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
            ("Thread32First", [w.HANDLE, c.POINTER(ThreadEntry)], w.BOOL),
            ("Thread32Next", [w.HANDLE, c.POINTER(ThreadEntry)], w.BOOL),
            ("OpenThread", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("ResumeThread", [w.HANDLE], w.DWORD),
        ):
            fn = getattr(self._api, name)
            fn.argtypes, fn.restype = args, result
        snapshot = self._api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = c.sizeof(entry)
            found = self._api.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.pid == proc.pid:
                    thread = self._api.OpenThread(2, False, entry.tid)
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if self._api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                    finally:
                        self._api.CloseHandle(thread)
                    return
                found = self._api.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Could not resume the installed agent's initial thread.")
        finally:
            self._api.CloseHandle(snapshot)

    def close(self):
        with self._lock:
            if self._handle:
                self._api.TerminateJobObject(self._handle, 1)
                self._api.CloseHandle(self._handle)
                self._handle = None


def popen_agent(argv, **kwargs):
    """Start windowlessly with descendant ownership, or fail before running."""
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        proc._agent_process_group = True
        return proc
    job = _WindowsJob()
    proc = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW | 4
        proc = subprocess.Popen(argv, **kwargs)
        job.attach_and_resume(proc)
        proc._agent_job = job
        return proc
    except BaseException:
        job.close()
        if proc is not None:
            proc.kill()
            proc.wait(timeout=3)
        raise


def kill_agent_process(proc, wait_s=3.0):
    """End owned descendants even after their parent has exited."""
    if proc is None:
        return
    job = getattr(proc, "_agent_job", None)
    if job is not None:
        job.close()
    elif getattr(proc, "_agent_process_group", False):
        # The group survives its leader, unlike a tree rebuilt from a PID.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                # macOS answers EPERM, not ESRCH, once the group holds only
                # its exited, unreaped leader; the wait below reaps it.
                break
    elif proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=wait_s,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc.terminate()
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1)


def run_agent_probe(argv, *, timeout, **kwargs):
    """A short captured command with the same ownership as meeting passes."""
    proc = popen_agent(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    finally:
        kill_agent_process(proc)
