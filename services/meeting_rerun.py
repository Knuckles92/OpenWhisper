"""Settings for re-running a past meeting's post-meeting steps.

The desktop Retry button, the dashboard's re-run action, and crash recovery
all resume the same pipeline, so they resolve provider, model, endpoint, ASR
language, and the speaker-pass gate here rather than each picking their own.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from meeting.finalization import SpeakerPassGate


def _openai_key() -> str:
    from services.transcript_cleanup import find_api_key

    return find_api_key("openai") or ""


def resolve_speaker_pass(
    settings: Optional[Dict[str, Any]] = None,
) -> "SpeakerPassGate":
    """Whether saved settings allow uploading system audio to OpenAI now.

    The key is looked up only when the backend and consent already allow the
    pass, and the returned gate carries it only when ``ok``.

    Args:
        settings: Loaded settings mapping; read from disk when omitted.
    """
    from meeting.finalization import speaker_pass_gate
    from services.settings import (
        resolve_meeting_audio_upload_consent,
        resolve_meeting_speaker_id_backend,
        settings_manager,
    )

    if settings is None:
        settings = settings_manager.load_all_settings()
    return speaker_pass_gate(
        backend=resolve_meeting_speaker_id_backend(settings),
        consent=resolve_meeting_audio_upload_consent(settings),
        find_key=_openai_key,
    )


def rerun_options(
    meeting: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return keyword arguments for ``meeting.refinalize.rerun_finalization``.

    The meeting's recorded provider, model, endpoint, and ASR model win so a
    retry matches the original run; current settings fill anything the row
    did not record. Speaker settings are always current: consent withdrawn
    since the meeting must stop a retry from uploading its audio.

    Args:
        meeting: The stored meeting row.
        settings: Loaded settings mapping; read from disk when omitted.

    Returns:
        Every model/provider/ASR keyword ``rerun_finalization`` accepts. The
        caller supplies ``store``, ``progress_cb``, ``model_lease``, and
        ``from_step``.
    """
    from meeting.refinalize import DEFAULT_TIMEOUT_S
    from services.meeting_agent_route import resolve_meeting_agent_route
    from services.settings import (
        resolve_meeting_audio_upload_consent,
        resolve_meeting_language,
        resolve_meeting_redecode_coverage_guard,
        resolve_meeting_speaker_id_backend,
        resolve_meeting_whisper_model,
        settings_manager,
    )
    from meeting.asr.remote import saved_remote_route

    if settings is None:
        settings = settings_manager.load_all_settings()
    meeting = meeting or {}
    remote = saved_remote_route(meeting)
    route = resolve_meeting_agent_route(settings, meeting)
    return {
        "provider": route.provider,
        "model": route.model,
        "endpoint": route.endpoint,
        "agent_core_kind": route.kind,
        "sidecar_payload_dir": route.payload_dir,
        "timeout_s": DEFAULT_TIMEOUT_S,
        "asr_model_name": str(
            meeting.get("asr_model") or resolve_meeting_whisper_model(settings)
        ),
        "language": (remote.get("language", "auto") if remote is not None
                     else resolve_meeting_language(settings)),
        "speaker_id_backend": resolve_meeting_speaker_id_backend(settings),
        "speaker_audio_consent": resolve_meeting_audio_upload_consent(settings),
        "speaker_api_key": resolve_speaker_pass(settings).api_key,
        "redecode_coverage_guard": resolve_meeting_redecode_coverage_guard(settings),
    }
