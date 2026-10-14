"""Speech presence check for Qwen's otherwise unfiltered decoding windows."""
from __future__ import annotations

import numpy as np


def has_speech(audio: np.ndarray) -> bool:
    """Keep the complete 16 kHz window if Silero detects any speech.

    Peak/RMS levels cannot distinguish a click from a quiet, isolated word.
    Reuse Whisper's bundled CPU detector without trimming or concatenating
    speech: Qwen still receives the original samples and source timestamps.
    No minimum speech duration is imposed, so short words are retained.
    """
    if not audio.size:
        return False
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    return bool(get_speech_timestamps(
        np.ascontiguousarray(audio, dtype=np.float32),
        vad_options=VadOptions(min_speech_duration_ms=0, min_silence_duration_ms=500),
    ))
