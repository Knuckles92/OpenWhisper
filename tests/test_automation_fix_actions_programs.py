"""run_once: what a program leaves running survives a clean exit; cancel stops it.

Real child processes (sys.executable -c …), like test_automation_hooks_programs.
"""

from __future__ import annotations

import threading
import time

from services.automation import programs
from tests.test_agent_process import _alive
from tests.test_automation_hooks_programs import PY, kill_pid, wait_until

#: Starts a helper that writes its pid, then a marker after a while, and exits at once.
LAUNCHER = (
    "import subprocess, sys; from pathlib import Path; "
    "helper = \"import sys, time; time.sleep(float(sys.argv[2])); "
    "open(sys.argv[1], 'w').write('ok')\"; "
    "p = subprocess.Popen([sys.executable, '-c', helper, sys.argv[1], sys.argv[3]]{redirect}); "
    "Path(sys.argv[2]).write_text(str(p.pid)); print('launched', flush=True)"
)


def _launch(tmp_path, *, holds_output: bool, delay: float = 2.5):
    marker, pid_file = tmp_path / "marker.txt", tmp_path / "helper.pid"
    redirect = "" if holds_output else ", stdout=subprocess.DEVNULL"
    code = LAUNCHER.format(redirect=redirect)
    started = time.monotonic()
    result = programs.run_once([PY, "-c", code, str(marker), str(pid_file), str(delay)],
                               b"", timeout_s=30)
    return result, time.monotonic() - started, marker, int(pid_file.read_text())


def test_an_app_the_program_opened_outlives_its_clean_exit(tmp_path):
    (code, out, error), elapsed, marker, helper = _launch(tmp_path, holds_output=False)
    try:
        assert (code, out.strip(), error) == (0, b"launched", "")
        assert elapsed < programs.OUTPUT_GRACE_S + 3
        # The helper finishes its own work after run_once returned.
        wait_until(lambda: marker.exists(), timeout=15)
    finally:
        kill_pid(helper)


def test_a_helper_holding_the_output_open_survives_too(tmp_path):
    # It inherited stdout, so run_once stops waiting for the output after the grace.
    (code, out, error), elapsed, marker, helper = _launch(tmp_path, holds_output=True)
    try:
        assert (code, out.strip(), error) == (0, b"launched", "")
        assert elapsed < programs.OUTPUT_GRACE_S + 3
        assert _alive(helper)
        wait_until(lambda: marker.exists(), timeout=15)
    finally:
        kill_pid(helper)


def test_cancel_stops_the_program_and_everything_it_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); print('started', flush=True); time.sleep(60)"
    )
    cancel = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(programs.run_once(
        [PY, "-c", code, str(pid_file)], b"", timeout_s=60, cancel=cancel)))
    worker.start()
    wait_until(lambda: pid_file.exists() and pid_file.read_text())
    child = int(pid_file.read_text())
    try:
        canceled_at = time.monotonic()
        cancel.set()
        worker.join(10)
        assert not worker.is_alive()
        assert time.monotonic() - canceled_at < 5
        ((code_, out, error),) = results
        assert (code_, error) == (None, "Canceled")
        assert out.strip() == b"started"
        wait_until(lambda: not _alive(child), timeout=5)
    finally:
        kill_pid(child)


def test_a_cancel_before_the_start_runs_nothing(tmp_path):
    marker = tmp_path / "ran.txt"
    cancel = threading.Event()
    cancel.set()
    result = programs.run_once(
        [PY, "-c", "import sys; open(sys.argv[1], 'w').write('x')", str(marker)],
        b"", timeout_s=20, cancel=cancel)
    assert result == (None, b"", "Canceled")
    time.sleep(0.3)
    assert not marker.exists()
