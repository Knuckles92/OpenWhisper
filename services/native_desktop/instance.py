"""One native audio service per data root, with authenticated local activation."""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import tempfile
from pathlib import Path


class NativeInstance:
    def __init__(self, lock, listener, token):
        self.lock, self.listener, self.token = lock, listener, token
        self.callback = None
        self.pending = False
        self.closed = False
        self.worker = threading.Thread(target=self._accept, name="native-instance", daemon=True)
        self.worker.start()

    def attach_activate(self, callback):
        self.callback = callback
        if self.pending:
            callback()

    def _accept(self):
        while not self.closed:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                break
            with connection:
                connection.settimeout(0.5)
                try:
                    packet = b""
                    while b"\n" not in packet and len(packet) <= 256:
                        chunk = connection.recv(256)
                        if not chunk:
                            break
                        packet += chunk
                    if secrets.compare_digest(packet, (self.token + " show\n").encode("ascii")):
                        self.pending = True
                        if self.callback:
                            self.callback()
                        connection.sendall(b"ok\n")
                except OSError:
                    pass

    def close(self):
        self.closed = True
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.lock.close()  # Both flock and Windows byte locks follow the handle.


def _activate(path):
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        port, token = metadata["port"], metadata["token"]
        if type(port) is not int or not 0 < port < 65536 or not isinstance(token, str) or len(token) != 64:
            return False
        with socket.create_connection(("127.0.0.1", port), timeout=0.4) as connection:
            connection.sendall((token + " show\n").encode("ascii"))
            return connection.recv(3) == b"ok\n"
    except (OSError, ValueError, KeyError, TypeError):
        return False


def acquire(data_dir):
    path = Path(data_dir) / ".openwhisper-native-ui.lock"
    metadata_path = path.with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("The native UI lock is a symbolic link")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    lock = os.fdopen(descriptor, "r+b", buffering=0)
    listener = None
    try:
        if os.fstat(descriptor).st_size == 0:
            lock.write(b" ")
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if _activate(metadata_path):
                    return None
                time.sleep(0.05)
            return None  # A hung primary must never result in duplicate capture.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        token = secrets.token_hex(32)
        metadata = json.dumps({"pid": os.getpid(), "port": listener.getsockname()[1], "token": token}).encode("utf-8")
        temp_descriptor, temp_path = tempfile.mkstemp(prefix=".native-ui-", suffix=".next", dir=path.parent)
        try:
            with os.fdopen(temp_descriptor, "wb") as temporary:
                temporary.write(metadata)
            os.replace(temp_path, metadata_path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        return NativeInstance(lock, listener, token)
    except Exception:
        if listener is not None:
            listener.close()
        lock.close()
        raise
