"""Bounded capture writer and recovery of interrupted Quick Record sessions.

The audio callback only enqueues owned PCM bytes. Filesystem work and periodic
durability checkpoints belong to the writer. A session stays recoverable until
its consumer explicitly acknowledges persistence, or the user cancels it.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import wave
from datetime import datetime

logger = logging.getLogger(__name__)
QUEUE_BLOCKS = 512
CHECKPOINT_SECONDS = 1.0
FINISH_TIMEOUT = 3.0
COPY_BYTES = 1024 * 1024


def recovery_directory(output_file: str) -> Path:
    output = Path(output_file).resolve()
    return output.parent / ('.' + output.stem + '-recovery')


class _Lease:
    """An OS lock, released even after a hard kill; no stale PID heuristics."""

    def __init__(self, path: Path):
        self.handle = open(path, 'a+b')
        try:
            self.handle.seek(0)
            if not self.handle.read(1):
                self.handle.write(b'0')
                self.handle.flush()
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.handle.close()
            raise

    def close(self):
        self.handle.close()


class RecordingJournal:
    def __init__(self, output_file, rate, channels, sample_width, on_error, *,
                 queue_blocks=QUEUE_BLOCKS):
        self.directory = recovery_directory(output_file) / uuid.uuid4().hex
        self.directory.mkdir(parents=True, mode=0o700)
        self._lease = _Lease(self.directory / 'active.lock')
        self.path = self.directory / 'audio.pcm'
        self.frame_bytes = channels * sample_width
        self._on_error = on_error
        self._queue = queue.Queue(maxsize=queue_blocks)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._discard = False
        self._release_lock = threading.Lock()
        self._released = False
        self.error = None
        self.written_bytes = 0
        self.dropped_frames = 0
        try:
            metadata = dict(version=1, rate=rate, channels=channels,
                            sample_width=sample_width, created=datetime.now().isoformat())
            with open(self.directory / 'session.json', 'x', encoding='utf-8') as handle:
                json.dump(metadata, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self._file = open(self.path, 'x+b', buffering=0)
        except Exception:
            self._lease.close()
            raise
        self._thread = threading.Thread(target=self._run, name='dictation-writer', daemon=True)
        self._thread.start()

    def append(self, payload: bytes) -> bool:
        if self._stop.is_set() or self.error:
            return False
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self.dropped_frames += len(payload) // self.frame_bytes
            # Notification is handled by the writer, never by PortAudio.
            self.error = 'Recording storage cannot keep up; recording stopped.'
            self._stop.set()
            return False

    def _run(self):
        checkpoint = time.monotonic()
        try:
            while True:
                if self._discard:
                    break
                try:
                    payload = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                try:
                    written = self._file.write(payload)
                    self.written_bytes += written or 0
                    if written != len(payload):
                        raise OSError('Incomplete recording write')
                    if time.monotonic() - checkpoint >= CHECKPOINT_SECONDS:
                        os.fsync(self._file.fileno())
                        checkpoint = time.monotonic()
                finally:
                    self._queue.task_done()
            if not self._discard:
                os.fsync(self._file.fileno())
        except Exception as exc:
            self.error = f'Recording could not be saved: {exc}'
            self._stop.set()
        finally:
            if self.error and not self._discard:
                try:
                    self._on_error(self.error)
                except Exception:
                    logger.exception('Could not report recording failure')
            self._done.set()
            if self._discard:
                self._release_resources(discard=True)

    def finish(self, timeout=FINISH_TIMEOUT) -> bool:
        self._stop.set()
        return self._done.wait(timeout)

    def read_from(self, offset: int):
        size = self.written_bytes
        if offset < 0 or offset > size:
            return None
        with open(self.path, 'rb') as handle:
            handle.seek(offset)
            return handle.read(size - offset)

    def copy_into(self, destination):
        # Stop always drains before save; never snapshot a writer still changing
        # the file, and never hold a lock the audio callback needs.
        if not self.finish():
            raise TimeoutError('Recording storage did not finish; audio kept for recovery')
        remaining = self.written_bytes // self.frame_bytes * self.frame_bytes
        with open(self.path, 'rb') as source:
            while remaining:
                block = source.read(min(COPY_BYTES, remaining))
                if not block:
                    raise OSError('Recording ended before the saved audio was read')
                destination.writeframesraw(block)
                remaining -= len(block)

    def close(self, *, discard=False) -> bool:
        with self._release_lock:
            if discard and not self._released:
                self._discard = True
                # Recovery must honor Cancel even if a disk write outlives this
                # timeout or the app is killed before the writer can clean up.
                try:
                    with open(self.directory / 'discarded', 'ab') as marker:
                        marker.flush()
                        os.fsync(marker.fileno())
                except OSError:
                    # Full/broken storage must not prevent in-process cancel.
                    # Deferred writer cleanup still removes the captured PCM.
                    logger.warning('Could not checkpoint recording cancellation', exc_info=True)
        if not self.finish():
            return False  # retain the lease while the disk operation is in flight
        self._release_resources(discard=self._discard)
        return True

    def _release_resources(self, *, discard):
        with self._release_lock:
            if self._released:
                return
            self._file.close()
            self._lease.close()
            self._released = True
            # Stat the actual file: a failed/short write may still have saved
            # a recoverable prefix before Python reported the error.
            if discard or self.path.stat().st_size == 0:
                _remove_session(self.directory)


def _remove_session(directory):
    # Delete only our fixed filenames, never recursively follow user paths.
    for name in ('audio.pcm', 'session.json', 'active.lock', 'discarded'):
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def recover_recordings(output_file: str, recordings_folder: str) -> list[str]:
    """Convert abandoned journals to ordinary retained WAVs; live ones are skipped.

    Idempotent destination names allow retry after a crash between WAV rename
    and journal deletion. Failures leave all source PCM for a later retry.
    """
    root = recovery_directory(output_file)
    if not root.is_dir():
        return []
    recovered = []
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        lease = None
        target = temporary = None
        try:
            lease = _Lease(directory / 'active.lock')
            if (directory / 'discarded').exists():
                lease.close()
                lease = None
                _remove_session(directory)
                continue
            with open(directory / 'session.json', encoding='utf-8') as handle:
                meta = json.load(handle)
            rate, channels, width = (meta[k] for k in ('rate', 'channels', 'sample_width'))
            if not (isinstance(rate, int) and 8000 <= rate <= 384000
                    and channels in (1, 2) and width in (1, 2, 3, 4)):
                raise ValueError('Invalid recovery audio format')
            source_path = directory / 'audio.pcm'
            size = source_path.stat().st_size // (channels * width) * (channels * width)
            if size:
                stamp = datetime.fromisoformat(meta['created']).strftime('%Y%m%d_%H%M%S')
                folder = Path(recordings_folder)
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f'recording_{stamp}-recovered-{directory.name}.wav'
                temporary = target.with_suffix('.wav.tmp')
                if not target.exists():
                    with open(temporary, 'wb') as destination:
                        with wave.open(destination, 'wb') as wav:
                            wav.setnchannels(channels)
                            wav.setsampwidth(width)
                            wav.setframerate(rate)
                            with open(source_path, 'rb') as source:
                                remaining = size
                                while remaining:
                                    block = source.read(min(COPY_BYTES, remaining))
                                    if not block:
                                        raise OSError('Incomplete recovery audio')
                                    wav.writeframesraw(block)
                                    remaining -= len(block)
                        destination.flush()
                        os.fsync(destination.fileno())
                    os.replace(temporary, target)
                recovered.append(str(target))
            lease.close()
            lease = None
            _remove_session(directory)
        except (BlockingIOError, PermissionError):
            pass  # active session, including another app process
        except Exception:
            logger.warning('Could not recover interrupted dictation', exc_info=True)
        finally:
            if lease is not None:
                lease.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return recovered
