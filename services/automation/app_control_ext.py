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
    bg = getattr(runtime, "_background_engines", None)
    bg_count = len(bg) if isinstance(bg, Mapping) else 0
    fin = getattr(runtime, "_finalization", None)
    fin_summary = None
    if isinstance(fin, Mapping):
        fin_summary = {
            "status": str(fin.get("status") or ""),
            "message": str(fin.get("message") or ""),
        }
    return {
        "active": active,
        "starting": starting,
        "paused": paused,
        "finalizing": finalizing,
        "meeting_id": meeting_id or getattr(runtime, "_card_meeting_id", None),
        "status": status,
        "elapsed_s": elapsed_s,
        "background_count": bg_count,
        "finalization": fin_summary,
    }


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
