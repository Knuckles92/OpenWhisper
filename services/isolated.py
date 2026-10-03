"""Cancelable process-backed adapters used by the existing backend interfaces."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from services.local_asr.process import SpeechProcess


def cleanup_orphaned_preview_audio():
    """Remove only worker audio belonging to app processes that no longer exist."""
    for path in Path(tempfile.gettempdir()).glob("whisper-worker-*-*.npy"):
        parts = path.name.split("-")
        if len(parts) < 4 or not parts[2].isdigit():
            continue
        if _process_exists(int(parts[2])):
            continue
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass


def _process_exists(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is not proof of death.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _CallbackCancel:
    def __init__(self, callback):
        self._callback = callback

    def is_set(self):
        return self._callback is not None and self._callback()


class IsolatedWhisperModel:
    """Preserve model.transcribe for dictation, previews, and remote hosting.

    One process owns the native model. Cancel kills that process; the next
    request lazily reloads it. Serialized calls never mix replies after cancel.
    """

    def __init__(self, model, **options):
        self._model = model
        self._options = options
        self._process = None
        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._generation = 0
        self._closed = False

    def _worker(self, generation, cancel=None):
        with self._state_lock:
            if self._closed or generation != self._generation:
                raise RuntimeError("Transcription canceled")
            process = self._process
            if process is not None:
                return process
            process = SpeechProcess(sys.executable, isolated=True)
            self._process = process
        try:
            process.request("whisper_load", model=self._model, options=self._options, timeout=300., cancel=cancel)
        except BaseException:
            process.close()
            with self._state_lock:
                if self._process is process:
                    self._process = None
            raise
        return process

    def load(self, cancel=None):
        with self._state_lock:
            generation = self._generation
        with self._request_lock:
            self._worker(generation, cancel)

    def transcribe(self, audio, **options):
        return self.transcribe_cancelable(audio, should_cancel=None, **options)

    def transcribe_cancelable(self, audio, *, should_cancel, **options):
        cancel = _CallbackCancel(should_cancel)
        with self._state_lock:
            generation = self._generation
        temporary = None
        try:
            if cancel.is_set():
                raise RuntimeError("Transcription canceled")
            if isinstance(audio, (str, os.PathLike)):
                path = os.fspath(audio)
            else:
                import numpy as np
                fd, temporary = tempfile.mkstemp(prefix=f"whisper-worker-{os.getpid()}-", suffix=".npy")
                with os.fdopen(fd, "wb") as output:
                    np.save(output, np.asarray(audio, dtype=np.float32), allow_pickle=False)
                path = temporary
            with self._request_lock:
                if cancel.is_set():
                    raise RuntimeError("Transcription canceled")
                process = self._worker(generation, cancel)
                try:
                    result = process.request("whisper_transcribe", audio_path=path,
                                             numpy_audio=temporary is not None, options=options,
                                             timeout=7200., cancel=cancel)
                except BaseException:
                    # Timeout/native crash also has a recoverable next attempt.
                    self.cancel()
                    raise
            return (iter(SimpleNamespace(**s) for s in result["segments"]),
                    SimpleNamespace(**result["info"]))
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass  # The worker removes the audio immediately after reading.

    def cancel(self):
        with self._state_lock:
            self._generation += 1
            process, self._process = self._process, None
        if process is not None:
            process.close()

    def close(self):
        with self._state_lock:
            self._closed = True
        self.cancel()


class IsolatedOpenAIClient:
    """Small SDK-shaped adapter; each upload owns a killable worker process."""

    def __init__(self, api_key):
        self._api_key = api_key
        self._lock = threading.Lock()
        self._closed = False
        self._workers = set()
        self.audio = SimpleNamespace(transcriptions=self)

    def create(self, *, model, file, response_format):
        with self._lock:
            if self._closed:
                raise RuntimeError("Transcription canceled")
            worker = SpeechProcess(sys.executable, isolated=True)
            self._workers.add(worker)
        try:
            return worker.request("openai_transcribe", api_key=self._api_key,
                                  audio_path=os.path.abspath(file.name), model=model,
                                  response_format=response_format, timeout=180.)["text"]
        finally:
            worker.close()
            with self._lock:
                self._workers.discard(worker)

    def close(self):
        with self._lock:
            self._closed = True
            workers = list(self._workers)
        # Signal all uploads before waiting for any one worker to unwind.
        for worker in workers:
            if worker.process.poll() is None:
                worker.process.terminate()
        for worker in workers:
            worker.close()
