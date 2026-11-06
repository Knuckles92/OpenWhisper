"""Walkthrough library access with canonical capture-folder validation."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .common import (
    boolean,
    changed,
    export_result,
    identifier,
    integer,
    paginate,
    settings,
    string,
)


def _root(context):
    from services.walkthrough.storage import captures_root
    return Path(getattr(context, "captures_root", None) or captures_root())


def _folder(context, params):
    from services.walkthrough.library import entries
    record_id = identifier(params)
    folder = next((folder for folder in entries(_root(context)) if folder.name == record_id), None)
    if folder is None:
        raise ValueError("Walkthrough was not found.")
    return folder


def _manifest(folder):
    from services.walkthrough.library import _seconds, _shot_file, local_file, read_json
    from services.walkthrough.storage import MANIFEST_NAME
    path = folder / MANIFEST_NAME
    if not path.exists():
        return _draft(folder)
    value = read_json(local_file(folder, MANIFEST_NAME))
    if value.get("version") != 1 or not isinstance(value.get("sections"), list):
        raise ValueError("Unsupported walkthrough document.")
    _seconds(value.get("duration_s", 0))
    for section in value["sections"]:
        if not isinstance(section, dict) or not isinstance(section.get("text", ""), str):
            raise ValueError("Invalid walkthrough section.")
        _shot_file(folder, section.get("shot"))
        _shot_file(folder, section.get("omitted_shot"), required=False)
    return value


def _draft(folder):
    import wave

    from services.walkthrough.library import _seconds, _shot_file, local_file, read_json
    draft = read_json(local_file(folder, "draft.json"))
    if draft.get("version") != 1:
        raise ValueError("Unsupported interrupted walkthrough.")
    datetime.fromisoformat(draft["started_at"])
    sections = []
    for item in draft.get("shots", []):
        try:
            image = _shot_file(folder, item["shot"])
        except ValueError:
            continue
        sections.append({"text": "", "shot": image.name,
                         "kept_at_s": _seconds(item["kept_at_s"]), "notes": []})
    audio_ready, duration = False, 0
    try:
        with wave.open(str(local_file(folder, "audio.wav")), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            audio_ready = audio.getnframes() > 0
    except (OSError, ValueError, wave.Error, EOFError, ZeroDivisionError):
        pass
    return {"version": 1, "started_at": draft["started_at"], "duration_s": duration,
            "sections": sections, "draft": True, "audio_ready": audio_ready,
            "notes_s": draft.get("notes_s", [])}


def _operation_state(context, record_id):
    return {key: value for key, value in context.ui.state.get("walkthrough_operations", {}).get(record_id, {}).items()
            if key != "item"}


def _busy(context):
    controller = context.controller
    runtime = controller.transcription_runtime
    return (getattr(controller, "_walkthrough_library_busy", False) is True
            or getattr(controller, "_backup_in_progress", False) is True
            or getattr(runtime, "writing_up", False) is True)


@contextmanager
def _lease(context):
    error = context.controller._acquire_walkthrough_library()
    if error:
        raise ValueError(str(error))
    try:
        yield
    finally:
        context.controller._release_walkthrough_library()


def _publish_operation(context, operation):
    context.ui.state["walkthrough_operations"] = {
        **context.ui.state.get("walkthrough_operations", {}), operation["id"]: operation}
    context.emit("walkthroughs.operation", operation)


def _start_operation(context, folder, kind, work, *, selection=None, record_id=None, cancelable=True):
    if getattr(context.controller, "_shutting_down", False) is True:
        raise ValueError("OpenWhisper is closing.")
    record_id = record_id or folder.name
    if _operation_state(context, record_id).get("status") == "running":
        raise ValueError("This walkthrough is already being changed.")
    error = context.controller._acquire_walkthrough_library()
    if error:
        raise ValueError(str(error))
    cancel = threading.Event()
    job_id = uuid4().hex
    entry = {"cancel": cancel, "job_id": job_id, "kind": kind}
    if not hasattr(context.ui, "_walkthrough_jobs"):
        context.ui._walkthrough_jobs = {}
    context.ui._walkthrough_jobs[record_id] = entry
    operation = {"id": record_id, "job_id": job_id, "kind": kind, "status": "running", "cancelable": cancelable}
    context.ui.state["walkthrough_library_busy"] = True
    context.emit("walkthroughs.busy", {"busy": True})
    _publish_operation(context, operation)

    def run():
        try:
            if cancel.is_set():
                raise RuntimeError("canceled")
            return work(cancel)
        finally:
            context.controller._release_walkthrough_library()

    def completed(future):
        if future.cancelled():
            context.controller._release_walkthrough_library()
        result, error = None, ""
        try:
            result = future.result()
        except Exception as exc:
            error = str(exc) or "The walkthrough change failed."

        def publish():
            context.ui._walkthrough_jobs.pop(record_id, None)
            status = "canceled" if error and cancel.is_set() else "failed" if error else "done"
            final = {**operation, "status": status, "error": error}
            context.ui.state["walkthrough_library_busy"] = getattr(context.controller, "_walkthrough_library_busy", False) is True
            context.emit("walkthroughs.busy", {"busy": context.ui.state["walkthrough_library_busy"]})
            context.ui.state["walkthrough_operations"] = {
                **context.ui.state.get("walkthrough_operations", {}), record_id: final}
            if not error and folder.is_dir():
                try:
                    final["item"] = detail(context, {"id": record_id, **(selection or {})})
                except (OSError, ValueError):
                    pass
            if isinstance(result, dict):
                final["result"] = result
            _publish_operation(context, final)
            context.emit("walkthroughs.changed", {"id": record_id})

        context.ui.call_on_owner(publish)

    try:
        future = context.controller.walkthrough_executor.submit(run)
    except Exception:
        context.ui._walkthrough_jobs.pop(record_id, None)
        context.controller._release_walkthrough_library()
        context.ui.state["walkthrough_library_busy"] = False
        context.emit("walkthroughs.busy", {"busy": False})
        _publish_operation(context, {**operation, "status": "failed", "error": "The work could not be started."})
        raise
    future.add_done_callback(completed)
    return changed("walkthroughs.list", id=record_id,
                   operation=context.ui.state["walkthrough_operations"].get(record_id, operation))


def _selection(folder, params):
    from services.walkthrough.library import earlier_write_ups, written_up
    spoken = boolean(params.get("spoken", False), "spoken")
    version = params.get("version")
    if version is not None:
        integer(version, "write-up version", 1, 1_000_000)
        if spoken or version not in earlier_write_ups(folder):
            raise ValueError("Choose an existing write-up version.")
    return spoken or not written_up(folder), version


def _stem(spoken, version):
    return "walkthrough" if spoken else "write-up" if version is None else f"write-up-{version}"


def _error_row(folder, error):
    return {"id": folder.name, "title": "Walkthrough " + folder.name.replace("_", " "),
            "started_at": "", "timestamp": "", "duration": 0, "step_count": 0,
            "preview": "This document needs recovery.", "status": "needs_recovery",
            "written_up": False, "error": str(error)[:500]}


def _summary(folder, manifest):
    from services.walkthrough.library import written_up
    sections = manifest.get("sections", [])
    return {"id": folder.name, "title": manifest.get("title") or "Walkthrough " + folder.name.replace("_", " "),
            "started_at": manifest.get("started_at", ""), "timestamp": manifest.get("started_at", ""),
            "duration": manifest.get("duration_s", 0),
            "step_count": sum(bool(s.get("shot")) for s in sections),
            "preview": " ".join(s.get("text", "") for s in sections)[:220],
            "status": "draft" if manifest.get("draft") else "ready",
            "written_up": written_up(folder)}


def list_walkthroughs(context, params):
    from services.walkthrough.library import entries
    query = string(params.get("query", ""), "search query", maximum=1000).casefold()
    items, warnings = [], []
    for folder in entries(_root(context)):
        try:
            manifest = _manifest(folder)
            row = _summary(folder, manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({"id": folder.name, "message": "This document needs recovery."})
            row = _error_row(folder, exc)
            manifest = {}
        searchable = " ".join(section.get("text", "") for section in manifest.get("sections", []))
        if not query or query in (row["title"] + " " + searchable).casefold():
            items.append(row)
    return {**paginate(items, params), "warnings": warnings}


def detail(context, params):
    from services.walkthrough import library
    from services.walkthrough.library import local_file
    folder = _folder(context, params)
    error = ""
    try:
        manifest = _manifest(folder)
        result = _summary(folder, manifest)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error = str(exc)[:500]
        result = _error_row(folder, exc)
        try:
            manifest = _draft(folder)
        except (OSError, ValueError, KeyError, TypeError):
            manifest = {"sections": [], "draft": False, "audio_ready": False}
    spoken, version = _selection(folder, params)
    steps = []
    for index, section in enumerate(manifest["sections"]):
        image = section.get("shot") or section.get("omitted_shot")
        steps.append({"index": index, "title": f"Step {index + 1}", "text": section.get("text", ""),
                      "image_path": str(local_file(folder, image)) if image else None,
                      "included": bool(section.get("shot")), "kept_at_s": section.get("kept_at_s"),
                      "notes": section.get("notes", [])})
    stem = _stem(spoken, version)
    markdown = local_file(folder, stem + ".md").read_text(encoding="utf-8") if (folder / (stem + ".md")).is_file() else ""
    if not spoken:
        steps = []
        for index, (text, line) in enumerate(library.write_up_parts(markdown)):
            image = library.write_up_shot(folder, line)
            steps.append({"index": index, "title": f"Part {index + 1}", "text": text,
                          "image_path": str(image) if image else None, "included": image is not None,
                          "image_line": line, "notes": []})
    versions = [{"label": "As spoken", "spoken": True, "version": None}]
    if library.written_up(folder):
        versions += [{"label": f"Write-up {number}", "spoken": False, "version": number}
                     for number in library.earlier_write_ups(folder)]
        versions.append({"label": "Latest write-up", "spoken": False, "version": None})
    draft = bool(manifest.get("draft"))
    blocked = _busy(context) or _operation_state(context, folder.name).get("status") == "running"
    def artifact(extension):
        return str(local_file(folder, stem + extension)) if (folder / (stem + extension)).is_file() else None
    result.update({"markdown": markdown, "steps": steps,
                   "audio_file": str(local_file(folder, "audio.wav")) if (folder / "audio.wav").is_file() else None,
                   "exports": [fmt for fmt in ("md", "pdf", "docx") if (folder / (stem + "." + fmt)).is_file()],
                   "spoken": spoken, "version": version, "versions": versions,
                   "is_draft": draft, "error": error, "folder_path": str(folder),
                   "pdf_path": artifact(".pdf"), "docx_path": artifact(".docx"),
                   "operation": _operation_state(context, folder.name),
                   "styles": styles(context, {})["items"], "notes_s": manifest.get("notes_s", []),
                   "can_recover": draft and manifest.get("audio_ready", False) and not blocked,
                   "can_retry": not draft and not error and not blocked,
                   "can_edit": not draft and not error and not blocked})
    return result


def review(context, params):
    from services.walkthrough.library import save_review
    folder = _folder(context, params)
    if "spoken" in params and not boolean(params["spoken"], "spoken"):
        raise ValueError("Choose As spoken to edit the recorded steps.")
    original = _manifest(folder)
    texts, keep, order = params.get("texts"), params.get("keep"), params.get("order")
    count = len(original["sections"])
    if not isinstance(texts, list) or len(texts) != count or not all(isinstance(v, str) and len(v) <= 1_000_000 for v in texts):
        raise ValueError("Supply the text of each saved step.")
    if not isinstance(keep, list) or len(keep) != count or not all(type(v) is bool for v in keep):
        raise ValueError("Choose which screenshots to include.")
    if order is not None and (not isinstance(order, list) or any(type(v) is not int for v in order) or sorted(order) != list(range(count))):
        raise ValueError("Step order must include each step once.")
    redo = boolean(params.get("redo", False), "redo")
    def save(cancel):
        result = save_review(folder, texts, keep, order, cancel_event=cancel)
        if redo:
            from services.walkthrough.library import retry_write_up
            result = retry_write_up(folder, cancel_event=cancel)
        return result
    return _start_operation(context, folder, "review", save, selection={"spoken": not redo})


def delete(context, params):
    from services.walkthrough.library import delete_entry
    folder = _folder(context, params)
    session = getattr(context.controller.transcription_runtime, "recording_walkthrough", None)
    if session is not None:
        active = getattr(session, "_folder", None) or getattr(session, "folder", None)
        active = active() if callable(active) else active
        if active is not None and Path(active).resolve() == folder.resolve():
            raise ValueError("Finish this walkthrough before deleting it.")
    return _start_operation(context, folder, "delete", lambda cancel: delete_entry(folder), cancelable=False)


def export(context, params):
    with _lease(context):
        return _export(context, params)


def _export(context, params):
    from services.walkthrough.library import local_file
    folder = _folder(context, params)
    spoken, version = _selection(folder, params)
    stem = _stem(spoken, version)
    fmt = params.get("format", "md")
    if fmt not in ("md", "markdown", "pdf", "docx", "json", "txt", "zip"):
        raise ValueError("Choose Markdown, text, JSON, PDF, Word or ZIP.")
    if fmt == "zip":
        import tempfile
        import zipfile

        from services.walkthrough.library import _EARLIER, _SHOT
        # Stage outside the capture: a partially written archive can never
        # masquerade as one of the user's saved documents.
        with tempfile.NamedTemporaryFile(prefix="openwhisper-walkthrough-", suffix=".zip", delete=False) as temporary:
            archive = Path(temporary.name)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for path in sorted(folder.iterdir()):
                    selected = {"walkthrough.json", stem + ".md", stem + ".pdf", stem + ".docx"}
                    all_versions = params.get("all_versions") is True
                    if (path.name in selected or _SHOT.fullmatch(path.name)
                            or all_versions and (_EARLIER.fullmatch(path.name) or path.name in {
                                "walkthrough.md", "walkthrough.pdf", "walkthrough.docx", "write-up.md", "write-up.pdf", "write-up.docx"})):
                        output.write(local_file(folder, path.name), path.name)
            result = export_result(params, "", f"{folder.name}.zip", "application/zip", source=archive)
            if params.get("path"):
                archive.unlink(missing_ok=True)
                result["source_path"] = result["path"]
            else:
                if not hasattr(context.ui, "_walkthrough_exports"):
                    context.ui._walkthrough_exports = []
                context.ui._walkthrough_exports.append(archive)
                while len(context.ui._walkthrough_exports) > 8:
                    context.ui._walkthrough_exports.pop(0).unlink(missing_ok=True)
            return result
        except Exception:
            archive.unlink(missing_ok=True)
            raise
    if fmt == "txt":
        text = "\n\n".join(step["text"] for step in detail(context, params)["steps"])
        return export_result(params, text, f"{folder.name}.txt")
    if fmt == "json":
        text = json.dumps(_manifest(folder) if spoken else {"id": folder.name, "spoken": False,
            "version": version, "markdown": detail(context, params)["markdown"]}, ensure_ascii=False, indent=2)
        return export_result(params, text, f"{folder.name}.json", "application/json")
    extension = "md" if fmt == "markdown" else fmt
    name = stem + "." + extension
    source = local_file(folder, name)
    if extension == "md":
        return _export_markdown(folder, source, params)
    mime = "application/pdf" if extension == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return export_result(params, "", f"{folder.name}.{extension}", mime, source=source)


def _export_markdown(folder, source, params):
    import shutil

    from services.walkthrough.library import _SHOT, local_file
    from services.walkthrough.write_up import _IMAGE
    destination = Path(params["path"]) if params.get("path") else None
    asset_directory = (destination.stem if destination else folder.name) + "-assets"
    assets = {}
    def image(match):
        name = Path(match[2].strip("<>")).name
        if not _SHOT.fullmatch(name):
            return ""
        path = local_file(folder, name)
        assets[name] = path
        return f"![{match[1]}](<{asset_directory}/{name}>)"
    text = _IMAGE.sub(image, source.read_text(encoding="utf-8"))
    asset_folder = None
    if destination and assets:
        if not destination.is_absolute() or not destination.parent.is_dir():
            raise ValueError("Choose an absolute export path in an existing folder.")
        if destination.exists():
            raise FileExistsError("That export file already exists.")
        asset_folder = destination.parent / asset_directory
        asset_folder.mkdir()
        try:
            for name, path in assets.items():
                shutil.copyfile(path, asset_folder / name)
        except Exception:
            shutil.rmtree(asset_folder)
            raise
    try:
        result = export_result(params, text, f"{folder.name}.md", "text/markdown")
    except Exception:
        if asset_folder:
            shutil.rmtree(asset_folder)
        raise
    if assets:
        result["asset_directory"] = asset_directory
        result["assets"] = [{"filename": name, "source_path": str(path)} for name, path in assets.items()]
    return result


def retention(context, params):
    import time

    from services.walkthrough.library import (
        delete_entry,
        entries,
        retention_days,
        set_retention,
    )
    root = _root(context)
    current = retention_days(root)
    days = integer(params.get("days", current), "retention days", high=3650)
    expired = [folder for folder in entries(root) if days and not (folder / "draft.json").exists()
               and (folder / "walkthrough.json").is_file()
               and (folder / "walkthrough.json").stat().st_mtime < time.time() - days * 86400]
    changing = "days" in params or params.get("prune") is True
    result = {"days": current, "requested_days": days, "expired_count": len(expired),
              "expired_ids": [folder.name for folder in expired]}
    if not changing:
        return result
    if expired and params.get("confirmed") is not True:
        return {**result, "requires_confirmation": True}
    if not expired:
        with _lease(context):
            set_retention(root, days)
        return {**result, "days": days, "ok": True}
    def prune(cancel):
        if cancel.is_set():
            raise RuntimeError("canceled")
        set_retention(root, days)
        removed = []
        for folder in expired:
            delete_entry(folder)
            removed.append(folder.name)
        return {"days": days, "removed": removed}
    return {**result, **_start_operation(context, root, "retention", prune,
        record_id="retention", cancelable=False)}


def styles(context, _params):
    from services.walkthrough.styles import load_styles
    return {"items": [style.to_dict() for style in load_styles(settings(context).load_all_settings())]}


def save_write_up(context, params):
    from services.walkthrough.library import save_write_up as save
    folder = _folder(context, params)
    spoken, version = _selection(folder, params)
    if spoken:
        raise ValueError("Choose an existing write-up to edit.")
    markdown = string(params.get("markdown"), "write-up Markdown", required=True, maximum=2_000_000)
    return _start_operation(context, folder, "write_up.save",
        lambda cancel: save(folder, markdown, version=version, cancel_event=cancel),
        selection={"spoken": False, "version": version})


def retry_write_up(context, params):
    from services.walkthrough.library import retry_write_up as retry
    folder = _folder(context, params)
    if _manifest(folder).get("draft"):
        raise ValueError("Recover the interrupted walkthrough before writing it up.")
    instructions = params.get("instructions")
    if instructions is not None:
        string(instructions, "write-up instructions", maximum=50_000)
    if "style_id" in params:
        if instructions is not None:
            raise ValueError("Choose a style or custom instructions.")
        selected = next((style for style in styles(context, {})["items"]
                         if style["id"] == params["style_id"]), None)
        if selected is None:
            raise ValueError("Choose an existing write-up style.")
        instructions = selected["instructions"]
    return _start_operation(context, folder, "write_up.retry",
        lambda cancel: retry(folder, instructions=instructions, cancel_event=cancel),
        selection={"spoken": False})


def cancel_write_up(context, params):
    folder = _folder(context, params)
    state = _operation_state(context, folder.name)
    entry = getattr(context.ui, "_walkthrough_jobs", {}).get(folder.name)
    if entry is None or state.get("status") != "running":
        return {"ok": True, "running": False}
    if not state.get("cancelable", True):
        raise ValueError("This change is finishing safely; wait for it to complete.")
    if entry["kind"] == "recover":
        runtime = context.controller.transcription_runtime
        session = getattr(runtime, "_walkthrough", None)
        if session is not None and Path(session.folder()).resolve() == folder.resolve():
            entry["cancel"].set()
            runtime.cancel()
        else:
            return {"ok": True, "running": True, "finishing": True}
    else:
        entry["cancel"].set()
    return {"ok": True, "running": True}


def recover(context, params):
    from services.walkthrough.library import recovery_session
    folder = _folder(context, params)
    draft = _draft(folder)
    if not draft["audio_ready"]:
        raise ValueError("This draft's audio has not been recovered yet.")
    controller = context.controller
    runtime = controller.transcription_runtime
    if controller.recorder.is_recording or runtime.has_active_job or controller.is_meeting_active():
        raise ValueError("Finish the recording or meeting before recovering a walkthrough.")
    readiness = controller.transcription_readiness_message()
    if readiness:
        raise ValueError(readiness)
    with _lease(context):
        checked = recovery_session(folder)
        checked.end()
    cancel = threading.Event()
    operation = {"id": folder.name, "job_id": uuid4().hex, "kind": "recover",
                 "status": "running", "cancelable": True}
    if not hasattr(context.ui, "_walkthrough_jobs"):
        context.ui._walkthrough_jobs = {}
    context.ui._walkthrough_jobs[folder.name] = {"cancel": cancel, "kind": "recover"}
    _publish_operation(context, operation)
    decoded = [False]

    def finish(error=""):
        if _operation_state(context, folder.name).get("status") != "running":
            return
        controller.transcription_completed.disconnect(complete)
        controller.transcription_failed.disconnect(failed)
        controller.history_persisted.disconnect(persisted)
        context.ui._walkthrough_jobs.pop(folder.name, None)
        final = {**operation, "status": "canceled" if cancel.is_set() else "failed" if error else "done", "error": error}
        context.ui.state["walkthrough_operations"] = {
            **context.ui.state.get("walkthrough_operations", {}), folder.name: final}
        try:
            final["item"] = detail(context, {"id": folder.name, "spoken": True})
        except (OSError, ValueError, KeyError, TypeError):
            pass
        _publish_operation(context, final)
        context.emit("walkthroughs.changed", {"id": folder.name})

    def complete(*_args):
        try:
            ready = not _manifest(folder).get("draft")
        except (OSError, ValueError, KeyError, TypeError):
            ready = False
        if ready:
            decoded[0] = True
            if not runtime.has_active_job:
                finish()
        else:
            finish("No document was recovered; the draft was kept for another attempt.")

    def failed(message):
        finish(str(message))

    def persisted(_result):
        if decoded[0]:
            finish()

    controller.transcription_completed.connect(complete)
    controller.transcription_failed.connect(failed)
    controller.history_persisted.connect(persisted)
    try:
        runtime.recover_walkthrough(folder)
        if not runtime.has_active_job:
            finish(context.ui.state.get("status", "Recovery could not start."))
    except Exception:
        finish("Recovery could not start.")
        raise
    return changed("walkthroughs.list", id=folder.name, operation=operation)


def copy_document(context, params):
    from services.settings import WalkthroughCopyFormat
    from services.walkthrough.library import load_result
    folder = _folder(context, params)
    spoken, version = _selection(folder, params)
    formats = {"pdf": WalkthroughCopyFormat.PDF, "md": WalkthroughCopyFormat.AGENT,
               "docx": WalkthroughCopyFormat.WORD}
    if params.get("format", "pdf") not in formats:
        raise ValueError("Choose PDF, Markdown, or Word.")
    with _lease(context):
        result = load_result(folder, spoken=spoken, version=version)
        files, text = result.clipboard(formats[params.get("format", "pdf")])
        if not context.ui.copy_files_to_clipboard(files, text=text):
            raise ValueError("Couldn't copy the walkthrough.")
    return {"ok": True}


def copy_screenshot(context, params):
    from PIL import Image

    from services.native_desktop.snap import CapturedImage
    from services.walkthrough.library import _SHOT, local_file
    folder = _folder(context, params)
    filename = string(params.get("filename"), "screenshot filename", required=True, maximum=100)
    if not _SHOT.fullmatch(filename):
        raise ValueError("Choose a screenshot from this walkthrough.")
    with _lease(context):
        path = local_file(folder, filename)
        with Image.open(path) as image:
            image.verify()
        if not context.ui.copy_files_to_clipboard([path], text=str(path), image=CapturedImage(path)):
            raise ValueError("Couldn't copy the screenshot.")
    return {"ok": True}
