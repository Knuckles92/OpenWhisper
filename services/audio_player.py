"""Playback of saved dictation audio from History.

Refused while recording or in a meeting, and stopped when either starts.
Plays only the recorder's own WAV (mono 16-bit PCM at config.SAMPLE_RATE):
every recording History keeps is one, since uploads and re-transcriptions
keep none, so there is no decoder here.

Uses its own sounddevice OutputStream rather than ``sd.play``, which shares
one module-wide stream with anything else that calls it.
"""

from __future__ import annotations

import logging
import threading
import wave
from functools import partial
from typing import Callable, Optional

import numpy as np

from config import config

logger = logging.getLogger(__name__)


class UnsupportedAudio(ValueError):
    """The file isn't a recording this player plays."""


def recording_frames(path: str) -> int:
    """How many frames the recording at ``path`` holds; reads only its header.

    Raises:
        UnsupportedAudio: Not a WAV in the recorder's format, or empty.
        OSError: The file can't be read.
    """
    try:
        with wave.open(path, "rb") as wav:
            supported = (
                wav.getcomptype() == "NONE"
                and wav.getnchannels() == config.CHANNELS
                and wav.getsampwidth() == np.dtype(config.AUDIO_FORMAT).itemsize
                and wav.getframerate() == config.SAMPLE_RATE
            )
            frames = wav.getnframes()
    except (wave.Error, EOFError) as exc:
        raise UnsupportedAudio("not a WAV recording") from exc
    if not supported:
        raise UnsupportedAudio("not in the recorder's format")
    if frames <= 0:
        raise UnsupportedAudio("the recording is empty")
    return frames


def _read_samples(path: str, frames: int) -> np.ndarray:
    with wave.open(path, "rb") as wav:
        data = wav.readframes(frames)
    return np.frombuffer(data, dtype="<i2")


class _Playback:
    """One play() call, from loading its file to its end."""

    def __init__(self, session: int, path: str, frames: int,
                 on_finished: Optional[Callable[[], None]]):
        self.session = session
        self.path = path
        self.frames = frames
        self.on_finished = on_finished
        self.samples: Optional[np.ndarray] = None
        #: Frames handed to the device so far; written only by its callback.
        self.position = 0
        self.stream = None
        self.done = False


class AudioPlayer:
    """Plays one file at a time; every method is safe from any thread.

    ``on_finished`` runs once per play(), however it ends (played out,
    stopped, replaced, or failed to start), on whichever thread ended it,
    so it must not touch Qt widgets.
    """

    def __init__(self, sounddevice=None):
        self._sd = sounddevice
        self._lock = threading.Lock()
        # Separate from _lock: PortAudio calls the finished callback, which
        # takes _lock, while abort() runs under this one.
        self._stream_lock = threading.Lock()
        self._current: Optional[_Playback] = None
        self._sessions = 0
        self.last_error = ""

    def _sounddevice(self):
        if self._sd is None:
            import sounddevice

            self._sd = sounddevice
        return self._sd

    def play(self, path: str, on_finished: Optional[Callable[[], None]] = None) -> bool:
        """Start playing ``path``; False when playback could not start.

        Only the header is read here; the samples load and the device
        opens on a worker thread.
        """
        try:
            frames = recording_frames(path)
        except (OSError, UnsupportedAudio) as exc:
            self.last_error = str(exc) or type(exc).__name__
            logger.info("A history recording can't be played: %s", self.last_error)
            return False
        self.stop()
        with self._lock:
            self._sessions += 1
            playback = _Playback(self._sessions, path, frames, on_finished)
            self._current = playback
        self.last_error = ""
        threading.Thread(
            target=self._start, args=(playback,), name="history-playback", daemon=True,
        ).start()
        return True

    def _start(self, playback: _Playback) -> None:
        try:
            playback.samples = _read_samples(playback.path, playback.frames)
            if not self._is_current(playback):
                return
            sd = self._sounddevice()
            stream = sd.OutputStream(
                samplerate=config.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=partial(self._fill, playback),
                finished_callback=partial(self._finished, playback),
            )
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            logger.warning("History playback couldn't start: %s", self.last_error)
            self._finish(playback)
            return
        with self._stream_lock:
            if not self._is_current(playback):
                _close(stream)
                return
            playback.stream = stream
            try:
                stream.start()
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                logger.warning("History playback couldn't start: %s", self.last_error)
                playback.stream = None
                _close(stream)
                failed = True
            else:
                failed = False
        if failed:
            self._finish(playback)

    def _fill(self, playback: _Playback, outdata, frames, _time, _status) -> None:
        """PortAudio's callback: the next block, then silence and a stop."""
        start = playback.position
        chunk = playback.samples[start:start + frames]
        count = len(chunk)
        outdata[:count, 0] = chunk
        playback.position = start + count
        if count < frames:
            outdata[count:] = 0
            raise self._sd.CallbackStop

    def _finished(self, playback: _Playback) -> None:
        """PortAudio's finished callback; the stream can't be closed from in here."""
        self._finish(playback)
        threading.Thread(
            target=self._release, args=(playback,), name="history-playback-close", daemon=True,
        ).start()

    def _release(self, playback: _Playback) -> None:
        with self._stream_lock:
            stream, playback.stream = playback.stream, None
            if stream is not None:
                _close(stream)

    def _is_current(self, playback: _Playback) -> bool:
        with self._lock:
            return self._current is playback

    def _finish(self, playback: _Playback) -> None:
        with self._lock:
            if playback.done:
                return
            playback.done = True
            if self._current is playback:
                self._current = None
        if playback.on_finished is not None:
            try:
                playback.on_finished()
            except Exception:
                logger.debug("A playback finished callback raised", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            playback, self._current = self._current, None
        if playback is None:
            return
        with self._stream_lock:
            stream, playback.stream = playback.stream, None
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    logger.debug("Could not abort history playback", exc_info=True)
                _close(stream)
        self._finish(playback)

    @property
    def is_playing(self) -> bool:
        """True from play() until the recording ends or is stopped."""
        with self._lock:
            return self._current is not None

    @property
    def session(self) -> int:
        """Which play() call is current, or 0 when nothing plays."""
        with self._lock:
            return self._current.session if self._current is not None else 0

    @property
    def path(self) -> str:
        """The recording playing now, or ""."""
        with self._lock:
            return self._current.path if self._current is not None else ""

    @property
    def position(self) -> float:
        """Seconds played of the current recording, or 0."""
        with self._lock:
            current = self._current
        return current.position / config.SAMPLE_RATE if current is not None else 0.0

    @property
    def duration(self) -> float:
        """Length in seconds of the current recording, or 0."""
        with self._lock:
            current = self._current
        return current.frames / config.SAMPLE_RATE if current is not None else 0.0


def _close(stream) -> None:
    try:
        stream.close()
    except Exception:
        logger.debug("Could not close a history playback stream", exc_info=True)


_player: AudioPlayer | None = None
_player_lock = threading.Lock()


def player() -> AudioPlayer:
    """The process-wide player, created on first use."""
    global _player
    with _player_lock:
        if _player is None:
            _player = AudioPlayer()
        return _player


def stop_playback() -> None:
    """Stop any playback, without creating a player; safe from any thread."""
    with _player_lock:
        current = _player
    if current is not None:
        current.stop()
