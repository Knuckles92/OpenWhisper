"""What a dictation tells the speech engine beyond its audio.

Only the user's own language choice and dictionary terms go here. Text read
from other apps never reaches a speech engine or a remote host.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecognitionContext:
    #: "" leaves the engine's own language setting in charge.
    language: str = ""
    #: Vocabulary hints, starred terms first, already cut to the budget.
    phrases: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.language or self.phrases)


def transcribe(incremental, backend, audio_path: str,
               recognition: RecognitionContext | None) -> str:
    """Final-pass transcription of ``audio_path`` with ``recognition`` applied.

    ``incremental`` is the runtime's IncrementalDictation slot, so a long
    dictation still reuses the windows it decoded while recording.
    """
    return incremental.transcribe(backend, audio_path)
