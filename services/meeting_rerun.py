"""Settings for re-running a past meeting's post-meeting steps.

The desktop Retry button, the dashboard's re-run action, and crash recovery
all resume the same pipeline, so they resolve provider, model, endpoint, ASR
language, and the speaker key here rather than each picking their own.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def rerun_options(
    meeting: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return keyword arguments for ``meeting.refinalize.rerun_finalization``.

    The meeting's recorded provider, model, endpoint, and ASR model win so a
    retry matches the original run; current settings fill anything the row
    did not record.

    Args:
        meeting: The stored meeting row.
        settings: Loaded settings mapping; read from disk when omitted.

    Returns:
        Every model/provider/ASR keyword ``rerun_finalization`` accepts. The
        caller supplies ``store``, ``progress_cb``, ``model_lease``, and
        ``from_step``.
    """
    from meeting.refinalize import DEFAULT_TIMEOUT_S
    from services.components import meeting_agent_payload_dir
    from services.settings import (
        resolve_meeting_agent_core,
        resolve_meeting_language,
        resolve_meeting_llm_model,
        resolve_meeting_llm_provider,
        resolve_meeting_redecode_coverage_guard,
        resolve_meeting_whisper_model,
        settings_manager,
    )
    from services.text_llm import snapshot_from_meeting

    if settings is None:
        settings = settings_manager.load_all_settings()
    meeting = meeting or {}
    provider = meeting.get("agent_provider") or resolve_meeting_llm_provider(settings)
    agent_core_kind = resolve_meeting_agent_core(settings)
    try:
        from services.transcript_cleanup import find_api_key

        speaker_api_key = find_api_key("openai") or ""
    except Exception:
        logger.exception("Could not look up the OpenAI key for speaker labels")
        speaker_api_key = ""
    return {
        "provider": provider,
        "model": meeting.get("agent_model") or resolve_meeting_llm_model(settings),
        "endpoint": snapshot_from_meeting(
            meeting, settings, fallback_provider=provider,
        ).to_dict(),
        "agent_core_kind": agent_core_kind,
        "sidecar_payload_dir": meeting_agent_payload_dir(agent_core_kind),
        "timeout_s": DEFAULT_TIMEOUT_S,
        "asr_model_name": str(
            meeting.get("asr_model") or resolve_meeting_whisper_model(settings)
        ),
        "language": resolve_meeting_language(settings),
        "speaker_api_key": speaker_api_key,
        "redecode_coverage_guard": resolve_meeting_redecode_coverage_guard(settings),
    }
