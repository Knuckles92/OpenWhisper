"""One desktop UI process per data directory on Linux."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from services import ui_instance


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _release_ui_instance():
    ui_instance.release_ui_instance()
    yield
    ui_instance.release_ui_instance()


def test_non_linux_stays_primary_without_locking(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_instance.sys, "platform", "win32")
    first = ui_instance.acquire_ui_instance(str(tmp_path))
    assert first is not None
    assert first._sock is None
    assert ui_instance.acquire_ui_instance(str(tmp_path / "other")) is first


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UI single-instance locking is Linux-only",
)
def test_second_launch_asks_the_first_to_show_and_does_not_become_primary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    shown = threading.Event()
    primary = ui_instance.acquire_ui_instance(str(tmp_path / "data"))
    assert primary is not None
    primary.attach_activate(shown.set)

    assert _peer_outcome(tmp_path / "data", tmp_path / "run") == "SECONDARY"
    assert shown.wait(2.0)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UI single-instance locking is Linux-only",
)
def test_a_show_request_calls_the_activate_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    shown = threading.Event()
    primary = ui_instance.acquire_ui_instance(str(tmp_path / "data"))
    assert primary is not None
    primary.attach_activate(shown.set)
    _lock_path, sock_name = ui_instance.runtime_paths(str(tmp_path / "data"))
    assert ui_instance.request_show(sock_name)
    assert shown.wait(2.0)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UI single-instance locking is Linux-only",
)
def test_different_data_directories_can_each_have_a_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    first = ui_instance.acquire_ui_instance(str(tmp_path / "one"))
    other = _hold_primary(tmp_path / "two", tmp_path / "run")
    try:
        assert first is not None
        assert other.outcome == "PRIMARY"
    finally:
        _stop(other)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UI single-instance locking is Linux-only",
)
def test_a_new_process_can_become_primary_after_release(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    first = ui_instance.acquire_ui_instance(str(tmp_path / "data"))
    assert first is not None
    ui_instance.release_ui_instance()
    assert _peer_outcome(tmp_path / "data", tmp_path / "run") == "PRIMARY"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UI single-instance locking is Linux-only",
)
def test_lock_holder_without_a_socket_blocks_a_second_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(ui_instance, "_HANDOFF_SECONDS", 0.2)
    lock_path, _sock_name = ui_instance.runtime_paths(str(tmp_path / "data"))
    lock = open(lock_path, "a+b")
    try:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ui_instance.acquire_ui_instance(str(tmp_path / "data")) is None
    finally:
        lock.close()


def _peer_outcome(data_dir: Path, runtime_dir: Path) -> str:
    script = (
        "import sys\n"
        "from services.ui_instance import acquire_ui_instance\n"
        "instance = acquire_ui_instance(sys.argv[1])\n"
        "print('PRIMARY' if instance is not None else 'SECONDARY', flush=True)\n"
        "if instance is not None:\n"
        "    instance.close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-u", "-c", script, str(data_dir)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _hold_primary(data_dir: Path, runtime_dir: Path) -> subprocess.Popen:
    script = (
        "import sys\n"
        "from services.ui_instance import acquire_ui_instance\n"
        "instance = acquire_ui_instance(sys.argv[1])\n"
        "print('PRIMARY' if instance is not None else 'SECONDARY', flush=True)\n"
        "sys.stdin.read()\n"
    )
    options = {
        "cwd": str(ROOT),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": {**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
    }
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(data_dir)], **options
    )
    ready: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(
        target=lambda: ready.put(process.stdout.readline()), daemon=True
    ).start()
    try:
        process.outcome = ready.get(timeout=10).strip()
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
