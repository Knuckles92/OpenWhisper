"""OpenAI's February 26, 2027 shutdown of its older transcription models.

OpenAI announced on 2026-08-26 that whisper-1, gpt-4o-transcribe,
gpt-4o-mini-transcribe and gpt-4o-transcribe-diarize leave the API on
2027-02-26 (https://developers.openai.com/api/docs/deprecations). Until then
the three transcription models stay selectable with a "retiring" label and the
OpenAI speaker-identification pass keeps working. From that date saved
choices resolve to ``gpt-transcribe`` and on-device speaker labels.

``gpt-transcribe`` has no diarization: it rejects ``response_format=
"diarized_json"`` with ``unsupported_value`` (probed 2026-09-26), so nothing
replaces the OpenAI speaker pass.

Everything date-dependent lives here so the whole transition can be deleted
once the shutdown has passed.
"""
from __future__ import annotations

from datetime import date
from typing import Final, FrozenSet, Optional

SHUTDOWN_DATE: Final[date] = date(2027, 2, 26)
SHUTDOWN_LABEL: Final[str] = "Feb 26, 2027"

#: Batch transcription models in ``config.API_MODEL_CHOICES`` that OpenAI
#: removes on ``SHUTDOWN_DATE``.
RETIRING_TRANSCRIPTION_MODELS: Final[FrozenSet[str]] = frozenset({
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",
})

#: Shown when the diarize model is gone before the local clock says so.
SPEAKER_MODEL_RETIRED_MESSAGE: Final[str] = (
    "OpenAI has retired its speaker-identification model. "
    "On-device speaker labels were kept."
)


def _today() -> date:
    """Local calendar date. Tests pin it through this seam."""
    return date.today()


def retired(today: Optional[date] = None) -> bool:
    """Whether OpenAI's shutdown date has arrived."""
    return (today or _today()) >= SHUTDOWN_DATE


def is_model_gone_error(exc: BaseException) -> bool:
    """Whether an OpenAI SDK error says the requested model no longer exists.

    Covers a shutdown that lands before this computer's clock reaches
    ``SHUTDOWN_DATE``. A plain 404 counts too: the transcription endpoint
    itself does not move, so a 404 from it means the model.
    """
    # Isolated API workers preserve these two structured SDK fields.
    if getattr(exc, "status_code", None) == 404 or getattr(exc, "code", None) == "model_not_found":
        return True
    try:
        import openai
    except ImportError:
        return False
    if isinstance(exc, openai.NotFoundError):
        return True
    return (
        isinstance(exc, openai.APIStatusError)
        and getattr(exc, "code", None) == "model_not_found"
    )
