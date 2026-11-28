"""Extended desktop app control helpers for the live API and MCP.

Keeps domain logic (meetings, walkthroughs, personalization, models/devices,
transforms, files and window control) modular while ``QtAppControl`` in
``services/automation/app_control.py`` owns the Qt thread relay.
"""

from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.automation.control import BUSY, NOT_FOUND, REFUSED, UNAVAILABLE, ControlError
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)

_AUDIO_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".webm",
    ".mp4", ".aac", ".wma", ".mkv", ".mov", ".m4b", ".aiff", ".aif",
})
_MAX_IMAGES_RETURNED = 10
_MAX_IMAGE_BYTES_TOTAL = 2 * 1024 * 1024
_MAX_IMAGE_FILE_BYTES = 1024 * 1024


def _refresh_settings_ui(controller) -> None:
    ui = getattr(controller, "ui_controller", None)
    dialog = getattr(ui, "_settings_dialog", None) if ui is not None else None
    if dialog is not None and callable(getattr(dialog, "refresh", None)):
        try:
            dialog.refresh()
        except Exception:
            pass


# ---- Meeting Mode -----------------------------------------------------------


def _finalization_payload(fin: Any) -> Optional[dict[str, Any]]:
    """Serialize a FinalizationState or mapping with full step-by-step progress."""
    if fin is None:
        return None
    if hasattr(fin, "to_dict") and callable(fin.to_dict):
        try:
            fin = fin.to_dict()
        except Exception:
            return None
    if not isinstance(fin, Mapping):
        return None
    steps_raw = fin.get("steps")
    steps = [dict(s) for s in steps_raw if isinstance(s, Mapping)] if isinstance(steps_raw, (list, tuple)) else []
    stats_raw = fin.get("summary_stats")
    summary_stats = dict(stats_raw) if isinstance(stats_raw, Mapping) else {}
    return {
        "status": str(fin.get("status") or ""),
        "message": str(fin.get("message") or ""),
        "stage": str(fin.get("stage") or ""),
        "current_step": int(fin.get("current_step") or 0),
        "total_steps": int(fin.get("total_steps") or 0),
        "step_details": str(fin.get("step_details") or ""),
        "steps": steps,
        "summary_stats": summary_stats,
    }


def _running_reports_from_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    reports = snapshot.get("custom_reports")
    if not isinstance(reports, list):
        return []
    running: list[dict[str, Any]] = []
    for rep in reports:
        if not isinstance(rep, Mapping) or rep.get("status") != "running":
            continue
        running.append({
            "id": str(rep.get("id") or ""),
            "request": str(rep.get("request") or ""),
            "title": str(rep.get("title") or ""),
            "status": "running",
            "message": str(rep.get("message") or ""),
            "created_at": str(rep.get("created_at") or ""),
        })
    return running


def _scheduler_job_status(engine: Any) -> Optional[dict[str, Any]]:
    scheduler = getattr(engine, "_scheduler", None)
    if scheduler is None:
        return None
    thread = getattr(scheduler, "_thread", None)
    active = bool(thread.is_alive()) if thread is not None and hasattr(thread, "is_alive") else True
    note_requests = getattr(scheduler, "_note_requests", None)
    try:
        note_requests_pending = len(note_requests) if note_requests is not None else 0
    except Exception:
        note_requests_pending = 0
    return {
        "active": active,
        "pending_segments": int(getattr(scheduler, "_pending_segments", 0) or 0),
        "guidance_pending": bool(getattr(scheduler, "_guidance_pending", False)),
        "notes_guidance_pending": bool(getattr(scheduler, "_notes_guidance_pending", False)),
        "notes_resume_pending": bool(getattr(scheduler, "_notes_resume_pending", False)),
        "note_requests_pending": note_requests_pending,
    }


def _snapshot_counts(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    cards_raw = snapshot.get("cards")
    card_counts: dict[str, int] = {}
    if isinstance(cards_raw, Mapping):
        for card_key, items in cards_raw.items():
            if isinstance(items, list):
                card_counts[str(card_key)] = sum(
                    1 for item in items if isinstance(item, Mapping) and item.get("status") != "removed"
                )
    participants = snapshot.get("participants")
    participant_count = len(participants) if isinstance(participants, Mapping) else 0
    questions = snapshot.get("questions")
    if isinstance(questions, Mapping):
        questions = list(questions.values())
    open_questions = sum(
        1 for q in (questions if isinstance(questions, list) else [])
        if isinstance(q, Mapping) and q.get("status") == "open"
    )
    reports = snapshot.get("custom_reports")
    report_count = len(reports) if isinstance(reports, list) else 0
    highlights = snapshot.get("live_highlights")
    highlight_count = len(highlights) if isinstance(highlights, list) else 0
    return {
        "participants": participant_count,
        "open_questions": open_questions,
        "cards": card_counts,
        "custom_reports": report_count,
        "live_highlights": highlight_count,
    }


def _engine_snapshot(engine: Any) -> Optional[dict[str, Any]]:
    store = getattr(engine, "store", None)
    if store is None or not callable(getattr(store, "snapshot", None)):
        return None
    try:
        snap = store.snapshot()
        return dict(snap) if isinstance(snap, Mapping) else None
    except Exception:
        return None


def _background_jobs(runtime: Any) -> list[dict[str, Any]]:
    bg = getattr(runtime, "_background_engines", None)
    if not isinstance(bg, Mapping):
        return []
    jobs: list[dict[str, Any]] = []
    for bg_id, bg_engine in list(bg.items()):
        snap = _engine_snapshot(bg_engine)
        jobs.append({
            "meeting_id": str(getattr(bg_engine, "meeting_id", None) or bg_id),
            "title": str((snap or {}).get("title") or ""),
            "status": str((snap or {}).get("status") or "finalizing"),
            "finalization": _finalization_payload((snap or {}).get("finalization")),
            "running_reports": _running_reports_from_snapshot(snap or {}),
        })
    return jobs


def meeting_status(controller) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        return {
            "active": False,
            "starting": False,
            "paused": False,
            "finalizing": False,
            "meeting_id": None,
            "status": "idle",
            "elapsed_s": 0.0,
            "background_count": 0,
            "background_jobs": [],
        }
    engine = getattr(runtime, "_engine", None)
    active = bool(runtime.is_active)
    starting = bool(getattr(runtime, "_starting", False))
    finalizing = bool(runtime.is_finalizing)
    meeting_id = str(getattr(engine, "meeting_id", "") or "") or None
    paused = False
    elapsed_s = 0.0
    status = "idle"
    if active and engine is not None:
        try:
            paused = bool(getattr(engine, "is_paused", lambda: False)())
        except Exception:
            paused = False
        try:
            clock = getattr(engine, "clock", None)
            if clock is not None:
                elapsed_s = round(float(clock.now_s()), 1)
        except Exception:
            elapsed_s = 0.0
        status = "paused" if paused else "active"
    elif starting:
        status = "starting"
    elif finalizing:
        status = "finalizing"
    bg_jobs = _background_jobs(runtime)
    snap = _engine_snapshot(engine) if engine is not None else None
    fin_summary = _finalization_payload((snap or {}).get("finalization")) or _finalization_payload(
        getattr(runtime, "_finalization", None)
    )
    payload: dict[str, Any] = {
        "active": active,
        "starting": starting,
        "paused": paused,
        "finalizing": finalizing,
        "meeting_id": meeting_id or getattr(runtime, "_card_meeting_id", None),
        "status": status,
        "elapsed_s": elapsed_s,
        "background_count": len(bg_jobs),
        "background_jobs": bg_jobs,
        "finalization": fin_summary,
    }
    if snap is not None:
        payload.update({
            "title": str(snap.get("title") or ""),
            "cloud_enabled": bool(snap.get("cloud_enabled", False)),
            "intelligence_online": bool(snap.get("intelligence_online", False)),
            "diarization_available": bool(snap.get("diarization_available", False)),
            "diarization_reason": str(snap.get("diarization_reason") or ""),
            "topic": snap.get("topic") if isinstance(snap.get("topic"), Mapping) else {},
            "intent": snap.get("intent") if isinstance(snap.get("intent"), Mapping) else {},
            "capture": snap.get("capture") if isinstance(snap.get("capture"), Mapping) else {},
            "speech": snap.get("speech") if isinstance(snap.get("speech"), Mapping) else {},
            "live_highlights_status": str(snap.get("live_highlights_status") or "unknown"),
            "counts": _snapshot_counts(snap),
            "running_reports": _running_reports_from_snapshot(snap),
            "scheduler": _scheduler_job_status(engine),
        })
    return payload



def start_meeting(controller, *, title: str = "", cloud: Optional[bool] = None) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available in this build.")
    if runtime.is_finalizing:
        raise ControlError(BUSY, "Final insights are still being prepared. Background or wait for them first.")
    if runtime.is_claimed or controller.is_meeting_active():
        raise ControlError(BUSY, "A meeting is already in progress.")
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job:
        raise ControlError(BUSY, "Finish the active dictation or walkthrough before starting a meeting.")
    if getattr(controller, "_backup_in_progress", False):
        raise ControlError(BUSY, "Wait for backup or restore preparation to finish.")

    saved = settings_manager.load_all_settings()
    effective_cloud = (
        bool(cloud) if cloud is not None
        else SettingsKey.MEETING_CLOUD_LAST_ENABLED.read(saved)
    )
    if effective_cloud and not SettingsKey.MEETING_CLOUD_CONSENT_GIVEN.read(saved):
        raise ControlError(
            REFUSED,
            "Accept Meeting Mode cloud-intelligence consent in OpenWhisper first, "
            "or start the meeting with cloud=false.",
        )

    messages: list[str] = []

    def capture(msg: str) -> None:
        messages.append(msg)

    runtime.meeting_status_update.connect(capture)
    try:
        runtime.start_meeting(cloud_enabled=effective_cloud, intent=(title or "").strip()[:200])
    finally:
        try:
            runtime.meeting_status_update.disconnect(capture)
        except Exception:
            pass

    if not runtime.is_claimed and not runtime.is_active:
        raise ControlError(REFUSED, messages[-1] if messages else "OpenWhisper could not start Meeting Mode.")
    return {
        "started": True,
        "cloud": effective_cloud,
        "status": "active" if runtime.is_active else "starting",
    }


def pause_meeting(controller) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None or not runtime.is_active:
        raise ControlError(REFUSED, "No meeting is currently active.")
    runtime.pause_meeting()
    return {"paused": True, "meeting_state": meeting_status(controller)}


def resume_meeting(controller) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None or not runtime.is_active:
        raise ControlError(REFUSED, "No meeting is currently active.")
    runtime.resume_meeting()
    return {"resumed": True, "meeting_state": meeting_status(controller)}


def end_meeting(controller) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None or not runtime.is_active:
        raise ControlError(REFUSED, "No meeting is currently active.")
    runtime.end_meeting()
    return {"ending": True, "meeting_state": meeting_status(controller)}


def background_meeting(controller) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available.")
    if not runtime.continue_in_background():
        raise ControlError(REFUSED, "No finalizing meeting is ready to move to the background.")
    return {"backgrounded": True, "meeting_state": meeting_status(controller)}


def retry_meeting_finalization(
    controller, *, step: str = "failed", meeting_id: Optional[str] = None
) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available.")
    step_key = (step or "failed").strip() or "failed"
    allowed_steps = {"failed", "all", "redecode", "speaker_id", "polish", "insights", "report"}
    if step_key not in allowed_steps:
        raise ControlError(REFUSED, f"Unsupported finalization step {step_key!r}.")
    if step_key == "all":
        step_key = "redecode"
    if runtime.is_active or runtime.is_claimed:
        raise ControlError(BUSY, "End the active meeting before retrying finalization.")
    runtime.retry_finalization(step_key, meeting_id=meeting_id or None)
    return {"retrying": True, "step": step_key, "meeting_id": meeting_id or None}


def manage_recovered_meeting(controller, meeting_id: str, action: str) -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available.")
    if action not in {"finalize", "discard"}:
        raise ControlError(REFUSED, "action must be 'finalize' or 'discard'.")
    if runtime.is_active or runtime.is_claimed:
        raise ControlError(BUSY, "End the active meeting before managing recovered meetings.")
    if action == "finalize":
        runtime.finalize_recovered(meeting_id)
        return {"meeting_id": meeting_id, "action": "finalize", "started": True}
    runtime.discard_recovered(meeting_id)
    return {"meeting_id": meeting_id, "action": "discard", "discarded": True}


def open_meeting_view(controller, *, meeting_id: str = "", view: str = "dashboard") -> dict[str, Any]:
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available.")
    if view not in {"dashboard", "report"}:
        raise ControlError(REFUSED, "view must be 'dashboard' or 'report'.")
    if meeting_id:
        runtime.open_past_meeting(meeting_id, view="" if view == "dashboard" else view)
    elif view == "report":
        runtime.open_report()
    else:
        runtime.open_dashboard()
    return {"opened": True, "view": view, "meeting_id": meeting_id or None}


def _require_live_meeting_engine(controller):
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available in this build.")
    engine = getattr(runtime, "_engine", None)
    if not runtime.is_active or engine is None or getattr(engine, "store", None) is None:
        raise ControlError(REFUSED, "No meeting is currently active.")
    return runtime, engine


def _meeting_repository(controller, runtime):
    repo_fn = getattr(runtime, "_repository", None)
    if callable(repo_fn):
        return repo_fn()
    db = getattr(controller, "database", None)
    if db is not None:
        from meeting.persist.repository import SqlMeetingRepository

        return SqlMeetingRepository(db)
    return None


def _apply_live_op(engine, op: dict[str, Any]):
    me_id = getattr(engine, "_me_participant_id", None)
    results = engine.apply_client_action("host", me_id, op)
    if not results or not results[0].ok:
        reason = results[0].reason if results else "rejected"
        raise ControlError(REFUSED, f"Meeting action rejected: {reason}")
    return results[0]


def get_meeting_dashboard_state(
    controller, *, meeting_id: Optional[str] = None
) -> dict[str, Any]:
    """Return the full MeetingState dashboard snapshot and live job progress."""
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available in this build.")
    engine = getattr(runtime, "_engine", None)
    engine_mid = str(getattr(engine, "meeting_id", "") or "") or None
    target_id = (meeting_id or "").strip() or engine_mid or getattr(runtime, "_card_meeting_id", None)
    if not target_id:
        raise ControlError(REFUSED, "No active meeting; pass meeting_id to read a saved meeting.")

    bg_jobs = _background_jobs(runtime)
    if engine is not None and engine_mid == target_id:
        snap = _engine_snapshot(engine)
        if snap is not None:
            elapsed_s = 0.0
            if bool(runtime.is_active):
                try:
                    clock = getattr(engine, "clock", None)
                    if clock is not None:
                        elapsed_s = round(float(clock.now_s()), 1)
                except Exception:
                    elapsed_s = 0.0
            return {
                "meeting_id": target_id,
                "is_live": bool(runtime.is_active),
                "finalizing": bool(runtime.is_finalizing),
                "elapsed_s": elapsed_s,
                "state": snap,
                "jobs": {
                    "scheduler": _scheduler_job_status(engine),
                    "finalization": _finalization_payload(snap.get("finalization")),
                    "running_reports": _running_reports_from_snapshot(snap),
                    "background_jobs": bg_jobs,
                },
            }

    bg = getattr(runtime, "_background_engines", None)
    if isinstance(bg, Mapping) and target_id in bg:
        bg_engine = bg[target_id]
        snap = _engine_snapshot(bg_engine)
        if snap is not None:
            return {
                "meeting_id": target_id,
                "is_live": False,
                "finalizing": True,
                "elapsed_s": 0.0,
                "state": snap,
                "jobs": {
                    "scheduler": None,
                    "finalization": _finalization_payload(snap.get("finalization")),
                    "running_reports": _running_reports_from_snapshot(snap),
                    "background_jobs": bg_jobs,
                },
            }

    repo = _meeting_repository(controller, runtime)
    if repo is None:
        raise ControlError(UNAVAILABLE, "Meeting history repository is unavailable.")
    meeting = repo.get_meeting(target_id)
    if not meeting:
        raise ControlError(NOT_FOUND, f"Meeting {target_id!r} was not found.")

    from meeting.state.schema import FinalizationState, parse_state_json

    snap = parse_state_json(meeting.get("state_json"))
    if snap is None:
        raise ControlError(NOT_FOUND, f"No dashboard state snapshot is available for meeting {target_id!r}.")
    fin = FinalizationState.normalize_historical(
        snap.get("finalization"),
        cloud_enabled=bool(snap.get("cloud_enabled", False)),
        meeting_status=str(meeting.get("status") or snap.get("status") or "ended"),
    )
    snap["finalization"] = fin.to_dict()
    return {
        "meeting_id": target_id,
        "is_live": False,
        "finalizing": False,
        "elapsed_s": 0.0,
        "state": snap,
        "jobs": {
            "scheduler": None,
            "finalization": _finalization_payload(fin),
            "running_reports": _running_reports_from_snapshot(snap),
            "background_jobs": bg_jobs,
        },
    }


def update_live_meeting(
    controller,
    *,
    title: Optional[str] = None,
    intent: Optional[str] = None,
    topic: Optional[str] = None,
    cloud_enabled: Optional[bool] = None,
) -> dict[str, Any]:
    """Update live meeting title, standing intent/brief, current topic, or AI insights toggle."""
    _runtime, engine = _require_live_meeting_engine(controller)
    if title is None and intent is None and topic is None and cloud_enabled is None:
        raise ControlError(REFUSED, "Provide at least one field to update (title, intent, topic, or cloud_enabled).")

    updated: list[str] = []
    seqs: list[int] = []
    if title is not None:
        res = _apply_live_op(engine, {"op": "set_title", "text": title})
        updated.append("title")
        if res.seq is not None:
            seqs.append(int(res.seq))
    if intent is not None:
        res = _apply_live_op(engine, {"op": "set_meeting_intent", "text": intent})
        updated.append("intent")
        if res.seq is not None:
            seqs.append(int(res.seq))
    if topic is not None:
        res = _apply_live_op(engine, {"op": "set_topic", "text": topic})
        updated.append("topic")
        if res.seq is not None:
            seqs.append(int(res.seq))
    if cloud_enabled is not None:
        want_cloud = bool(cloud_enabled)
        if want_cloud:
            saved = settings_manager.load_all_settings()
            if not SettingsKey.MEETING_CLOUD_CONSENT_GIVEN.read(saved):
                raise ControlError(
                    REFUSED,
                    "Turn on AI insights once from the desktop app first; it asks for consent "
                    "before meeting text is sent to the AI provider.",
                )
        try:
            engine.set_cloud_enabled(want_cloud)
        except RuntimeError as exc:
            raise ControlError(REFUSED, str(exc)) from exc
        try:
            settings_manager.save_setting(SettingsKey.MEETING_CLOUD_LAST_ENABLED, want_cloud)
        except Exception:
            logger.warning("Could not persist MEETING_CLOUD_LAST_ENABLED", exc_info=True)
        updated.append("cloud_enabled")

    return {
        "ok": True,
        "meeting_id": str(getattr(engine, "meeting_id", "") or ""),
        "updated": updated,
        "seqs": seqs,
        "meeting_state": meeting_status(controller),
    }


def manage_meeting_card_item(
    controller,
    *,
    action: str,
    card: Optional[str] = None,
    item_id: Optional[str] = None,
    text: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
    evidence: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Add, update, remove, pin, unpin, or confirm a live meeting dashboard card item."""
    _runtime, engine = _require_live_meeting_engine(controller)
    from meeting.state.schema import CARD_KEYS

    act = (action or "").strip().lower()
    if act == "add":
        if card not in CARD_KEYS:
            raise ControlError(REFUSED, f"card must be one of: {', '.join(CARD_KEYS)}.")
        if not text or not text.strip():
            raise ControlError(REFUSED, "text is required when adding a card item.")
        op: dict[str, Any] = {
            "op": "add_item",
            "card": card,
            "text": text.strip(),
            "data": dict(data or {}),
            "evidence": list(evidence or []),
        }
    elif act == "update":
        if not item_id:
            raise ControlError(REFUSED, "item_id is required when updating a card item.")
        changes: dict[str, Any] = {}
        if text is not None:
            changes["text"] = text.strip()
        if data is not None:
            changes["data"] = dict(data)
        if not changes and not evidence:
            raise ControlError(REFUSED, "Provide text, data, or evidence to update.")
        op = {"op": "update_item", "id": item_id, "set": changes}
        if evidence is not None:
            op["evidence"] = list(evidence)
    elif act in {"remove", "pin", "unpin", "confirm"}:
        if not item_id:
            raise ControlError(REFUSED, f"item_id is required for action {act!r}.")
        op_name = {
            "remove": "remove_item",
            "pin": "pin_item",
            "unpin": "unpin_item",
            "confirm": "confirm_item",
        }[act]
        op = {"op": op_name, "id": item_id}
    else:
        raise ControlError(
            REFUSED, "action must be one of: add, update, remove, pin, unpin, confirm."
        )

    res = _apply_live_op(engine, op)
    return {
        "ok": True,
        "meeting_id": str(getattr(engine, "meeting_id", "") or ""),
        "action": act,
        "seq": res.seq,
        "item_id": res.target_id,
        "item": (res.effect or {}).get("item") if isinstance(res.effect, Mapping) else None,
    }


def manage_meeting_question(
    controller,
    *,
    action: str,
    text: Optional[str] = None,
    question_id: Optional[str] = None,
    answer_text: Optional[str] = None,
    evidence: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Ask, answer, dismiss, or reopen a question in the active meeting inbox."""
    _runtime, engine = _require_live_meeting_engine(controller)
    act = (action or "").strip().lower()
    if act == "ask":
        if not text or not text.strip():
            raise ControlError(REFUSED, "text is required when asking a question.")
        op: dict[str, Any] = {
            "op": "ask_question",
            "text": text.strip(),
            "evidence": list(evidence or []),
        }
    elif act == "answer":
        if not question_id:
            raise ControlError(REFUSED, "question_id is required when answering a question.")
        if not answer_text or not answer_text.strip():
            raise ControlError(REFUSED, "answer_text is required when answering a question.")
        op = {
            "op": "answer_question",
            "question_id": question_id,
            "answer_text": answer_text.strip(),
        }
    elif act == "dismiss":
        if not question_id:
            raise ControlError(REFUSED, "question_id is required when dismissing a question.")
        op = {"op": "dismiss_question", "question_id": question_id}
    elif act == "reopen":
        if not question_id:
            raise ControlError(REFUSED, "question_id is required when reopening a question.")
        op = {"op": "reopen_question", "question_id": question_id}
    else:
        raise ControlError(REFUSED, "action must be one of: ask, answer, dismiss, reopen.")

    res = _apply_live_op(engine, op)
    return {
        "ok": True,
        "meeting_id": str(getattr(engine, "meeting_id", "") or ""),
        "action": act,
        "seq": res.seq,
        "question_id": res.target_id,
        "question": (res.effect or {}).get("question") if isinstance(res.effect, Mapping) else None,
    }


def update_meeting_transcript_or_speaker(
    controller,
    *,
    action: str,
    participant_id: Optional[str] = None,
    display_name: Optional[str] = None,
    segment_id: Optional[str] = None,
    text: Optional[str] = None,
) -> dict[str, Any]:
    """Rename a live meeting participant or reassign a transcript segment's speaker."""
    _runtime, engine = _require_live_meeting_engine(controller)
    act = (action or "").strip().lower()
    if act == "rename_participant":
        if not participant_id or not display_name or not display_name.strip():
            raise ControlError(REFUSED, "participant_id and display_name are required.")
        op: dict[str, Any] = {
            "op": "rename_participant",
            "participant_id": participant_id,
            "display_name": display_name.strip(),
        }
    elif act == "reassign_speaker":
        if not segment_id or not participant_id:
            raise ControlError(REFUSED, "segment_id and participant_id are required.")
        op = {
            "op": "reassign_segment_speaker",
            "segment_id": segment_id,
            "participant_id": participant_id,
        }
    else:
        raise ControlError(
            REFUSED,
            "action must be one of: rename_participant, reassign_speaker.",
        )

    res = _apply_live_op(engine, op)
    return {
        "ok": True,
        "meeting_id": str(getattr(engine, "meeting_id", "") or ""),
        "action": act,
        "seq": res.seq,
        "target_id": res.target_id,
        "effect": res.effect,
    }


def request_meeting_note_adjustment(
    controller,
    *,
    text: str,
    wait: bool = True,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Submit a live note adjustment request to the meeting AI note-taker."""
    _runtime, engine = _require_live_meeting_engine(controller)
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 1000:
        raise ControlError(REFUSED, "Enter a note request of 1 to 1000 characters.")
    try:
        future = engine.request_note_adjustment(cleaned)
    except (ValueError, RuntimeError) as exc:
        raise ControlError(REFUSED, str(exc)) from exc

    meeting_id = str(getattr(engine, "meeting_id", "") or "")
    if not wait:
        return {"ok": True, "status": "queued", "meeting_id": meeting_id}
    try:
        result = future.result(timeout=max(1.0, min(float(timeout_s), 120.0)))
    except TimeoutError:
        return {
            "ok": True,
            "status": "running",
            "meeting_id": meeting_id,
            "message": "Note adjustment is still running; poll get_meeting_live_state to track progress.",
        }
    return {
        "ok": bool(getattr(result, "ok", False)),
        "status": "completed",
        "meeting_id": meeting_id,
        "applied": sum(1 for op in (getattr(result, "op_results", None) or []) if getattr(op, "ok", False)),
        "rejected": sum(1 for op in (getattr(result, "op_results", None) or []) if not getattr(op, "ok", False)),
        "error": getattr(result, "error", None),
    }


def request_meeting_custom_report(
    controller,
    *,
    request_text: str,
    meeting_id: Optional[str] = None,
) -> dict[str, Any]:
    """Start a tailored custom report job on the active or an archived meeting."""
    runtime = getattr(controller, "meeting_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Meeting Mode is not available in this build.")
    from meeting.custom_report import MAX_REQUEST_CHARS, start_custom_report
    from meeting.stored import open_store
    from meeting.web.api import _REPORT_REJECTIONS, _report_endpoint

    cleaned = (request_text or "").strip()
    if not cleaned or len(cleaned) > MAX_REQUEST_CHARS:
        raise ControlError(
            REFUSED, f"Describe the report you want, in 1 to {MAX_REQUEST_CHARS} characters."
        )

    engine = getattr(runtime, "_engine", None)
    engine_mid = str(getattr(engine, "meeting_id", "") or "") or None
    target_id = (meeting_id or "").strip() or engine_mid
    if not target_id:
        raise ControlError(REFUSED, "No active meeting; pass meeting_id to generate a report for a saved meeting.")

    repo = _meeting_repository(controller, runtime)
    if repo is None:
        raise ControlError(UNAVAILABLE, "Meeting history repository is unavailable.")
    meeting = repo.get_meeting(target_id)
    if not meeting:
        raise ControlError(NOT_FOUND, f"Meeting {target_id!r} was not found.")

    store = None
    if engine is not None and engine_mid == target_id and getattr(engine, "store", None) is not None:
        store = engine.store
    else:
        bg = getattr(runtime, "_background_engines", None)
        if isinstance(bg, Mapping) and target_id in bg:
            store = getattr(bg[target_id], "store", None)
    if store is None:
        store = open_store(repo, target_id, meeting, historical=True)

    endpoint = _report_endpoint(engine, meeting)
    result = start_custom_report(
        store,
        repo,
        cleaned,
        provider=endpoint["provider"],
        model=endpoint["model"],
        endpoint=endpoint["endpoint"],
    )
    if not result.get("ok"):
        err_key = str(result.get("error") or "")
        raise ControlError(
            REFUSED,
            _REPORT_REJECTIONS.get(err_key, "The report could not be started."),
        )
    return {
        "ok": True,
        "meeting_id": target_id,
        "report_id": result.get("report_id"),
        "run_id": result.get("run_id"),
        "status": "running",
    }


def undo_live_meeting_action(controller, *, event_seq: int) -> dict[str, Any]:
    """Undo a previous live meeting dashboard action by its sequence number."""
    _runtime, engine = _require_live_meeting_engine(controller)
    me_id = getattr(engine, "_me_participant_id", None)
    results = engine.undo(int(event_seq), me_id)
    if not results or not any(getattr(r, "ok", False) for r in results):
        raise ControlError(REFUSED, f"Action seq {event_seq} could not be undone.")
    return {
        "ok": True,
        "meeting_id": str(getattr(engine, "meeting_id", "") or ""),
        "undone_seq": int(event_seq),
        "applied": sum(1 for r in results if getattr(r, "ok", False)),
    }


# ---- Walkthroughs & Screenshots ---------------------------------------------


def walkthrough_status(controller) -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    trans = getattr(controller, "transcription_runtime", None)
    session = getattr(trans, "recording_walkthrough", None) if trans is not None else None
    active = session is not None
    paused = bool(active and getattr(controller.recorder, "is_paused", False))
    shot_count = int(session.shot_count) if session is not None else 0
    writing_up = bool(getattr(trans, "writing_up", False)) if trans is not None else False
    saved = settings_manager.load_all_settings()
    snap_mode = SettingsKey.WALKTHROUGH_SNAP_MODE.read(saved)
    auto_mode = SettingsKey.WALKTHROUGH_AUTO_MODE.read(saved)
    countdown = int(getattr(runtime, "_countdown_left", 0) or 0) if runtime is not None else 0
    return {
        "active": active,
        "paused": paused,
        "shot_count": shot_count,
        "snap_mode": snap_mode,
        "auto_mode": auto_mode,
        "writing_up": writing_up,
        "countdown_left": countdown,
    }


def start_walkthrough(
    controller,
    *,
    snap_mode: str = "",
    auto_mode: Optional[bool] = None,
    source: str = "mcp",
) -> dict[str, Any]:
    from services.settings import WalkthroughSnapMode

    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Walkthroughs are not available in this build.")
    if controller.transcription_runtime.recording_walkthrough is not None:
        raise ControlError(BUSY, "A walkthrough is already recording.")
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job:
        raise ControlError(BUSY, "Finish the current recording before starting a walkthrough.")
    if controller.is_meeting_active():
        raise ControlError(BUSY, "Meeting Mode is active; end the meeting first.")
    if controller.transcription_runtime.writing_up:
        raise ControlError(BUSY, "Wait for the last walkthrough's write-up to finish, or cancel it.")
    if snap_mode:
        if snap_mode not in WalkthroughSnapMode.ALL:
            raise ControlError(REFUSED, f"Unsupported snap_mode {snap_mode!r}.")
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_SNAP_MODE, snap_mode)
    if auto_mode is not None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_AUTO_MODE, bool(auto_mode))
    unavailable = controller.transcription_readiness_message()
    if unavailable:
        raise ControlError(REFUSED, unavailable)
    runtime.toggle(source=source)
    if controller.transcription_runtime.recording_walkthrough is None:
        raise ControlError(REFUSED, "OpenWhisper could not start a walkthrough.")
    job = controller.transcription_runtime.current_job
    return {
        "started": True,
        "job_id": getattr(job, "id", "") or None,
        "walkthrough_state": walkthrough_status(controller),
    }


def finish_walkthrough(controller) -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None or controller.transcription_runtime.recording_walkthrough is None:
        raise ControlError(REFUSED, "No walkthrough is currently recording.")
    runtime.finish()
    return {"finishing": True}


def cancel_walkthrough(controller) -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    trans = controller.transcription_runtime
    if runtime is None or (trans.recording_walkthrough is None and not trans.writing_up):
        raise ControlError(REFUSED, "No walkthrough or write-up is currently running.")
    runtime.cancel()
    return {"canceled": True}


def set_walkthrough_paused(controller, paused: bool) -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None or controller.transcription_runtime.recording_walkthrough is None:
        raise ControlError(REFUSED, "No walkthrough is currently recording.")
    runtime.set_paused(bool(paused))
    return {"paused": bool(paused), "walkthrough_state": walkthrough_status(controller)}


def snap_walkthrough(controller, *, snap_mode: str = "", source: str = "mcp") -> dict[str, Any]:
    from services.settings import WalkthroughSnapMode

    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None or controller.transcription_runtime.recording_walkthrough is None:
        raise ControlError(REFUSED, "No walkthrough is currently recording.")
    if snap_mode:
        if snap_mode not in WalkthroughSnapMode.ALL:
            raise ControlError(REFUSED, f"Unsupported snap_mode {snap_mode!r}.")
        runtime.choose_snap_mode(snap_mode)
    else:
        runtime.snap(source=source)
    return {"snapped": True, "walkthrough_state": walkthrough_status(controller)}


def choose_walkthrough_snap_mode(controller, mode: str) -> dict[str, Any]:
    from services.settings import WalkthroughSnapMode

    if mode not in WalkthroughSnapMode.ALL:
        raise ControlError(REFUSED, f"Unsupported snap_mode {mode!r}.")
    settings_manager.save_setting(SettingsKey.WALKTHROUGH_SNAP_MODE, mode)
    runtime = getattr(controller, "walkthrough_runtime", None)
    panel = getattr(runtime, "_panel", None) if runtime is not None else None
    if panel is not None and callable(getattr(panel, "set_snap_mode", None)):
        panel.set_snap_mode(mode)
    return {"snap_mode": mode}


def edit_walkthrough_shots(
    controller, action: str, *, number: Optional[int] = None, step: Optional[int] = None
) -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None or controller.transcription_runtime.recording_walkthrough is None:
        raise ControlError(REFUSED, "No walkthrough is currently recording.")
    if action == "undo":
        runtime.undo_last_shot()
    elif action == "remove":
        if number is None or number < 1:
            raise ControlError(REFUSED, "Provide a valid positive shot number to remove.")
        runtime.remove_shot(int(number))
    elif action == "move":
        if number is None or number < 1 or step not in (-1, 1):
            raise ControlError(REFUSED, "Provide a valid shot number and step (-1 or 1) to move.")
        runtime.move_shot(int(number), int(step))
    else:
        raise ControlError(REFUSED, "action must be 'undo', 'remove', or 'move'.")
    return {"ok": True, "walkthrough_state": walkthrough_status(controller)}


def take_screenshot(controller, *, source: str = "mcp") -> dict[str, Any]:
    runtime = getattr(controller, "walkthrough_runtime", None)
    if runtime is None:
        raise ControlError(UNAVAILABLE, "Screenshots are not available in this build.")
    if controller.transcription_runtime.recording_walkthrough is not None:
        raise ControlError(BUSY, "A walkthrough is recording; use snap_walkthrough instead.")
    runtime.snap(source=source)
    return {"started": True}


def _walkthrough_title(folder: Path, document) -> str:
    import re
    from services.walkthrough.write_up import MARKDOWN_NAME as WRITEUP_MD

    heading_re = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
    try:
        heading = heading_re.search((folder / WRITEUP_MD).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        heading = None
    if heading:
        return heading.group(1)
    said = " ".join(" ".join(s.text.split()) for s in document.sections if s.text.strip())
    if len(said) <= 90:
        return said or "Untitled walkthrough"
    return said[:90].rsplit(" ", 1)[0] + "…"


def list_walkthroughs(*, q: str = "", limit: int = 20, root: Optional[Path] = None) -> dict[str, Any]:
    from services.walkthrough import library
    from services.walkthrough.storage import captures_root

    base = Path(root) if root is not None else captures_root()
    folders = library.entries(base)
    numbers = library.folder_numbers(base)
    needle = (q or "").strip().casefold()
    items: list[dict[str, Any]] = []
    for folder in folders:
        num = numbers.get(folder)
        ref = library.format_number(num)
        draft = (folder / library.DRAFT_NAME).exists()
        try:
            doc = library.load_document(folder)
        except (OSError, ValueError, KeyError, TypeError):
            doc = None
        if doc is None:
            title = "Interrupted walkthrough" if draft else "Unreadable walkthrough"
            haystack = f"{ref} {folder.name} {title}".casefold()
            if needle and needle not in haystack:
                continue
            items.append({
                "ref": ref,
                "number": num,
                "id": folder.name,
                "title": title,
                "started_at": None,
                "duration_s": 0.0,
                "shots": 0,
                "written_up": False,
                "draft": draft,
            })
        else:
            title = _walkthrough_title(folder, doc)
            shots = sum(1 for s in doc.sections if s.shot is not None)
            written_up = library.written_up(folder)
            if needle:
                haystack = " ".join([ref, folder.name, title, *(s.text for s in doc.sections)]).casefold()
                if needle not in haystack:
                    continue
            items.append({
                "ref": ref,
                "number": num,
                "id": folder.name,
                "title": title,
                "started_at": doc.started_at.isoformat(),
                "duration_s": round(float(doc.duration_s), 1),
                "shots": shots,
                "written_up": written_up,
                "draft": draft,
            })
        if len(items) >= limit:
            break
    return {"walkthroughs": items, "total": len(folders)}


def get_walkthrough(
    ref: str,
    *,
    spoken: bool = False,
    include_images: bool = False,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    from services.walkthrough import library
    from services.walkthrough.storage import MARKDOWN_NAME, captures_root
    from services.walkthrough.write_up import MARKDOWN_NAME as WRITEUP_MD

    base = Path(root) if root is not None else captures_root()
    folder = library.resolve_folder(base, ref)
    if folder is None:
        raise ControlError(NOT_FOUND, f"Walkthrough {ref!r} was not found.")
    try:
        doc = library.load_document(folder)
    except (OSError, ValueError, KeyError, TypeError):
        raise ControlError(UNAVAILABLE, "This walkthrough is incomplete or cannot be read.") from None
    num = doc.number if doc.number is not None else library.number_for(folder, base)
    formatted_ref = library.format_number(num)
    written = library.written_up(folder)
    md_file = folder / (WRITEUP_MD if (written and not spoken) else MARKDOWN_NAME)
    markdown_text = ""
    if md_file.is_file():
        try:
            markdown_text = md_file.read_text(encoding="utf-8")
        except OSError:
            markdown_text = ""

    sections = []
    images: list[dict[str, Any]] = []
    total_bytes = 0
    images_truncated = False
    shot_index = 0
    for idx, section in enumerate(doc.sections, start=1):
        shot_name = section.shot.name if section.shot is not None else None
        if shot_name is not None:
            shot_index += 1
            if include_images:
                if len(images) >= _MAX_IMAGES_RETURNED:
                    images_truncated = True
                else:
                    try:
                        raw = section.shot.read_bytes()
                        if len(raw) <= _MAX_IMAGE_FILE_BYTES and total_bytes + len(raw) <= _MAX_IMAGE_BYTES_TOTAL:
                            total_bytes += len(raw)
                            images.append({
                                "shot": shot_name,
                                "number": shot_index,
                                "mime_type": "image/png",
                                "size_bytes": len(raw),
                                "data_base64": base64.b64encode(raw).decode("ascii"),
                            })
                        else:
                            images_truncated = True
                    except OSError:
                        pass
        sections.append({
            "index": idx,
            "text": section.text,
            "shot": shot_name,
            "kept_at_s": section.kept_at_s,
        })

    payload: dict[str, Any] = {
        "ref": formatted_ref,
        "number": num,
        "id": folder.name,
        "title": _walkthrough_title(folder, doc),
        "started_at": doc.started_at.isoformat(),
        "duration_s": round(float(doc.duration_s), 1),
        "shots": shot_index,
        "written_up": written,
        "showing_write_up": bool(written and not spoken),
        "markdown": markdown_text,
        "sections": sections,
    }
    if include_images:
        payload["images"] = images
        if images_truncated:
            payload["images_truncated"] = True
    return payload


def run_walkthrough_writeup(
    controller, ref: str, *, style: str = "", root: Optional[Path] = None
) -> dict[str, Any]:
    from services.automation import catalog, events
    from services.walkthrough import library, styles as walkthrough_styles
    from services.walkthrough.storage import captures_root

    base = Path(root) if root is not None else captures_root()
    folder = library.resolve_folder(base, ref)
    if folder is None:
        raise ControlError(NOT_FOUND, f"Walkthrough {ref!r} was not found.")
    if not (folder / library.MANIFEST_NAME).is_file():
        raise ControlError(REFUSED, "This walkthrough is incomplete and cannot be written up yet.")

    saved = settings_manager.load_all_settings()
    instructions: Optional[str] = None
    chosen_style = ""
    if style.strip():
        available = walkthrough_styles.load_styles(saved)
        needle = style.strip().casefold()
        match = next(
            (s for s in available if s.id.casefold() == needle or s.name.casefold() == needle),
            None,
        )
        if match is None:
            names = ", ".join(s.name for s in available)
            raise ControlError(NOT_FOUND, f"Unknown write-up style {style!r}. Available: {names}.")
        instructions = match.instructions
        chosen_style = match.name

    num = library.number_for(folder, base)
    formatted_ref = library.format_number(num)

    def work() -> None:
        try:
            library.retry_write_up(folder, instructions=instructions)
            events.publish(
                catalog.WALKTHROUGH_WRITEUP_COMPLETED,
                catalog.walkthrough_writeup_completed(
                    folder_name=folder.name, number=num, ref=formatted_ref, ok=True,
                ),
            )
        except Exception as exc:
            logger.warning("MCP walkthrough write-up failed: %s", type(exc).__name__)
            events.publish(
                catalog.WALKTHROUGH_WRITEUP_COMPLETED,
                catalog.walkthrough_writeup_completed(
                    folder_name=folder.name, number=num, ref=formatted_ref, ok=False,
                    error=str(exc) or "Write-up failed",
                ),
            )

    executor = getattr(controller, "walkthrough_executor", None)
    if executor is not None:
        try:
            executor.submit(work)
        except RuntimeError:
            raise ControlError(UNAVAILABLE, "OpenWhisper is shutting down.") from None
    else:
        threading.Thread(target=work, name="mcp-walkthrough-writeup", daemon=True).start()

    return {
        "started": True,
        "ref": formatted_ref,
        "number": num,
        "id": folder.name,
        "style": chosen_style or "default",
    }


# ---- Profiles, Transforms & Audio Files -------------------------------------


def list_profiles() -> list[dict[str, Any]]:
    from services.cleanup_profiles import load_cleanup_profiles

    saved = settings_manager.load_all_settings()
    active = SettingsKey.QUICK_RECORD_PROFILE.read(saved)
    return [
        {
            "id": p.id,
            "name": p.name,
            "hotkey": p.hotkey,
            "use_learned_rules": p.use_learned_rules,
            "active_for_quick_record": p.id == active,
        }
        for p in load_cleanup_profiles(saved)
    ]


def list_transforms() -> list[dict[str, Any]]:
    from services.text_transforms import load_transforms

    saved = settings_manager.load_all_settings()
    return [
        {"id": t.id, "name": t.name, "hotkey": t.hotkey}
        for t in load_transforms(saved)
    ]


def run_transform(controller, transform_id: str, text: str) -> str:
    from services.text_transforms import find_transform

    saved = settings_manager.load_all_settings()
    transform = find_transform(saved, transform_id)
    if transform is None:
        raise ControlError(NOT_FOUND, f"No saved transform has id {transform_id!r}.")
    if not text.strip():
        raise ControlError(REFUSED, "Provide non-empty text to transform.")
    cmd = getattr(controller, "command_runtime", None)
    if cmd is None:
        raise ControlError(UNAVAILABLE, "Text transforms are not available right now.")
    try:
        rewritten, _orig, _info = cmd.rewrite_text(text, transform.instruction)
    except Exception as exc:
        raise ControlError(REFUSED, str(exc) or "Transform could not run.") from None
    return rewritten


def transcribe_audio_file(
    controller, path: str, *, language: Optional[str] = None
) -> dict[str, Any]:
    if not isinstance(path, str) or not path.strip():
        raise ControlError(REFUSED, "Provide a path to an audio file.")
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ControlError(NOT_FOUND, "Audio file not found.")
    if candidate.suffix.lower() not in _AUDIO_EXTENSIONS:
        raise ControlError(REFUSED, "Unsupported audio file extension.")
    if controller.is_meeting_active():
        raise ControlError(BUSY, "Meeting Mode is active; end the meeting first.")
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job:
        raise ControlError(BUSY, "OpenWhisper is busy with another recording or transcription.")
    resolved = str(candidate.resolve())
    controller.upload_audio_file(resolved)
    return {"queued": True, "filename": candidate.name}


# ---- Personalization --------------------------------------------------------


def get_personalization_summary() -> dict[str, Any]:
    from config import config

    saved = settings_manager.load_all_settings()
    dictionary_data = list_dictionary()
    snippets_data = list_snippets()
    styles_data = get_app_styles()
    languages_data = list_languages()
    profiles_data = list_profiles()
    wt_styles = list_walkthrough_styles()
    return {
        "dictionary": {
            **dictionary_data,
            "count": len(dictionary_data["terms"]),
            "max_terms": config.MAX_DICTIONARY_TERMS,
        },
        "snippets": {
            **snippets_data,
            "count": len(snippets_data["snippets"]),
            "max_snippets": config.MAX_SNIPPETS,
        },
        "app_styles": styles_data,
        "languages": languages_data,
        "profiles": profiles_data,
        "walkthrough_styles": wt_styles["styles"],
        "meeting_context": {
            "past_recall_enabled": SettingsKey.MEETING_PAST_RECALL_ENABLED.read(saved),
            "context_folder_enabled": SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED.read(saved),
        },
    }


def list_dictionary() -> dict[str, Any]:
    from services import dictionary

    saved = settings_manager.load_all_settings()
    terms = dictionary.load_dictionary(saved)
    return {
        "learn_enabled": SettingsKey.DICTIONARY_LEARN_ENABLED.read(saved),
        "steer_recognition": SettingsKey.DICTIONARY_STEER_RECOGNITION.read(saved),
        "terms": [
            {
                "id": t.id,
                "term": t.term,
                "starred": t.starred,
                "heard": list(t.heard),
                "learned": t.learned,
                "protected": t.protected,
            }
            for t in terms
        ],
    }


def add_dictionary_term(
    controller, term: str, *, heard: Sequence[str] = (), starred: bool = False
) -> dict[str, Any]:
    from services import dictionary

    def commit(saved):
        return dictionary.add_term(saved, term, heard=tuple(heard), starred=bool(starred))

    try:
        added = settings_manager.mutate_settings(commit)
    except dictionary.DictionaryError as exc:
        raise ControlError(REFUSED, str(exc)) from None
    _refresh_settings_ui(controller)
    return {
        "id": added.id,
        "term": added.term,
        "starred": added.starred,
        "heard": list(added.heard),
    }


def bulk_add_dictionary_terms(
    controller, items: Sequence[Mapping[str, Any]], *, dry_run: bool = False
) -> dict[str, Any]:
    import copy
    from services import dictionary

    if not items or len(items) > 50:
        raise ControlError(REFUSED, "Provide between 1 and 50 dictionary terms per call.")

    def apply(saved: dict[str, Any]) -> dict[str, Any]:
        added: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw in items:
            term = str(raw.get("term") or "")
            heard_raw = raw.get("heard") or ()
            heard = tuple(str(h) for h in heard_raw) if isinstance(heard_raw, (list, tuple)) else ()
            starred = bool(raw.get("starred", False))
            before = {t.term.casefold(): t for t in dictionary.load_dictionary(saved)}
            existed = before.get(" ".join(term.split()).casefold())
            try:
                entry = dictionary.add_term(saved, term, heard=heard, starred=starred)
            except dictionary.DictionaryError as exc:
                msg = str(exc)
                if "already in your dictionary" in msg.lower():
                    skipped.append({"term": " ".join(term.split()), "reason": msg})
                else:
                    rejected.append({"term": term, "reason": msg})
                continue
            row = {"id": entry.id, "term": entry.term, "starred": entry.starred, "heard": list(entry.heard)}
            if existed is not None:
                updated.append(row)
            else:
                added.append(row)
        return {
            "dry_run": bool(dry_run),
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "rejected": rejected,
            "total_after": len(dictionary.load_dictionary(saved)),
        }

    if dry_run:
        return apply(copy.deepcopy(settings_manager.load_all_settings()))
    result = settings_manager.mutate_settings(apply)
    if result["added"] or result["updated"]:
        _refresh_settings_ui(controller)
    return result


def remove_dictionary_term(controller, term_or_id: str) -> dict[str, Any]:
    from services import dictionary

    needle = (term_or_id or "").strip()
    if not needle:
        raise ControlError(REFUSED, "Provide a dictionary term id or spelling.")

    def commit(saved):
        terms = dictionary.load_dictionary(saved)
        target = next((t for t in terms if t.id == needle), None)
        if target is None:
            target = next((t for t in terms if t.term.casefold() == needle.casefold()), None)
        if target is None:
            raise ControlError(NOT_FOUND, "That word is not in your dictionary.")
        return dictionary.delete_term(saved, target.id)

    removed = settings_manager.mutate_settings(commit)
    _refresh_settings_ui(controller)
    return {"removed": True, "id": removed.id if removed else needle, "term": removed.term if removed else ""}


def list_snippets() -> dict[str, Any]:
    from services import snippets

    saved = settings_manager.load_all_settings()
    return {
        "enabled": SettingsKey.SNIPPETS_ENABLED.read(saved),
        "snippets": [s.to_dict() for s in snippets.load_snippets(saved)],
    }


def upsert_snippet(
    controller,
    trigger: str,
    text: str,
    *,
    snippet_id: str = "",
    formatted: bool = False,
) -> dict[str, Any]:
    from services import snippets

    sid = snippet_id.strip() or snippets.new_snippet_id()
    entry = snippets.Snippet(sid, trigger, text, bool(formatted))

    def commit(saved):
        snippets.save_snippet(saved, entry)
        return entry

    try:
        saved_entry = settings_manager.mutate_settings(commit)
    except ValueError as exc:
        raise ControlError(REFUSED, str(exc)) from None
    _refresh_settings_ui(controller)
    return saved_entry.to_dict()


def bulk_upsert_snippets(
    controller,
    items: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> dict[str, Any]:
    import copy
    from services import snippets

    if not items or len(items) > 20:
        raise ControlError(REFUSED, "Provide between 1 and 20 snippets per call.")

    def apply(saved: dict[str, Any]) -> dict[str, Any]:
        added: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw in items:
            trigger = str(raw.get("trigger") or "")
            text = str(raw.get("text") or "")
            formatted = bool(raw.get("formatted", False))
            existing_list = snippets.load_snippets(saved)
            dup = snippets.find_duplicate(trigger, existing_list)
            if dup is not None and skip_existing:
                skipped.append({"trigger": trigger.strip(), "id": dup.id, "reason": "Trigger already exists."})
                continue
            sid = dup.id if dup is not None else snippets.new_snippet_id()
            entry = snippets.Snippet(sid, trigger, text, formatted)
            try:
                snippets.save_snippet(saved, entry)
            except ValueError as exc:
                rejected.append({"trigger": trigger, "reason": str(exc)})
                continue
            saved_row = next((s.to_dict() for s in snippets.load_snippets(saved) if s.id == sid), entry.to_dict())
            if dup is not None:
                updated.append(saved_row)
            else:
                added.append(saved_row)
        return {
            "dry_run": bool(dry_run),
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "rejected": rejected,
            "total_after": len(snippets.load_snippets(saved)),
        }

    if dry_run:
        return apply(copy.deepcopy(settings_manager.load_all_settings()))
    result = settings_manager.mutate_settings(apply)
    if result["added"] or result["updated"]:
        _refresh_settings_ui(controller)
    return result


def delete_snippet(controller, snippet_id: str) -> dict[str, Any]:
    from services import snippets

    sid = (snippet_id or "").strip()
    if not sid:
        raise ControlError(REFUSED, "Provide a snippet id.")

    def commit(saved):
        existing = snippets.load_snippets(saved)
        if all(s.id != sid for s in existing):
            raise ControlError(NOT_FOUND, "Snippet not found.")
        snippets.delete_snippet(saved, sid)

    settings_manager.mutate_settings(commit)
    _refresh_settings_ui(controller)
    return {"deleted": True, "id": sid}


def get_app_styles() -> dict[str, Any]:
    from services import app_styles

    saved = settings_manager.load_all_settings()
    return {
        "enabled": SettingsKey.APP_STYLES_ENABLED.read(saved),
        "tones": app_styles.resolve_tones(saved),
        "overrides": [
            {"match": match, "category": category}
            for match, category in app_styles.resolve_overrides(saved)
        ],
        "available_categories": list(app_styles.AppCategory.ALL),
        "available_tones": list(app_styles.Tone.ALL),
    }


def set_app_style_tone(controller, category: str, tone: str) -> dict[str, Any]:
    from services import app_styles

    def commit(saved):
        app_styles.set_tone(saved, category, tone)
        return app_styles.resolve_tones(saved)

    try:
        tones = settings_manager.mutate_settings(commit)
    except ValueError as exc:
        raise ControlError(REFUSED, str(exc)) from None
    _refresh_settings_ui(controller)
    return {"category": category, "tone": tone, "tones": tones}


def set_app_style_override(controller, match: str, category: str) -> dict[str, Any]:
    from services import app_styles

    def commit(saved):
        app_styles.set_override(saved, match, category)
        return [{"match": m, "category": c} for m, c in app_styles.resolve_overrides(saved)]

    try:
        overrides = settings_manager.mutate_settings(commit)
    except ValueError as exc:
        raise ControlError(REFUSED, str(exc)) from None
    _refresh_settings_ui(controller)
    return {"match": match.strip(), "category": category, "overrides": overrides}


def remove_app_style_override(controller, match: str) -> dict[str, Any]:
    from services import app_styles

    def commit(saved):
        app_styles.remove_override(saved, match)
        return [{"match": m, "category": c} for m, c in app_styles.resolve_overrides(saved)]

    overrides = settings_manager.mutate_settings(commit)
    _refresh_settings_ui(controller)
    return {"removed": True, "match": match.strip(), "overrides": overrides}


def list_walkthrough_styles() -> dict[str, Any]:
    from services.walkthrough import styles as wt_styles

    saved = settings_manager.load_all_settings()
    return {
        "styles": [
            {
                "id": s.id,
                "name": s.name,
                "instructions": s.instructions,
                "built_in": s.id == wt_styles.DEFAULT_STYLE.id,
            }
            for s in wt_styles.load_styles(saved)
        ],
    }


def upsert_walkthrough_style(controller, name: str, instructions: str) -> dict[str, Any]:
    from services.walkthrough import styles as wt_styles

    def commit(saved):
        return wt_styles.save_style(saved, name, instructions)

    try:
        saved_style = settings_manager.mutate_settings(commit)
    except ValueError as exc:
        raise ControlError(REFUSED, str(exc)) from None
    _refresh_settings_ui(controller)
    return saved_style.to_dict()


def delete_walkthrough_style(controller, style_id: str) -> dict[str, Any]:
    from services.walkthrough import styles as wt_styles

    sid = (style_id or "").strip()
    if not sid or sid == wt_styles.DEFAULT_STYLE.id:
        raise ControlError(REFUSED, "The built-in Walkthrough style cannot be deleted.")

    def commit(saved):
        existing = wt_styles.saved_styles(saved)
        if all(s.id != sid for s in existing):
            raise ControlError(NOT_FOUND, "Walkthrough write-up style not found.")
        wt_styles.delete_style(saved, sid)

    settings_manager.mutate_settings(commit)
    _refresh_settings_ui(controller)
    return {"deleted": True, "id": sid}


def list_languages() -> dict[str, Any]:
    from services import dictation_language

    saved = settings_manager.load_all_settings()
    return {
        "current": dictation_language.current_language(saved),
        "active_override": dictation_language.job_language(saved) or None,
        "engine_language": dictation_language.engine_language(saved),
        "chosen": dictation_language.chosen_languages(saved),
        "choices": dictation_language.language_choices(saved),
        "accepted": list(dictation_language.accepted_languages(saved)),
    }


def set_active_language(controller, code: str) -> dict[str, Any]:
    from services import dictation_language

    code = (code or "").strip()
    if not code:
        def clear(saved):
            saved[SettingsKey.DICTATION_ACTIVE_LANGUAGE] = ""
        settings_manager.mutate_settings(clear)
        _refresh_settings_ui(controller)
        return list_languages()

    def commit(saved):
        accepted = set(dictation_language.accepted_languages(saved))
        if code not in accepted:
            raise ControlError(
                REFUSED,
                f"Language {code!r} is not supported by the current speech engine.",
            )
        if code not in dictation_language.chosen_languages(saved):
            dictation_language.add_chosen(saved, code)
        dictation_language.set_active(saved, code)

    settings_manager.mutate_settings(commit)
    _refresh_settings_ui(controller)
    return list_languages()


def set_quick_record_profile(controller, profile_id: str) -> dict[str, Any]:
    from services.cleanup_profiles import find_cleanup_profile

    pid = (profile_id or "").strip()
    saved = settings_manager.load_all_settings()
    if pid and find_cleanup_profile(saved, pid) is None:
        raise ControlError(NOT_FOUND, f"No cleanup profile has id {pid!r}.")
    settings_manager.save_setting(SettingsKey.QUICK_RECORD_PROFILE, pid)
    _refresh_settings_ui(controller)
    return {"quick_record_profile": pid or None}


# ---- Engines, Models, Audio Devices, Remote & Window ------------------------


def engine_status(controller) -> dict[str, Any]:
    saved = settings_manager.load_all_settings()
    selected_engine = SettingsKey.SELECTED_MODEL.read(saved)
    loaded_local = None
    models_rt = getattr(controller, "models", None)
    if models_rt is not None and callable(getattr(models_rt, "get_loaded_local_model", None)):
        try:
            loaded_local = models_rt.get_loaded_local_model()
        except Exception:
            loaded_local = None
    readiness = None
    if callable(getattr(controller, "transcription_readiness_message", None)):
        try:
            readiness = controller.transcription_readiness_message()
        except Exception:
            readiness = None
    return {
        "engine": selected_engine,
        "whisper_model": SettingsKey.WHISPER_MODEL.read(saved),
        "local_asr_models": dict(SettingsKey.LOCAL_ASR_MODELS.read(saved)),
        "loaded_local_model": loaded_local,
        "ready": readiness is None,
        "readiness_message": readiness,
        "transcribing": bool(controller.is_transcribing()),
    }


def list_models(controller) -> dict[str, Any]:
    from config import config

    saved = settings_manager.load_all_settings()
    engines = [
        {"id": engine_id, "label": label}
        for label, engine_id in config.MODEL_VALUE_MAP.items()
        if label in config.MODEL_CHOICES
    ]
    whisper_models: list[dict[str, Any]] = []
    try:
        from services.hf_access import is_model_cached
        from services.model_catalog import MODEL_CATALOG

        for name, info in MODEL_CATALOG.items():
            whisper_models.append({
                "id": name,
                "label": getattr(info, "display_name", name),
                "language_support": getattr(info, "language_support", ""),
                "downloaded": bool(is_model_cached(name)),
            })
    except Exception:
        logger.warning("Failed to list Whisper models", exc_info=True)
    local_asr: list[dict[str, Any]] = []
    try:
        from services.local_asr.catalog import MODELS

        for name, info in MODELS.items():
            local_asr.append({
                "id": name,
                "backend": getattr(info, "backend", ""),
                "label": getattr(info, "label", name),
            })
    except Exception:
        logger.warning("Failed to list local ASR models", exc_info=True)
    return {
        "selected_engine": SettingsKey.SELECTED_MODEL.read(saved),
        "selected_whisper_model": SettingsKey.WHISPER_MODEL.read(saved),
        "engines": engines,
        "whisper_models": whisper_models,
        "local_asr_models": local_asr,
    }


def select_model(controller, engine: str, *, model: str = "") -> dict[str, Any]:
    from config import config

    if controller.recorder.is_recording or controller.is_transcribing():
        raise ControlError(BUSY, "Finish recording or transcription before changing engines.")
    if controller.is_meeting_active():
        raise ControlError(BUSY, "End Meeting Mode before changing dictation engines.")
    valid_engines = set(config.MODEL_VALUE_MAP.values())
    engine_id = (engine or "").strip()
    if engine_id not in valid_engines:
        raise ControlError(
            REFUSED,
            f"Unsupported engine {engine_id!r}. Supported: {', '.join(sorted(valid_engines))}.",
        )
    if model.strip():
        model_id = model.strip()
        if engine_id == "local_whisper":
            settings_manager.save_setting(SettingsKey.WHISPER_MODEL, model_id)
        else:
            def save_local(saved):
                mapping = dict(SettingsKey.LOCAL_ASR_MODELS.read(saved))
                mapping[engine_id] = model_id
                saved[SettingsKey.LOCAL_ASR_MODELS] = mapping
            settings_manager.mutate_settings(save_local)
    settings_manager.save_setting(SettingsKey.SELECTED_MODEL, engine_id)
    controller.on_model_changed(engine_id)
    _refresh_settings_ui(controller)
    return engine_status(controller)


def download_model(controller, model_name: str) -> dict[str, Any]:
    from services.settings import HuggingFaceAccessPolicy

    name = (model_name or "").strip()
    if not name:
        raise ControlError(REFUSED, "Provide a model name to download.")
    saved = settings_manager.load_all_settings()
    policy = SettingsKey.HF_ACCESS_POLICY.read(saved)
    if policy == HuggingFaceAccessPolicy.NEVER:
        raise ControlError(REFUSED, "Model downloads are disabled by your Hugging Face access policy.")
    models_rt = getattr(controller, "models", None)
    if models_rt is None:
        raise ControlError(UNAVAILABLE, "Model downloads are not available right now.")
    models_rt.request_model_download(name)
    return {"started": True, "model": name}


def list_audio_devices() -> dict[str, Any]:
    from services import audio_devices

    saved = settings_manager.load_all_settings()
    priority = audio_devices.normalize_priority(SettingsKey.AUDIO_INPUT_PRIORITY.read(saved))
    try:
        devices = audio_devices.list_input_devices()
    except Exception as exc:
        raise ControlError(UNAVAILABLE, f"Could not query audio devices ({type(exc).__name__}).") from None
    return {
        "priority": priority,
        "devices": [
            {
                "name": dev.name,
                "display": dev.display,
                "hostapi": dev.hostapi,
                "channels": dev.channels,
                "samplerate": dev.samplerate,
                "default_api": dev.default_api,
            }
            for dev in devices
        ],
    }


def select_audio_device(controller, name: str, *, hostapi: str = "") -> dict[str, Any]:
    from services import audio_devices

    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job:
        raise ControlError(BUSY, "Finish the current recording before changing the microphone.")
    needle = (name or "").strip()
    if not needle:
        raise ControlError(REFUSED, "Provide a microphone name.")
    devices = audio_devices.list_input_devices()
    match = audio_devices.match_entry({"name": needle, "hostapi": hostapi.strip()}, devices)
    if match is None:
        match = next(
            (d for d in devices if d.display.casefold() == needle.casefold() or d.name.casefold() == needle.casefold()),
            None,
        )
    if match is None:
        raise ControlError(NOT_FOUND, f"No input device matched {needle!r}.")
    chosen = match.key

    def commit(saved):
        current = audio_devices.normalize_priority(SettingsKey.AUDIO_INPUT_PRIORITY.read(saved))
        rest = [entry for entry in current if entry != chosen]
        saved[SettingsKey.AUDIO_INPUT_PRIORITY] = [chosen, *rest]

    settings_manager.mutate_settings(commit)
    controller.change_audio_device()
    _refresh_settings_ui(controller)
    return {"selected": chosen, "display": match.display}


def remote_status(controller) -> dict[str, Any]:
    saved = settings_manager.load_all_settings()
    client_cfg = SettingsKey.REMOTE_ENGINE_CLIENT.read(saved)
    host_enabled = SettingsKey.REMOTE_HOST_ENABLED.read(saved)
    remote_rt = getattr(controller, "remote_runtime", None)
    engine = getattr(controller, "remote_engine", None)
    connected = False
    host_name = ""
    if isinstance(client_cfg, Mapping):
        host_name = str(client_cfg.get("host_name") or client_cfg.get("name") or "")
    if engine is not None:
        status_fn = getattr(engine, "client_status", None)
        if callable(status_fn):
            try:
                st = status_fn()
                connected = bool(getattr(st, "connected", False))
            except Exception:
                pass
    return {
        "host_mode_enabled": host_enabled,
        "paired_to_host": bool(client_cfg),
        "paired_host_name": host_name or None,
        "connected": connected,
        "restoring": bool(remote_rt.restoring()) if remote_rt is not None and callable(getattr(remote_rt, "restoring", None)) else False,
    }


def reconnect_remote(controller) -> dict[str, Any]:
    remote_rt = getattr(controller, "remote_runtime", None)
    if remote_rt is None or not callable(getattr(remote_rt, "retry_now", None)):
        raise ControlError(UNAVAILABLE, "Remote engine runtime is not available.")
    saved = settings_manager.load_all_settings()
    if not SettingsKey.REMOTE_ENGINE_CLIENT.read(saved):
        raise ControlError(REFUSED, "This computer is not paired with a remote host.")
    remote_rt.retry_now()
    return {"reconnecting": True}


def control_window(
    controller, action: str, *, text: str = "", tab: str = ""
) -> dict[str, Any]:
    from ui_qt.widgets.tabbed_content import TabbedContentWidget

    ui = getattr(controller, "ui_controller", None)
    if ui is None:
        raise ControlError(UNAVAILABLE, "Desktop window is not available.")
    if action == "show":
        ui.show_main_window()
        return {"ok": True, "action": action}
    if action == "minimize_to_tray":
        controller.minimize_to_tray()
        return {"ok": True, "action": action}
    if action == "open_scratchpad":
        if text:
            ui.show_in_scratchpad(text)
        else:
            ui.toggle_scratchpad()
        return {"ok": True, "action": action}
    if action == "show_in_scratchpad":
        if not text:
            raise ControlError(REFUSED, "Provide text to show in the Scratchpad.")
        ui.show_in_scratchpad(text)
        return {"ok": True, "action": action}
    if action == "set_compact_mode":
        ui.main_window.set_compact_mode(True, persist=True)
        return {"ok": True, "action": action, "compact_mode": True}
    if action == "set_normal_mode":
        ui.main_window.set_compact_mode(False, persist=True)
        return {"ok": True, "action": action, "compact_mode": False}
    if action == "switch_tab":
        tab_map = {
            "quick_record": TabbedContentWidget.TAB_QUICK_RECORD,
            "upload": TabbedContentWidget.TAB_UPLOAD_FILE,
            "walkthrough": TabbedContentWidget.TAB_WALKTHROUGH,
            "meeting": TabbedContentWidget.TAB_MEETING_MODE,
        }
        key = (tab or "").strip().lower()
        if key not in tab_map:
            raise ControlError(
                REFUSED,
                f"Unknown tab {tab!r}. Choose one of: {', '.join(tab_map)}.",
            )
        ui.show_main_window()
        ui.switch_to_tab(tab_map[key])
        return {"ok": True, "action": action, "tab": key}
    raise ControlError(
        REFUSED,
        "action must be one of: show, minimize_to_tray, open_scratchpad, "
        "show_in_scratchpad, set_compact_mode, set_normal_mode, switch_tab.",
    )
