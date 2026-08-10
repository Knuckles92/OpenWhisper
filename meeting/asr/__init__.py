"""Meeting ASR: background transcription of spooled audio chunks."""

from meeting.asr.revise import REVISION_WINDOW_S, match_segments, revision_window

__all__ = [
    "REVISION_WINDOW_S",
    "match_segments",
    "revision_window",
]
