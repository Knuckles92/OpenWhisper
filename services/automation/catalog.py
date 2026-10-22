"""Every event type OpenWhisper publishes, with one builder per type.

Publishers call a builder rather than writing dicts by hand, so the payload
of a type is the same everywhere. Payloads never carry selected text, caret
context, window titles, file paths or audio.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

RECORDING_STARTED = "recording.started"
RECORDING_STOPPED = "recording.stopped"
RECORDING_CANCELED = "recording.canceled"
TRANSCRIPTION_PREVIEW = "transcription.preview"
TRANSCRIPTION_COMPLETED = "transcription.completed"
TRANSCRIPTION_FAILED = "transcription.failed"
HISTORY_SAVED = "history.saved"
ACTION_RAN = "action.ran"
CHANNEL_MESSAGE = "channel.message"
ROUTE_DECIDED = "route.decided"
FILTER_FAILED = "filter.failed"
MEETING_STARTED = "meeting.started"
MEETING_ENDED = "meeting.ended"
MEETING_SEGMENTS = "meeting.segments"
MEETING_VOICE_COMMAND = "meeting.voice_command"
#: Synthetic, sent only on an event stream whose client missed events.
EVENTS_GAP = "events.gap"

#: One line per type, for checklists, the docs and MCP events/list.
EVENT_TYPES: dict[str, str] = {
    RECORDING_STARTED: "A recording started (dictation, Command Mode or an action).",
    RECORDING_STOPPED: "A recording stopped and is being transcribed.",
    RECORDING_CANCELED: "A recording or transcription was canceled.",
    TRANSCRIPTION_PREVIEW: "Live preview text while recording, at most four a second.",
    TRANSCRIPTION_COMPLETED: "Finished text of a dictation, command, action or file.",
    TRANSCRIPTION_FAILED: "A transcription or rewrite failed.",
    HISTORY_SAVED: "A finished transcription was saved to history.",
    ACTION_RAN: "An action ran, with whether it worked.",
    CHANNEL_MESSAGE: "Words sent to Claude Code sessions listening on the channel.",
    ROUTE_DECIDED: "Route by voice chose an action, or fell back to pasting.",
    FILTER_FAILED: "A text filter timed out or answered badly; the text was kept.",
    MEETING_STARTED: "A meeting started.",
    MEETING_ENDED: "A meeting ended.",
    MEETING_SEGMENTS: "New finished lines of a meeting transcript.",
    MEETING_VOICE_COMMAND: "Someone gave the meeting note-taker a spoken command.",
    EVENTS_GAP: "This stream missed events; fetch history to catch up.",
}
PUBLIC_TYPES: tuple[str, ...] = tuple(t for t in EVENT_TYPES if t != EVENTS_GAP)

#: Values of ``source``: who started the job.
SOURCES = ("hotkey", "ui", "api", "mcp", "dbus")
#: Sources where the user started the job; any other is a program's request.
USER_SOURCES = frozenset({"hotkey", "ui", "dbus"})
#: ``channel.message`` ``origin``: the user's own words, or words a program
#: supplied or changed (DictationJob.program_words).
ORIGIN_DICTATED = "dictated"
ORIGIN_PROGRAM = "program"
#: ``mode`` of a transcription read from a file rather than dictated live.
FILE_MODE = "file"
#: ``transcription.failed`` ``error``: why a job ended without text.
FAILURE_CANCELED = "canceled"
FAILURE_NO_SPEECH = "no_speech"
#: Nothing to do, for a reason the user was told (``message``).
FAILURE_REFUSED = "refused"
FAILURE_FAILED = "failed"
FAILURES = frozenset({FAILURE_CANCELED, FAILURE_NO_SPEECH, FAILURE_REFUSED, FAILURE_FAILED})
#: Modes whose ``raw_text`` is the words before AI cleanup. A command's or
#: transform's original is the user's selected text, which events never carry.
_RAW_TEXT_MODES = frozenset({"dictation", "action", "walkthrough", FILE_MODE})


def user_started(source: Any) -> bool:
    """Whether a job of ``source`` was started by the user rather than a program.

    An empty or unknown source counts as a program's.
    """
    return source in USER_SOURCES


def known_types(types: Iterable[str]) -> list[str]:
    """``types`` filtered to public event types, order kept, duplicates dropped."""
    seen: list[str] = []
    for item in types:
        if item in EVENT_TYPES and item != EVENTS_GAP and item not in seen:
            seen.append(item)
    return seen


def job_fields(job: Any) -> dict:
    """``job_id``, ``mode``, ``action_id``, ``source`` and ``route`` of a DictationJob or None."""
    if job is None:
        return {"job_id": "", "mode": FILE_MODE, "action_id": None, "source": "", "route": ""}
    return {
        "job_id": getattr(job, "id", "") or "",
        "mode": getattr(job, "mode", "") or "",
        "action_id": getattr(job, "action_id", "") or None,
        "source": getattr(job, "source", "") or "",
        "route": getattr(job, "route", "") or "",
    }


def app_from_fields(fields: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The ``app`` object from history context fields (``history_fields``), or None."""
    fields = fields or {}
    app_id = fields.get("app_id")
    if not app_id:
        return None
    return {
        "id": app_id,
        "name": fields.get("app_name") or "",
        "category": fields.get("app_category") or "",
    }


def recording_started(job: Any) -> dict:
    return job_fields(job)


def recording_stopped(job: Any, duration_s: Optional[float]) -> dict:
    return {
        "job_id": job_fields(job)["job_id"],
        "duration_s": round(float(duration_s), 3) if duration_s is not None else None,
    }


def recording_canceled(job: Any) -> dict:
    return {"job_id": job_fields(job)["job_id"]}


def transcription_preview(job: Any, text: str, final: bool) -> dict:
    return {"job_id": job_fields(job)["job_id"], "text": text or "", "final": bool(final)}


def transcription_completed(
    job: Any,
    *,
    text: str,
    raw_text: Optional[str],
    fields: Optional[Mapping[str, Any]] = None,
    cleanup_info: Any = None,
    audio_s: Optional[float] = None,
) -> dict:
    """Finished text; ``fields`` are the entry's ``history_fields`` (app, language).

    A job of None is an upload or a re-transcription (``mode`` "file").
    ``raw_text`` is published only where it is the words before AI cleanup,
    never for Command Mode or a transform, whose original is the selection.
    """
    base = job_fields(job)
    if base["mode"] not in _RAW_TEXT_MODES:
        raw_text = None
    cleanup = None
    if cleanup_info is not None:
        cleanup = {
            "provider": getattr(cleanup_info, "provider", "") or "",
            "model": getattr(cleanup_info, "model", "") or "",
            "level": getattr(cleanup_info, "level", "") or "",
        }
    return {
        **{k: base[k] for k in ("job_id", "mode", "action_id", "source")},
        "routed": bool(getattr(job, "routed", False)),
        "text": text or "",
        "raw_text": raw_text if raw_text and raw_text != text else None,
        "language": (fields or {}).get("language") or "",
        "app": app_from_fields(fields),
        "cleanup": cleanup,
        "audio_s": round(float(audio_s), 3) if audio_s else None,
    }


def transcription_failed(job: Any, error: str, *, message: Optional[str] = None) -> dict:
    """Why a job ended without text: a FAILURE_* category, never an exception's
    text (it can name files and folders).

    ``message`` is kept only for a refusal: the copy the user was shown
    ("Select text first").
    """
    category = error if error in FAILURES else FAILURE_FAILED
    refusal = str(message)[:500] if category == FAILURE_REFUSED and message else None
    return {"job_id": job_fields(job)["job_id"], "error": category, "message": refusal}


def history_saved(job_id: str, record_id: Any, kind: str) -> dict:
    return {"job_id": job_id or "", "record_id": str(record_id), "kind": kind or ""}


def action_ran(job: Any, action: Any, *, ok: bool, message: str, output: str,
               text: Optional[str] = None) -> dict:
    """``text`` is the words the action got, for "Send to my scripts" only."""
    fields = job_fields(job)
    data = {
        "job_id": fields["job_id"],
        "source": fields["source"],
        "action_id": getattr(action, "id", ""),
        "name": getattr(action, "name", ""),
        "kind": getattr(action, "kind", ""),
        "ok": bool(ok),
        "message": str(message or "")[:500],
        "output": output or "none",
    }
    if text is not None:
        data["text"] = text
    return data


def channel_message(job: Any, action: Any, text: str, app: Optional[Mapping]) -> dict:
    """``source`` says who started the job; ``origin`` whose words these are:
    ``program`` when a program supplied or changed them, else ``dictated``.

    A job that can't say counts as a program's.
    """
    fields = job_fields(job)
    users = getattr(job, "program_words", True) is False
    return {
        "job_id": fields["job_id"],
        "source": fields["source"],
        "origin": ORIGIN_DICTATED if users else ORIGIN_PROGRAM,
        "action_id": getattr(action, "id", ""),
        "text": text or "",
        "app": dict(app) if app else None,
    }


def route_decided(job: Any, action_id: Optional[str], confidence: Optional[float],
                  fallback: bool) -> dict:
    return {
        "job_id": job_fields(job)["job_id"],
        "action_id": action_id or None,
        "confidence": round(float(confidence), 4) if confidence is not None else None,
        "fallback": bool(fallback),
    }


def filter_failed(filter_id: str, name: str, stage: str, reason: str) -> dict:
    return {"filter_id": filter_id, "name": name, "stage": stage, "reason": str(reason)[:200]}


def meeting_started(meeting_id: str, title: str) -> dict:
    return {"meeting_id": meeting_id or "", "title": title or ""}


def meeting_ended(meeting_id: str, status: str) -> dict:
    return {"meeting_id": meeting_id or "", "status": status or ""}


def meeting_segments(meeting_id: str, items: Iterable[Mapping[str, Any]]) -> dict:
    rows = []
    for item in items:
        rows.append({
            "segment_id": str(item.get("segment_id") or item.get("id") or ""),
            "speaker": item.get("speaker") or "",
            "text": item.get("text") or "",
            "start_s": item.get("start_s"),
            "end_s": item.get("end_s"),
        })
    return {"meeting_id": meeting_id or "", "items": rows}


def meeting_voice_command(meeting_id: str, command: str) -> dict:
    return {"meeting_id": meeting_id or "", "command": command or ""}


#: ``events.gap`` ``reason``: OpenWhisper no longer holds events published
#: after the stream's position (more came than it keeps, or history was
#: deleted); fetch history to catch up.
GAP_EVICTED = "evicted"
#: The position is from before OpenWhisper restarted: what the earlier run
#: published after it can't be replayed; everything since the restart is.
GAP_RESTARTED = "restarted"
#: The stream fell too far behind and ends; reconnecting with Last-Event-ID
#: replays the rest.
GAP_SLOW = "slow"


def events_gap(missed_from_seq: int, reason: str) -> dict:
    """``reason`` is GAP_EVICTED, GAP_RESTARTED or GAP_SLOW."""
    return {"missed_from_seq": int(missed_from_seq), "reason": reason}
