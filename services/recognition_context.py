"""What a dictation tells the speech engine beyond its audio.

Only the user's own language choice and dictionary terms go here. Text read
from other apps never reaches a speech engine or a remote host.

A context is passed with each call and never stored on a backend: one
backend serves this computer's dictation, its previews and paired clients.
Backends that take one set ``supports_recognition``; their
``recognition_support`` says whether the phrases reach the speech model
("model") or only the steps after it ("after").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence

#: Whether a backend's speech model takes the phrases, or only the
#: replacement and cleanup steps after recognition use the dictionary.
SUPPORT_MODEL: Final[str] = "model"
SUPPORT_AFTER: Final[str] = "after"

#: faster-whisper keeps the first 223 hotword tokens, and they share the
#: decoder's 448 positions with the previous window's text and the output.
#: Rare names cost two to four characters a token, so this stays near 100.
WHISPER_HOTWORDS_MAX_CHARS: Final[int] = 300
#: OpenAI's keyword list, which the SDK documents without limits; a longer
#: list risks a rejected request.
OPENAI_MAX_KEYWORDS: Final[int] = 50
MAX_PHRASE_CHARS: Final[int] = 64
#: Whisper-style prompts are read as preceding text and kept to the last
#: 224 tokens.
OPENAI_PROMPT_MAX_CHARS: Final[int] = 600


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
    dictation still reuses the windows it decoded while recording. A
    backend that takes no context, or an empty one, is called exactly as
    before contexts existed.
    """
    if recognition and getattr(backend, "supports_recognition", False) is True:
        return incremental.transcribe(backend, audio_path, recognition=recognition)
    return incremental.transcribe(backend, audio_path)


def engine_support(engine: str) -> str:
    """What a dictation engine family does with phrases, or "" if only its host knows.

    ``engine`` is a ``selected_model`` value. A remote engine depends on what
    the paired host runs, which only its connected backend can say.
    """
    if engine in ("local_whisper", "api"):
        return SUPPORT_MODEL
    if engine == "remote":
        return ""
    if engine == "nemotron":
        from services.local_asr.nvidia import WORD_BOOSTING

        return SUPPORT_MODEL if WORD_BOOSTING else SUPPORT_AFTER
    return SUPPORT_AFTER


def language_code(language: str | None) -> str:
    """The ISO 639-1 code Whisper and OpenAI take, or "" to detect."""
    if not language or language.lower() == "auto":
        return ""
    return language.replace("_", "-").split("-", 1)[0].lower()


def _bounded(phrases: Sequence[str], *, count: int, chars: int, separator: str) -> list[str]:
    kept: list[str] = []
    used = 0
    for phrase in phrases:
        phrase = " ".join(str(phrase).split())
        if not phrase or len(phrase) > MAX_PHRASE_CHARS:
            continue
        cost = len(phrase) + (len(separator) if kept else 0)
        if len(kept) >= count or used + cost > chars:
            break
        kept.append(phrase)
        used += cost
    return kept


def hotwords(phrases: Sequence[str], max_chars: int = WHISPER_HOTWORDS_MAX_CHARS) -> str:
    """faster-whisper ``hotwords``: the first phrases that fit, space-separated."""
    return " ".join(_bounded(phrases, count=len(phrases), chars=max_chars, separator=" "))


def keywords(phrases: Sequence[str]) -> list[str]:
    """gpt-transcribe ``keywords``: at most 50 phrases of at most 64 characters."""
    return _bounded(phrases, count=OPENAI_MAX_KEYWORDS, chars=OPENAI_MAX_KEYWORDS * MAX_PHRASE_CHARS,
                    separator="")


def vocabulary_prompt(phrases: Sequence[str], max_chars: int = OPENAI_PROMPT_MAX_CHARS) -> str:
    """A prompt that primes Whisper-style models with the right spellings."""
    kept = _bounded(phrases, count=len(phrases), chars=max_chars - 1, separator=", ")
    return ", ".join(kept) + "." if kept else ""
