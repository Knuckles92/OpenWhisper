"""run_once lets go of a clean run's output, without stopping what the program left running.

Real child processes, like test_automation_hooks_programs.
"""

from __future__ import annotations

import threading
import time

from services.automation import programs
from tests.test_agent_process import _alive
from tests.test_automation_hooks_programs import PY, kill_pid, wait_until

#: Writes to its inherited stdout until that fails, then says so in a file.
HELPER = """
import sys, time
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        sys.stdout.write("tick\\n")
        sys.stdout.flush()
    except OSError:
        open(sys.argv[1], "w").write("closed")
        break
    time.sleep(0.05)
"""

#: The program: starts the helper (which inherits stdout) and exits at once.
LAUNCHER = """
import subprocess, sys
from pathlib import Path
helper = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
Path(sys.argv[3]).write_text(str(helper.pid))
print("launched", flush=True)
"""


def _run_threads(before):
    return [t for t in threading.enumerate() if t not in before and t.name.startswith("program-")]


def test_a_clean_run_lets_go_of_the_output_a_helper_still_holds(tmp_path):
    helper, launcher = tmp_path / "helper.py", tmp_path / "launcher.py"
    helper.write_text(HELPER, encoding="utf-8")
    launcher.write_text(LAUNCHER, encoding="utf-8")
    marker, pid_file = tmp_path / "closed.txt", tmp_path / "helper.pid"
    before = set(threading.enumerate())

    code, out, error = programs.run_once(
        [PY, str(launcher), str(helper), str(marker), str(pid_file)], b"", timeout_s=30)
    helper_pid = int(pid_file.read_text())
    try:
        assert (code, error) == (0, "")
        assert b"launched" in out
        # No thread of the run is left reading...
        wait_until(lambda: not _run_threads(before), timeout=5)
        # ...and the pipe is closed: the helper's next write fails, and it
        # was still running to notice (nothing killed it).
        wait_until(lambda: marker.exists() and marker.read_text() == "closed", timeout=10)
    finally:
        kill_pid(helper_pid)


def test_output_is_still_read_in_full_and_quickly(monkeypatch):
    code, out, error = programs.run_once(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024))"],
        b"", timeout_s=30)
    assert (code, error) == (0, "")
    assert out == b"x" * (1024 * 1024)


def test_output_written_just_before_the_exit_is_kept():
    code, out, error = programs.run_once(
        [PY, "-c", "import sys, time; time.sleep(0.3); sys.stdout.write('last words')"],
        b"", timeout_s=30)
    assert (code, out, error) == (0, b"last words", "")


def test_a_timed_out_run_lets_go_too(tmp_path):
    before = set(threading.enumerate())
    started = time.monotonic()
    code, out, error = programs.run_once(
        [PY, "-c", "import time; print('working', flush=True); time.sleep(60)"],
        b"", timeout_s=1)
    assert (code, error) == (None, "Timed out after 1 s")
    assert out.strip() == b"working"
    assert time.monotonic() - started < 6
    wait_until(lambda: not _run_threads(before), timeout=5)


def test_the_helper_is_alive_when_run_once_returns(tmp_path):
    # Letting go of the output never stops the helper itself.
    helper, launcher = tmp_path / "helper.py", tmp_path / "launcher.py"
    helper.write_text("import time; time.sleep(30)", encoding="utf-8")
    launcher.write_text(LAUNCHER, encoding="utf-8")
    pid_file = tmp_path / "helper.pid"
    code, _out, error = programs.run_once(
        [PY, str(launcher), str(helper), "unused", str(pid_file)], b"", timeout_s=30)
    helper_pid = int(pid_file.read_text())
    try:
        assert (code, error) == (0, "")
        time.sleep(0.5)
        assert _alive(helper_pid)
    finally:
        kill_pid(helper_pid)
