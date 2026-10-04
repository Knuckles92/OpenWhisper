"""Cross-process startup isolation for a staged data restore."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from services import backup
from services.backup_startup import (
    DataLease,
    DataLeaseBusy,
    StartupRestoreError,
    acquire_startup_data_lease,
    release_startup_data_lease,
    restart_command,
)


ROOT = Path(__file__).resolve().parents[1]


def _reader_process(data_dir: Path) -> subprocess.Popen:
    script = (
        "import sys\n"
        "from services.backup_startup import DataLease\n"
        "with DataLease.acquire(sys.argv[1]):\n"
        "    print('READY', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    options = {
        "cwd": str(ROOT),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([sys.executable, "-u", "-c", script, str(data_dir)], **options)
    ready: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        assert ready.get(timeout=10).strip() == "READY", process.stderr.read()
    except Exception:
        process.kill()
        process.wait(timeout=10)
        raise
    return process


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def test_shared_processes_coexist_and_exclusive_waits_for_exit(tmp_path):
    other = _reader_process(tmp_path)
    try:
        with DataLease.acquire(str(tmp_path)):
            with pytest.raises(DataLeaseBusy):
                DataLease.acquire(str(tmp_path), exclusive=True)
    finally:
        _stop(other)

    # Windows may finish releasing a terminated process's region a moment
    # after wait() reports its exit.
    deadline = time.monotonic() + 2
    while True:
        try:
            exclusive = DataLease.acquire(str(tmp_path), exclusive=True)
            break
        except DataLeaseBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    with exclusive:
        with pytest.raises(DataLeaseBusy):
            DataLease.acquire(str(tmp_path))


def test_pending_restore_never_runs_while_another_process_uses_data(tmp_path, monkeypatch):
    release_startup_data_lease()
    marker = tmp_path / "pending-test"
    marker.write_text("pending", encoding="utf-8")
    applied = []

    monkeypatch.setattr(backup, "pending_restore_exists", lambda root: marker.exists())

    def apply(*, data_dir):
        applied.append(str(data_dir))
        marker.unlink()
        return True

    monkeypatch.setattr(backup, "apply_pending_restore", apply)
    other = _reader_process(tmp_path)
    try:
        with pytest.raises(StartupRestoreError, match="Close every OpenWhisper window"):
            acquire_startup_data_lease(str(tmp_path))
        assert marker.exists()
        assert not applied
    finally:
        _stop(other)

    try:
        lease = acquire_startup_data_lease(str(tmp_path))
        assert lease.exclusive is False
        assert applied == [os.path.normcase(os.path.realpath(tmp_path))]
        with pytest.raises(DataLeaseBusy):
            DataLease.acquire(str(tmp_path), exclusive=True)
    finally:
        release_startup_data_lease()


def test_normal_startup_holds_shared_lease_without_restore(tmp_path, monkeypatch):
    release_startup_data_lease()
    monkeypatch.setattr(backup, "pending_restore_exists", lambda root: False)
    monkeypatch.setattr(backup, "apply_pending_restore", lambda **kw: pytest.fail("unexpected restore"))
    try:
        assert acquire_startup_data_lease(str(tmp_path)).exclusive is False
        with DataLease.acquire(str(tmp_path)):
            pass
        with pytest.raises(DataLeaseBusy):
            DataLease.acquire(str(tmp_path), exclusive=True)
    finally:
        release_startup_data_lease()


def test_main_gates_restore_after_worker_modes_before_ui_import():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    gate = source.index("_APP_DATA_LEASE = acquire_startup_data_lease(data_root())")
    assert source.index("_handle_early_cli()") < gate
    assert source.index("_handle_package_self_test()\n\n# A restore") < gate
    assert gate < source.index("from ui_qt.bootstrap import main")


def test_restart_command_drops_update_and_restore_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--update-health-token", "old"])
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert restart_command(str(ROOT / "main.py")) == [
        sys.executable, str((ROOT / "main.py").resolve()),
    ]
