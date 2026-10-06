"""Playback of saved dictation audio from History.

Refused while recording or in a meeting, and stopped when either starts.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional


class AudioPlayer:
    """Plays one file at a time; every method is safe from any thread."""

    def play(self, path: str, on_finished: Optional[Callable[[], None]] = None) -> bool:
        """Start playing ``path``; False when playback could not start."""
        return False

    def stop(self) -> None:
        pass

    @property
    def is_playing(self) -> bool:
        return False


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
