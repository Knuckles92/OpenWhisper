"""Allowlisted JSON commands for the toolkit-independent native desktop.

The transport owns authentication (private parent/child pipes), request size,
thread dispatch and errors. This module owns parameter validation and invokes
the same domain services as the desktop application.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from . import collections, helpers, history, meetings, models, settings, system, walkthroughs
from .common import boolean, changed, identifier, integer, serializable, string


def _idle(context):
    controller = context.controller
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job or controller.is_meeting_active():
        raise ValueError("Finish the current recording or transcription first.")


def _record_start(context, params):
    _idle(context)
    profile_id = params.get("profile_id", "")
    action_id = params.get("action_id", "")
    string(profile_id, "profile ID", maximum=200)
    string(action_id, "action ID", maximum=200)
    if profile_id and action_id:
        raise ValueError("Choose either a profile or an action.")
    if profile_id:
        started = context.controller.start_profile_recording(profile_id, source="ui")
    elif action_id:
        started = context.controller.start_action_recording(action_id, source="ui")
    else:
        started = context.controller.start_recording(source="ui")
    if started is False:
        raise ValueError(context.ui.state.get("status") or "Recording could not start.")
    return {"ok": True}


def _record_stop(context, _params):
    if not context.controller.recorder.is_recording:
        raise ValueError("No recording is active.")
    context.controller.stop_recording()
    return {"ok": True}


def _cancel(context, _params):
    context.controller.cancel()
    return {"ok": True}


def _audio_path(value):
    path = Path(string(value, "audio path", required=True, maximum=4096)).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Choose an existing audio or video file.")
    return str(path.resolve())


def _file_transcribe(context, params):
    _idle(context)
    unavailable = context.controller.transcription_readiness_message()
    if unavailable:
        raise ValueError(unavailable)
    path = _audio_path(params.get("path"))
    context.controller.upload_audio_file(path)
    return changed("history.list")


def _file_enqueue(context, params):
    from services.batch_upload import BatchItem, BatchUploadRequest, BatchRelation
    _idle(context)
    unavailable = context.controller.transcription_readiness_message()
    if unavailable:
        raise ValueError(unavailable)
    paths = params.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
        raise ValueError("Choose between 1 and 100 audio files.")
    paths = [_audio_path(path) for path in paths]
    relation = params.get("relation", "separate")
    if relation not in BatchRelation.ALL:
        raise ValueError("Unknown file grouping.")
    instructions = string(params.get("instructions", ""), "batch instructions", maximum=30_000)
    combine = boolean(params.get("combine", False), "combine")
    queue = {"active": True, "items": [{"id": uuid4().hex, "path": path,
                "name": Path(path).name, "status": "queued", "error": ""} for path in paths]}
    queue["items"][0]["status"] = "running"
    context.ui.state["file_queue"] = queue
    context.emit("file.queue", queue)
    request = BatchUploadRequest(tuple(BatchItem(path) for path in paths), relation, instructions, combine)
    try:
        context.controller.upload_audio_files(request)
    except Exception:
        queue["active"] = False
        for item in queue["items"]:
            item["status"] = "failed"
            item["error"] = "The batch could not start."
        context.emit("file.queue", queue)
        raise
    return changed("file.queue")


def _file_queue(context, _params):
    return context.ui.state.setdefault("file_queue", {"items": [], "active": False})


def _file_remove(context, params):
    queue = _file_queue(context, {})
    if queue["active"]:
        raise ValueError("Cancel the batch before removing files.")
    item_id = identifier(params)
    queue["items"] = [item for item in queue["items"] if item["id"] != item_id]
    context.emit("file.queue", queue)
    return changed("file.queue")


def _file_clear(context, _params):
    if _file_queue(context, {})["active"]:
        raise ValueError("Cancel the batch before clearing it.")
    context.ui.state["file_queue"] = {"items": [], "active": False}
    context.emit("file.queue", context.ui.state["file_queue"])
    return changed("file.queue")


def _file_cancel(context, _params):
    queue = _file_queue(context, {})
    if not queue["active"]:
        raise ValueError("No file batch is active.")
    context.controller.cancel()
    queue["active"] = False
    for item in queue["items"]:
        if item["status"] in ("queued", "running"):
            item["status"] = "canceled"
    context.emit("file.queue", queue)
    return changed("file.queue", "history.list")


def _meeting_start(context, params):
    _idle(context)
    cloud = params.get("cloud_enabled")
    if cloud is not None:
        boolean(cloud, "cloud_enabled")
    mode = params.get("capture_mode", "both")
    if mode not in ("both", "mic"):
        raise ValueError("Choose microphone or microphone and system audio.")
    policy = params.get("system_audio_policy", "disabled" if mode == "mic" else "auto")
    if policy not in ("auto", "disabled", "required"):
        raise ValueError("Unknown system audio policy.")
    intent = string(params.get("intent", params.get("title", "")), "meeting intent", maximum=2000)
    context.controller.meeting_runtime.start_meeting(cloud_enabled=cloud, system_audio_policy=policy, intent=intent)
    return {"ok": True}


def _meeting_control(name):
    def run(context, _params):
        runtime = context.controller.meeting_runtime
        if name in ("pause_meeting", "resume_meeting", "end_meeting") and not runtime.is_active:
            raise ValueError("No meeting is active.")
        accepted = getattr(runtime, name)()
        if accepted is False:
            raise ValueError(context.ui.state.get("meeting_status") or "The meeting is not ready for this action.")
        return changed("meetings.list")
    return run


def _walkthrough_control(name):
    def run(context, _params):
        runtime = context.controller.walkthrough_runtime
        if name in ("toggle", "snap"):
            getattr(runtime, name)(source="ui")
        else:
            getattr(runtime, name)()
        return changed("walkthroughs.list")
    return run


def _walkthrough_pause(context, params):
    context.controller.walkthrough_runtime.set_paused(boolean(params.get("paused"), "paused"))
    return {"ok": True}


def _walkthrough_note(context, params):
    on = boolean(params.get("on"), "on")
    if context.controller.transcription_runtime.recording_walkthrough is None:
        raise ValueError("Start a walkthrough before recording a note.")
    context.controller.walkthrough_runtime.hold_note(on)
    state = context.ui.state.setdefault("walkthrough_live", {})
    state["note_held"] = on
    context.emit("walkthrough.state", state)
    return {"ok": True}


def _walkthrough_remove(context, params):
    context.controller.walkthrough_runtime.remove_shot(integer(params.get("number"), "screenshot", 1))
    return {"ok": True}


def _walkthrough_move(context, params):
    context.controller.walkthrough_runtime.move_shot(integer(params.get("number"), "screenshot", 1),
                                                   integer(params.get("step"), "direction", -1, 1))
    return {"ok": True}


def _walkthrough_mode(context, params):
    from services.settings import WalkthroughSnapMode
    mode = params.get("mode")
    if mode not in WalkthroughSnapMode.ALL:
        raise ValueError("Unknown screenshot mode.")
    context.controller.walkthrough_runtime.choose_snap_mode(mode)
    return changed("settings.schema")


def _prompt(context, params):
    prompt_id = identifier(params)
    if "value" not in params:
        raise ValueError("Choose a prompt response.")
    context.ui.answer_prompt(prompt_id, params["value"])
    return {"ok": True}


def _scratchpad_update(context, params):
    text = string(params.get("text"), "scratchpad text", maximum=5_000_000)
    context.ui.update_scratchpad(text)
    return {"ok": True}


def _scratchpad_get(context, _params):
    return {"text": context.ui.state.get("scratchpad", ""),
            "save": context.ui.state.get("scratchpad_save", {"loaded": True, "dirty": False, "error": ""})}


def _components(context, _params):
    from dataclasses import asdict
    from services.components import component_coordinator
    return {"items": [asdict(item) for item in component_coordinator.list_components()]}


def _component_action(name):
    def run(context, params):
        from services.components import available_component_ids
        component_id = identifier(params)
        if component_id not in available_component_ids():
            raise ValueError("Unknown component.")
        getattr(context.controller, name)(component_id)
        return changed("components.list")
    return run


_COMMANDS = {
    "snapshot": lambda c, p: c.snapshot(),
    "record.start": _record_start, "record.stop": _record_stop, "record.cancel": _cancel,
    "file.transcribe": _file_transcribe, "file.enqueue": _file_enqueue, "file.queue": _file_queue,
    "file.remove": _file_remove, "file.clear": _file_clear, "file.cancel": _file_cancel,
    "settings.schema": settings.schema, "settings.values": settings.values, "settings.update": settings.update,
    "collections.schema": lambda c, p: {"collections": collections.definitions()},
    "settings.coverage": settings.coverage,
    "appearance.update": lambda c, p: settings.update(c, {"key": "ui_theme", "value": p.get("theme")}),
    "credentials.set": settings.credential_write,
    "collections.list": collections.list_items, "collections.save": collections.save, "collections.delete": collections.delete,
    "models.list": models.list_models, "models.download": models.download,
    "models.download_batch": models.download_batch, "models.remove": models.remove,
    "models.select": models.select, "models.cancel": models.cancel,
    "devices.list": models.devices, "devices.select": models.select_device,
    "devices.priority": models.device_priority, "models.discover": models.discover,
    "components.list": _components,
    "components.install": _component_action("request_component_install"),
    "components.remove": _component_action("request_component_uninstall"),
    "components.cancel": _component_action("cancel_component_install"),
    "history.list": history.list_entries, "history.detail": history.detail, "history.update": history.update,
    "history.delete": history.delete, "history.export": history.export, "history.playback": history.playback,
    "meetings.list": meetings.list_meetings, "meetings.detail": meetings.detail, "meetings.update": meetings.update,
    "meetings.current": meetings.current,
    "meetings.delete": meetings.delete, "meetings.export": meetings.export, "meetings.retry": meetings.retry,
    "meetings.patch": meetings.patch, "meetings.review": meetings.review,
    "meetings.report.request": meetings.request_report, "meetings.report.remove": meetings.remove_report,
    "meeting.start": _meeting_start, "meeting.stop": _meeting_control("end_meeting"),
    "meeting.pause": _meeting_control("pause_meeting"), "meeting.resume": _meeting_control("resume_meeting"),
    "meeting.dashboard": _meeting_control("open_dashboard"), "meeting.report": _meeting_control("open_report"),
    "meeting.background": _meeting_control("continue_in_background"), "meeting.defer": _meeting_control("defer_finalization_card"),
    "walkthroughs.list": walkthroughs.list_walkthroughs, "walkthroughs.detail": walkthroughs.detail,
    "walkthroughs.review": walkthroughs.review, "walkthroughs.delete": walkthroughs.delete,
    "walkthroughs.export": walkthroughs.export, "walkthroughs.retention": walkthroughs.retention,
    "walkthrough.toggle": _walkthrough_control("toggle"), "walkthrough.snap": _walkthrough_control("snap"),
    "walkthrough.finish": _walkthrough_control("finish"), "walkthrough.cancel": _walkthrough_control("cancel"),
    "walkthrough.undo": _walkthrough_control("undo_last_shot"), "walkthrough.pause": _walkthrough_pause,
    "walkthrough.note": _walkthrough_note, "walkthrough.remove": _walkthrough_remove,
    "walkthrough.move": _walkthrough_move, "walkthrough.snap_mode": _walkthrough_mode,
    "prompt.resolve": _prompt,
    "scratchpad.get": _scratchpad_get,
    "scratchpad.update": _scratchpad_update,
    "app.quit": lambda c, p: (c.shutdown() or {"ok": True}),
}


_COMMANDS.update(system.COMMANDS)
_COMMANDS.update(helpers.COMMANDS)


def dispatch(context, method: str, params: dict | None = None):
    """Invoke an explicitly registered command; never reflect arbitrary methods."""
    if not isinstance(method, str) or method not in _COMMANDS:
        raise ValueError("Unknown native desktop command.")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Command parameters must be an object.")
    return serializable(_COMMANDS[method](context, params))


def methods():
    return tuple(sorted(_COMMANDS))
