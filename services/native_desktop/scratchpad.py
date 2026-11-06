"""Durable plain-text Scratchpad storage, independent of the desktop toolkit."""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_CHARS = 5_000_000
AUTOSAVE_SECONDS = 0.7


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".scratchpad-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ScratchpadStore:
    """Coalesce edits on one worker and drain the latest revision on close.

    A failed load never authorizes replacing unread notes with an empty file.
    A failed save retains the latest text and dirty state so flush or a later
    edit can retry. Neither log messages nor status callbacks contain notes.
    """

    def __init__(self, path=None, *, on_state=None, debounce=AUTOSAVE_SECONDS,
                 max_chars=MAX_CHARS, writer=None):
        if path is None:
            from config import config
            path = config.SCRATCHPAD_FILE
        self.path = Path(path)
        self._on_state = on_state
        self._debounce = debounce
        self._max_chars = max_chars
        self._writer = writer or _write_atomically
        self._condition = threading.Condition()
        self._thread = None
        self._text = ""
        self._sequence = self._written = self._failed = 0
        self._deadline = 0.0
        self._saving = self._closed = False
        self._closing = False
        self._load_error = self._error = ""
        self._load()

    def _load(self):
        try:
            if self.path.stat().st_size > self._max_chars * 4:
                raise ValueError("Saved notes exceed the Scratchpad size limit.")
            with self.path.open(encoding="utf-8", errors="replace", newline="") as stream:
                text = stream.read(self._max_chars + 1)
            if len(text) > self._max_chars:
                raise ValueError("Saved notes exceed the Scratchpad size limit.")
            self._text = text.replace("\r\n", "\n")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._load_error = (str(exc) if isinstance(exc, ValueError) else
                "Couldn't open your saved notes. Check the Scratchpad file permissions.")
            self._error = self._load_error
            logger.warning("Couldn't read the Scratchpad (%s)", type(exc).__name__)

    @property
    def text(self):
        with self._condition:
            return self._text

    def snapshot(self):
        with self._condition:
            return {"dirty": self._sequence > self._written, "saving": self._saving,
                "error": self._error, "loaded": not bool(self._load_error),
                "revision": self._sequence, "saved_revision": self._written}

    def _publish(self):
        if self._on_state is not None:
            try:
                self._on_state(self.snapshot())
            except Exception:
                logger.debug("Could not publish Scratchpad save status", exc_info=True)

    def set_text(self, text):
        if not isinstance(text, str) or len(text) > self._max_chars:
            raise ValueError(f"Scratchpad text is limited to {self._max_chars:,} characters.")
        with self._condition:
            if self._closed or self._closing:
                raise RuntimeError("The Scratchpad is closed.")
            if self._load_error:
                raise ValueError(self._load_error + " Your existing file was preserved.")
            if text == self._text:
                return
            self._text = text
            self._sequence += 1
            self._failed = 0
            self._error = ""
            self._deadline = time.monotonic() + self._debounce
            self._ensure_worker()
            self._condition.notify_all()
        self._publish()

    def _ensure_worker(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="native-scratchpad-save", daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    if self._sequence <= self._written or self._failed >= self._sequence:
                        self._condition.wait()
                        continue
                    wait = self._deadline - time.monotonic()
                    if wait > 0:
                        self._condition.wait(wait)
                        continue
                    sequence, text = self._sequence, self._text
                    self._saving = True
                    break
            self._publish()
            error = ""
            try:
                self._writer(self.path, text)
            except Exception as exc:
                error = "Couldn't save your notes. Check free space and Scratchpad file permissions."
                logger.warning("Couldn't save the Scratchpad (%s)", type(exc).__name__)
            with self._condition:
                self._saving = False
                if error:
                    self._failed = sequence
                    self._error = error
                else:
                    self._written = sequence
                    self._error = ""
                self._condition.notify_all()
            self._publish()

    def flush(self):
        with self._condition:
            if self._load_error:
                return False
            target = self._sequence
            if target <= self._written:
                return True
            if self._closed:
                return False
            self._failed = 0
            self._deadline = 0
            self._ensure_worker()
            self._condition.notify_all()
            while self._written < target and self._failed < target:
                self._condition.wait()
            return self._written >= target

    def close(self):
        with self._condition:
            self._closing = True
        saved = self.flush()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        return saved
