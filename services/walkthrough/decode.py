"""Decode walkthrough sections so short clips are not silently lost.

Each screenshot's clip is still transcribed on its own, so every engine
works. Whisper, though, pads a few seconds of speech to a 30 s window:
language detection then guesses (Malay at 0.26 on a short English clip),
the no-speech skip returns 0 characters while logging English at 0.99,
and the padding invents phrases such as "Thank you guys". This pins the
language from the whole recording, asks Whisper to treat a short clip as
one utterance, retries a silent result when the clip had speech, and
marks a section that still could not be transcribed.
"""

from __future__ import annotations

import inspect
import logging
import wave
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from meeting.asr.hallucination import is_stock_phrase
from services.recognition_context import RecognitionContext, language_code
from transcriber.local_backend import (
    WHISPER_WINDOW_S,
    whisper_section_options,
    whisper_section_retry_options,
)

logger = logging.getLogger(__name__)

#: Shown in the document when a clip held speech but the engine returned
#: nothing usable, so a blank screenshot is not read as "said nothing".
UNTRANSCRIBED = "[This section could not be transcribed.]"

#: Peak abs sample (16-bit) above which a clip is treated as holding speech.
#: Digital silence is 0; this sits above a quiet noise floor.
HAS_SPEECH_PEAK = 64
#: faster-whisper's own language-detection threshold; below it, keep auto.
LANGUAGE_MIN_PROB = 0.5


def pin_recognition(backend, audio_path: str, recognition: RecognitionContext | None):
    """``recognition`` with a language, detected from the whole recording when auto."""
    current = language_code(getattr(recognition, "language", "") if recognition else "")
    if current:
        return recognition
    detect = getattr(backend, "detect_language", None)
    if not callable(detect):
        return recognition
    try:
        language, probability = detect(audio_path)
    except Exception as exc:
        if "canceled" in str(exc).lower():
            raise
        logger.debug("Walkthrough language detection failed", exc_info=True)
        return recognition
    code = language_code(language)
    if not code or float(probability or 0.0) < LANGUAGE_MIN_PROB:
        return recognition
    logger.info(
        "Walkthrough language %s (probability: %.2f) from the full recording",
        code, float(probability),
    )
    base = recognition if recognition is not None else RecognitionContext()
    return replace(base, language=code)


def wav_stats(path: str) -> tuple[float, int]:
    """``(duration_s, peak)`` of a 16-bit WAV; peak is 0 when it can't be read."""
    try:
        with wave.open(path, "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate() or 1
            width = source.getsampwidth()
            pcm = source.readframes(frames)
        duration = frames / rate
        if width != 2 or not pcm:
            return duration, 0
        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size == 0:
            return duration, 0
        return duration, int(np.max(np.abs(samples.astype(np.int32))))
    except Exception:
        return 0.0, 0


def takes_decode_options(backend) -> bool:
    """Whether ``backend.transcribe`` accepts walkthrough ``decode_options``."""
    transcribe = getattr(backend, "transcribe", None)
    if transcribe is None:
        return False
    try:
        return "decode_options" in inspect.signature(transcribe).parameters
    except (TypeError, ValueError):
        return False


class SectionDecoder:
    """``path -> text`` for one walkthrough, with short-clip recovery."""

    def __init__(self, backend, recognition, incremental, audio_path: str):
        self.backend = backend
        self.incremental = incremental
        self.recognition = pin_recognition(backend, audio_path, recognition)
        self._decode_options = takes_decode_options(backend)

    def __call__(self, path: str) -> str:
        duration_s, peak = wav_stats(path)
        options = whisper_section_options(duration_s) if self._decode_options else None
        text = self._decode(path, options)
        if peak >= HAS_SPEECH_PEAK and _unreliable(text, duration_s):
            retry = whisper_section_retry_options() if self._decode_options else None
            text = self._decode(path, retry)
        if peak >= HAS_SPEECH_PEAK and _unusable(text, duration_s):
            logger.warning(
                "Walkthrough section had speech (%.1f s) but no usable transcript",
                duration_s,
            )
            return UNTRANSCRIBED
        return (text or "").strip()

    def _decode(self, path: str, options: dict | None) -> str:
        backend = self.backend
        recognition = self.recognition
        kwargs: dict = {}
        if recognition and getattr(backend, "supports_recognition", False) is True:
            kwargs["recognition"] = recognition
        if options and self._decode_options:
            kwargs["decode_options"] = options
        if kwargs:
            return backend.transcribe(path, **kwargs) or ""
        from services import recognition_context

        return recognition_context.transcribe(
            self.incremental, backend, path, recognition
        ) or ""


def bind(backend, recognition, incremental, audio_path: str) -> Callable[[str], str]:
    """A ``transcribe(path)`` for :func:`services.walkthrough.finish.transcribe_sections`."""
    return SectionDecoder(backend, recognition, incremental, audio_path)


def _unreliable(text: str, duration_s: float) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return duration_s < WHISPER_WINDOW_S and is_stock_phrase(stripped)


def _unusable(text: str, duration_s: float) -> bool:
    return _unreliable(text, duration_s)
