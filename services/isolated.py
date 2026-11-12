"""Cancelable process-backed adapters used by the existing backend interfaces."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from services.local_asr.process import SpeechProcess

logger = logging.getLogger(__name__)

#: A warmed upload worker that no upload took is closed after this long.
STANDBY_IDLE_S = 600.0
#: The longest a warm-up may take; an import this slow has failed.
_WARM_TIMEOUT_S = 60.0


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


class _Standby:
    """A worker being readied for the next upload, and how that went."""

    def __init__(self, worker):
        self.worker = worker
        #: Set once the warm-up has ended, whichever way.
        self.ready = threading.Event()
        self.ok = False
        self.expiry = None


class IsolatedOpenAIClient:
    """Small SDK-shaped adapter; each upload owns a killable worker process.

    ``base_url`` None is OpenAI. ``read_timeout`` bounds the wait for the
    server's answer; the worker gets a minute more to connect and upload.

    ``warm`` readies the next upload's worker ahead of it. An upload without
    one starts its own, as every upload used to.
    """

    def __init__(self, api_key, base_url=None, read_timeout=120.):
        self._api_key = api_key
        self._base_url = base_url
        self._read_timeout = read_timeout
        self._lock = threading.Lock()
        self._closed = False
        self._workers = set()
        self._standby = None
        self.audio = SimpleNamespace(transcriptions=self)

    def warm(self) -> None:
        """Start the next upload's worker and build its client in it. Blocking.

        An upload that starts from nothing spends 1.3 s before a byte leaves:
        0.16 s starting the process, 0.7 s importing the OpenAI SDK, and
        0.45 s building the client and on the SDK's first use (November
        2026; with the worker ready, a 10 s recording reached a server on
        the same computer in 0.02 s). Called when a recording starts, that
        happens while the user speaks.

        Nothing is sent anywhere: the worker loads the SDK, builds its client
        and waits. One that no upload takes is closed after
        ``STANDBY_IDLE_S``.
        """
        with self._lock:
            if self._closed:
                return
            stale = self._standby
            if stale is not None and stale.worker.process.poll() is None:
                return
            standby = _Standby(SpeechProcess(sys.executable, isolated=True))
            self._standby = standby
            self._workers.add(standby.worker)
            if stale is not None:
                self._workers.discard(stale.worker)
        if stale is not None:
            if stale.expiry is not None:
                stale.expiry.cancel()
            stale.worker.close()
        try:
            standby.worker.request(
                "openai_prepare", api_key=self._api_key, base_url=self._base_url,
                read_timeout=self._read_timeout, timeout=_WARM_TIMEOUT_S)
            standby.ok = True
        except Exception as exc:
            # Type only: the SDK's errors can quote the server's address.
            logger.info("Upload worker warm-up failed (%s); the upload starts its own",
                        type(exc).__name__)
        finally:
            standby.ready.set()
        if not standby.ok:
            self._retire(standby)
            return
        with self._lock:
            if self._standby is standby:
                standby.expiry = threading.Timer(STANDBY_IDLE_S, self._retire, args=(standby,))
                standby.expiry.daemon = True
                standby.expiry.start()

    def _retire(self, standby: _Standby) -> None:
        """Close a standby no upload took: its warm-up failed, or it sat unused."""
        with self._lock:
            if self._standby is not standby:
                return
            self._standby = None
            self._workers.discard(standby.worker)
        standby.worker.close()

    def _upload_worker(self):
        """The worker for one upload: the warmed one when it is ready, else a new one."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Transcription canceled")
            standby, self._standby = self._standby, None
            if standby is None:
                return self._start_worker_locked()
            if standby.expiry is not None:
                standby.expiry.cancel()
        # A warm-up still under way is further along than a new worker would
        # be, and close() ends the wait by killing it.
        standby.ready.wait(_WARM_TIMEOUT_S)
        usable = standby.ok and standby.worker.process.poll() is None
        if not usable:
            standby.worker.close()
        with self._lock:
            if self._closed:
                raise RuntimeError("Transcription canceled")
            if usable:
                return standby.worker
            self._workers.discard(standby.worker)
            return self._start_worker_locked()

    def _start_worker_locked(self):
        worker = SpeechProcess(sys.executable, isolated=True)
        self._workers.add(worker)
        return worker

    def create(self, *, model, file, response_format, prompt=None, language=None,
               languages=None, keywords=None):
        hints = {name: value for name, value in (
            ("prompt", prompt), ("language", language),
            ("languages", languages), ("keywords", keywords),
        ) if value}
        worker = self._upload_worker()
        try:
            return worker.request("openai_transcribe", api_key=self._api_key,
                                  base_url=self._base_url, read_timeout=self._read_timeout,
                                  audio_path=os.path.abspath(file.name), model=model,
                                  response_format=response_format,
                                  timeout=self._read_timeout + 60., **hints)["text"]
        finally:
            worker.close()
            with self._lock:
                self._workers.discard(worker)

    def close(self):
        with self._lock:
            self._closed = True
            workers = list(self._workers)
            standby, self._standby = self._standby, None
        if standby is not None and standby.expiry is not None:
            standby.expiry.cancel()
        # Signal all uploads before waiting for any one worker to unwind.
        for worker in workers:
            if worker.process.poll() is None:
                worker.process.terminate()
        for worker in workers:
            worker.close()
