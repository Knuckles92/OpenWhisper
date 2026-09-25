"""Drop Whisper segments that are almost certainly not speech.

Whisper invents text on silence, music, and noise: stock video phrases
("Thanks for watching"), a lone "you", or a phrase looped until the window
ends. Those rows then become evidence for cards, so they are dropped before
they are stored.

The thresholds follow openai-whisper's own no-speech and compression checks.
Backends that do not report these scores leave them at zero, which never
trips a rule.
"""
from __future__ import annotations

import re
from typing import Any

#: Whisper's own skip rule: likely silence *and* a low-confidence decode.
NO_SPEECH_PROB = 0.6
LOW_AVG_LOGPROB = -1.0
#: Above this, Whisper's temperature fallback has already failed to break a
#: repetition loop.
MAX_COMPRESSION_RATIO = 2.4
#: Stock phrases are also real speech ("Thank you."), so they are dropped
#: only when the decoder shows some doubt that anyone was talking.
STOCK_PHRASE_NO_SPEECH_PROB = 0.3
STOCK_PHRASE_AVG_LOGPROB = -0.8

_STOCK_PHRASES = frozenset({
    "you",
    "thank you for watching",
    "thanks for watching",
    "thank you for watching and see you next time",
    "thank you so much for watching",
    "please subscribe",
    "like and subscribe",
    "subscribe to my channel",
    "please like and subscribe",
    "see you in the next video",
    "subtitles by the amara org community",
    "transcription by castingwords",
    "captions by",
})

_NON_WORD = re.compile(r"[^\w\s]+")
_SPACES = re.compile(r"\s+")


def _score(seg: Any, name: str) -> float:
    value = getattr(seg, name, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _normalized(text: str) -> str:
    return _SPACES.sub(" ", _NON_WORD.sub(" ", text.lower())).strip()


def is_hallucination(seg: Any) -> bool:
    """Return True when a decoded segment should not become transcript.

    Args:
        seg: A faster-whisper ``Segment`` (or anything with ``text`` and the
            optional ``no_speech_prob``, ``avg_logprob``, and
            ``compression_ratio`` scores).
    """
    no_speech = _score(seg, "no_speech_prob")
    avg_logprob = _score(seg, "avg_logprob")
    if no_speech > NO_SPEECH_PROB and avg_logprob < LOW_AVG_LOGPROB:
        return True
    if _score(seg, "compression_ratio") > MAX_COMPRESSION_RATIO:
        return True
    if _normalized(str(getattr(seg, "text", "") or "")) in _STOCK_PHRASES:
        return (
            no_speech > STOCK_PHRASE_NO_SPEECH_PROB
            or avg_logprob < STOCK_PHRASE_AVG_LOGPROB
        )
    return False
