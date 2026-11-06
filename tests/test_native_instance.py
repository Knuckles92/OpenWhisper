"""Exercise the native portable lock/activation path on the available host OS."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

from services.native_desktop import instance
from services import ui_instance


ROOT = Path(__file__).resolve().parents[1]


def _peer(folder):
    result = subprocess.run(
        [sys.executable, "-c", "from services.native_desktop.instance import acquire; "
         "import sys; item=acquire(sys.argv[1]); "
         "print('PRIMARY' if item else 'SECONDARY'); item and item.close()", str(folder)],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_portable_lock_activates_primary_and_releases_for_next_process(tmp_path):
    primary = instance.acquire(tmp_path)
    shown = threading.Event()
    primary.attach_activate(shown.set)
    try:
        assert _peer(tmp_path) == "SECONDARY"
        assert shown.wait(1)
    finally:
        primary.close()
    assert _peer(tmp_path) == "PRIMARY"


def test_activation_requires_token_and_rotates_between_owners(tmp_path):
    primary = instance.acquire(tmp_path)
    path = tmp_path / ".openwhisper-native-ui.json"
    metadata = json.loads(path.read_text())
    shown = threading.Event()
    primary.attach_activate(shown.set)
    try:
        with socket.create_connection(("127.0.0.1", metadata["port"]), timeout=1) as connection:
            connection.sendall(b"incorrect-token show\n")
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(3) == b""
        assert not shown.is_set()
        assert instance._activate(path)
        assert shown.wait(1)
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        primary.close()
    next_owner = instance.acquire(tmp_path)
    try:
        assert json.loads(path.read_text())["token"] != metadata["token"]
    finally:
        next_owner.close()


def test_hung_primary_never_allows_duplicate_capture(tmp_path):
    primary = instance.acquire(tmp_path)
    try:
        try:
            primary.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        primary.listener.close()
        assert _peer(tmp_path) == "SECONDARY"
    finally:
        primary.close()


def test_native_non_linux_entrypoint_uses_portable_instance(monkeypatch, tmp_path):
    ui_instance.release_ui_instance()
    monkeypatch.setenv("OPENWHISPER_DESKTOP_RUNTIME", "native")
    monkeypatch.setattr(ui_instance.sys, "platform", "darwin")
    try:
        primary = ui_instance.acquire_ui_instance(str(tmp_path))
        assert isinstance(primary, instance.NativeInstance)
        assert (tmp_path / ".openwhisper-native-ui.json").is_file()
    finally:
        ui_instance.release_ui_instance()


def test_windows_lock_adapter_locks_first_byte_from_offset_zero(monkeypatch, tmp_path):
    calls = []
    windows_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    windows_os.name = "nt"
    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda fd, kind, count:
        calls.append((os.lseek(fd, 0, os.SEEK_CUR), kind, count)))
    monkeypatch.setattr(instance, "os", windows_os)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    primary = instance.acquire(tmp_path)
    try:
        assert calls == [(0, fake_msvcrt.LK_NBLCK, 1)]
        assert (tmp_path / ".openwhisper-native-ui.lock").read_bytes() == b" "
    finally:
        primary.close()
