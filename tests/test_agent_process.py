"""Cleanup retains ownership after a headless agent exits before its helper."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from services.agent_process import popen_agent, run_agent_probe
from services.installed_agents import kill_process_tree


def _alive(pid):
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        api = c.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        api.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD()
            return bool(api.GetExitCodeProcess(handle, c.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    # An exited child can remain a zombie until init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("parent_exits", [True, False])
def test_cleanup_reaps_helpers_even_after_parent_exit(tmp_path, parent_exits):
    pid_file = tmp_path / "helper.pid"
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); "
        + ("" if parent_exits else "time.sleep(20)")
    )
    proc = popen_agent([sys.executable, "-c", code, str(pid_file)], stdin=subprocess.DEVNULL)
    pid = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        assert _alive(pid)
        if parent_exits:
            assert proc.wait(timeout=5) == 0
        kill_process_tree(proc)
        deadline = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(pid)
        assert proc.poll() is not None
        kill_process_tree(proc)  # repeated driver/finally cleanup is harmless
    finally:
        kill_process_tree(proc)
        if pid is not None and _alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.kill(pid, signal.SIGKILL)


def test_probe_timeout_closes_owned_process(tmp_path):
    marker = tmp_path / "late-write"
    code = "import time,sys; from pathlib import Path; time.sleep(.5); Path(sys.argv[1]).touch()"
    with pytest.raises(subprocess.TimeoutExpired):
        run_agent_probe([sys.executable, "-c", code, str(marker)], timeout=.1,
                        stdin=subprocess.DEVNULL)
    time.sleep(.6)
    assert not marker.exists()
