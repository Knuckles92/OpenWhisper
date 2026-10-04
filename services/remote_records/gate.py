"""Drain active host record requests before taking a stable backup."""

from contextlib import contextmanager
import threading
import time


class RecordsBusy(RuntimeError):
    pass


class RecordGate:
    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0
        self._paused = False

    @contextmanager
    def request(self):
        with self._condition:
            if self._paused:
                raise RecordsBusy("The host is backing up or recovering records. Try again shortly.")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @contextmanager
    def paused(self, timeout=30):
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._paused:
                raise RecordsBusy("Another backup or record recovery is already running.")
            self._paused = True
            try:
                while self._active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RecordsBusy("Host record transfers are still finishing. Try again after sync finishes.")
                    self._condition.wait(remaining)
            except BaseException:
                self._paused = False
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._paused = False
                self._condition.notify_all()
