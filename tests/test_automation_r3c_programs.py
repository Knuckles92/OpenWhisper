"""run_once lets go of a clean run's stdin too, when a process the program left
running holds it without reading.

Real child processes, like test_automation_r2c_programs.
"""

from __future__ import annotations

import threading

from services.automation import programs
from tests.test_agent_process import _alive
from tests.test_automation_hooks_programs import PY, kill_pid, wait_until

#: The program: starts the helper, which inherits its stdin (not its stdout),
#: and exits at once without reading anything.
LAUNCHER = """
import subprocess, sys
from pathlib import Path
helper = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[3]).write_text(str(helper.pid))
"""
#: Holds the inherited stdin without reading it.
IDLE_HELPER = "import time; time.sleep(30)"
#: Reads the inherited stdin to its end and says how much there was.
READING_HELPER = """
import sys
data = sys.stdin.buffer.read()
open(sys.argv[1], "w").write(str(len(data)))
"""

#: More than any pipe buffer (4 KB on Windows, 64 KiB on Linux).
PAYLOAD = b"x" * (1024 * 1024)


def _stdin_threads(before):
    return [t for t in threading.enumerate() if t not in before and t.name == "program-stdin"]


def _run(tmp_path, helper_source, payload):
    helper, launcher = tmp_path / "helper.py", tmp_path / "launcher.py"
    helper.write_text(helper_source, encoding="utf-8")
    launcher.write_text(LAUNCHER, encoding="utf-8")
    pid_file, marker = tmp_path / "helper.pid", tmp_path / "read.txt"
    result = programs.run_once(
        [PY, str(launcher), str(helper), str(marker), str(pid_file)], payload, timeout_s=30)
    return result, int(pid_file.read_text()), marker


def test_a_clean_run_lets_go_of_the_input_a_helper_holds_without_reading(tmp_path):
    before = set(threading.enumerate())
    (code, _out, error), helper_pid, _marker = _run(tmp_path, IDLE_HELPER, PAYLOAD)
    try:
        assert (code, error) == (0, "")
        # The writer stops instead of waiting for the helper to read or exit...
        wait_until(lambda: not _stdin_threads(before), timeout=5)
        # ...and nothing stopped the helper.
        assert _alive(helper_pid)
    finally:
        kill_pid(helper_pid)


def test_a_helper_still_gets_the_input_it_reads_in_time(tmp_path, monkeypatch):
    # A program may hand its stdin on and exit (cmd's start /b); what the
    # helper reads before the run lets go still reaches it in full.
    monkeypatch.setattr(programs, "OUTPUT_GRACE_S", 15.0)  # slow machines start Python slowly
    payload = b"y" * 200_000
    (code, _out, error), helper_pid, marker = _run(tmp_path, READING_HELPER, payload)
    try:
        assert (code, error) == (0, "")
        wait_until(lambda: marker.exists() and marker.read_text() == str(len(payload)),
                   timeout=15)
    finally:
        kill_pid(helper_pid)


def test_a_program_that_reads_its_input_gets_all_of_it():
    code, out, error = programs.run_once(
        [PY, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"],
        PAYLOAD, timeout_s=30)
    assert (code, out, error) == (0, str(len(PAYLOAD)).encode(), "")
