from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import weakref

_workers = weakref.WeakSet()
_workers_lock = threading.Lock()


def close_all_workers():
    """Interrupt workers owned by preview, remote-host, and download paths too."""
    with _workers_lock:
        workers = list(_workers)
    for worker in workers:
        worker.close()


class SpeechProcess:
    """One serialized request stream; cancellation may interrupt it from any thread."""

    def __init__(self, python: str, *, isolated: bool = False):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._serial = 0
        self._closed = False
        self._connected = threading.Event()
        self._listener = None
        self._connection = None
        self._input = self._output = None
        self._replies = queue.Queue()
        self._errors = deque(maxlen=25)
        environment = os.environ.copy()
        environment.update(PYTHONUTF8="1")
        if not isolated:
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        command = [python, "-u", str(Path(__file__).with_name("worker.py"))]
        # Off Windows the worker runs on the app's own interpreter; a frozen
        # app has no python to hand a script to, so it relaunches itself.
        if sys.platform != "win32" and getattr(sys, "frozen", False):
            command = [python, "--local-asr-worker"]
        if isolated:
            # Frozen Windows GUI executables have no sys.stdin/stdout even
            # with Popen pipes. An authenticated loopback socket works for
            # source and packaged workers without a visible console window.
            self._listener = socket.socket()
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(1)
            self._listener.settimeout(.2)
            self._token = secrets.token_hex(32)
            environment["OPENWHISPER_WORKER_PORT"] = str(self._listener.getsockname()[1])
            environment["OPENWHISPER_WORKER_TOKEN"] = self._token
            command = ([python, "--isolated-worker"] if getattr(sys, "frozen", False)
                       else [python, "-u", str(Path(__file__).resolve().parents[2] / "main.py"),
                             "--isolated-worker"])
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            if self._listener is not None:
                self._listener.close()
            raise
        if not isolated:
            self._input, self._output = self.process.stdin, self.process.stdout
            self._connected.set()
        self._readers = [
            threading.Thread(target=self._read, daemon=True, name="speech-results"),
            threading.Thread(target=self._read_errors, daemon=True, name="speech-errors"),
        ]
        for reader in self._readers:
            reader.start()
        with _workers_lock:
            _workers.add(self)

    def _read(self):
        try:
            if self._listener is not None:
                self._accept_worker()
            if self._closed or self._output is None:
                return
            for line in self._output:
                try:
                    self._replies.put(json.loads(line))
                except ValueError:
                    self._errors.append(line.rstrip())
        except (OSError, ValueError):
            if not self._closed:
                self._errors.append("Worker connection was interrupted")
        finally:
            self._connected.set()
            self._replies.put(None)

    def _accept_worker(self):
        while not self._closed and self.process.poll() is None:
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection.settimeout(1.)
            try:
                # Fixed-size token only; don't read arbitrary unauthenticated
                # lines into memory or reveal requests before verification.
                received = bytearray()
                while len(received) < len(self._token):
                    block = connection.recv(len(self._token) - len(received))
                    if not block:
                        break
                    received.extend(block)
                if not secrets.compare_digest(bytes(received), self._token.encode("ascii")):
                    connection.close()
                    continue
            except OSError:
                connection.close()
                continue
            connection.settimeout(None)
            with self._state_lock:
                if self._closed:
                    connection.close()
                    return
                self._connection = connection
                self._input = connection.makefile("w", encoding="utf-8", buffering=1)
                self._output = connection.makefile("r", encoding="utf-8")
                self._connected.set()
            return

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                self._errors.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def recent_errors(self, lines: int = 12) -> str:
        """The worker's last stderr lines, such as the traceback of a failed request."""
        return "\n".join(list(self._errors)[-lines:])

    def request(self, op: str, *, timeout=180., cancel=None, progress=None, **payload) -> dict:
        with self._lock:
            deadline = time.monotonic() + timeout
            while not self._connected.wait(.05):
                if self._closed or (cancel is not None and cancel.is_set()):
                    self.close()
                    raise RuntimeError("Operation canceled")
                if time.monotonic() >= deadline:
                    self.close()
                    raise RuntimeError("Speech engine timed out and was stopped. Reload the engine.")
            if cancel is not None and cancel.is_set():
                self.close()
                raise RuntimeError("Operation canceled")
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                if self._input is None:
                    raise RuntimeError("Speech worker stopped: " + " ".join(self._errors)[-1600:])
                self._serial += 1
                serial = self._serial
                try:
                    self._input.write(json.dumps(dict(id=serial, op=op, **payload)) + "\n")
                    self._input.flush()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    raise RuntimeError("Speech worker stopped. Reload the engine.") from exc
            while True:
                if cancel is not None and cancel.is_set():
                    self.close()
                    raise RuntimeError("Operation canceled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise RuntimeError("Speech engine timed out and was stopped. Reload the engine.")
                try:
                    response = self._replies.get(timeout=min(.2, remaining))
                except queue.Empty:
                    if self._closed:
                        raise RuntimeError("Transcription canceled")
                    continue
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                if response is None:
                    raise RuntimeError("Speech worker stopped: " + " ".join(self._errors)[-1600:])
                if response.get("id") != serial:
                    continue
                if "progress" in response:
                    if progress is not None:
                        progress(*response["progress"])
                    continue
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                if response.get("error"):
                    error = RuntimeError(response["error"])
                    error.status_code = response.get("status_code")
                    error.code = response.get("code")
                    raise error
                return response["result"]

    def close(self):
        """Terminate and reap within a two-second budget, including stuck native calls."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if self._listener is not None:
                self._listener.close()
            if self._connection is not None:
                try:
                    self._connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if self.process.poll() is None:
                self.process.terminate()
        try:
            self.process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=.5)
        # Wake a request waiting for a response even if native code never wrote one.
        self._replies.put(None)
        for reader in self._readers:
            reader.join(timeout=.1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
        if self._connection is not None:
            for pipe in (self._input, self._output):
                pipe.close()
            self._connection.close()
        with _workers_lock:
            _workers.discard(self)
