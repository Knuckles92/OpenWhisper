from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class SpeechProcess:
    """One serialized request stream; cancellation may interrupt it from any thread."""

    def __init__(self, python: str):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._serial = 0
        self._closed = False
        self._replies = queue.Queue()
        self._errors = deque(maxlen=25)
        environment = os.environ.copy()
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUTF8="1")
        self.process = subprocess.Popen(
            [python, "-u", str(Path(__file__).with_name("worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._readers = [
            threading.Thread(target=self._read, daemon=True, name="speech-results"),
            threading.Thread(target=self._read_errors, daemon=True, name="speech-errors"),
        ]
        for reader in self._readers:
            reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self._replies.put(json.loads(line))
                except ValueError:
                    self._errors.append(line.rstrip())
        finally:
            self._replies.put(None)

    def _read_errors(self):
        for line in self.process.stderr:
            self._errors.append(line.rstrip())

    def request(self, op: str, *, timeout=180., **payload) -> dict:
        with self._lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                self._serial += 1
                serial = self._serial
                try:
                    self.process.stdin.write(json.dumps(dict(id=serial, op=op, **payload)) + "\n")
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    raise RuntimeError("Speech worker stopped. Reload the engine.") from exc
            deadline = time.monotonic() + timeout
            while True:
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
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                if response.get("error"):
                    raise RuntimeError(response["error"])
                return response["result"]

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if self.process.poll() is None:
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        # Wake a request waiting for a response even if native code never wrote one.
        self._replies.put(None)
        for reader in self._readers:
            reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
