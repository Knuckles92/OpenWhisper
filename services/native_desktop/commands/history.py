"""Native history browsing, editing, retention and document export."""
from __future__ import annotations

from pathlib import Path

from .common import changed, export_result, history, identifier, page, string


def _database(context):
    value = getattr(context, "database", None)
    if value is None:
        from services.database import db
        value = db
    return value


def _entry(context, params):
    entry = _database(context).get_history_entry_by_id(identifier(params))
    if entry is None:
        from services.history_manager import remote_history_entry
        remote = _records(context).remote_item("dictation", identifier(params))
        if remote is None:
            raise ValueError("History entry was not found.")
        entry = remote_history_entry(remote)
    return entry


def _records(context):
    records = getattr(context, "record_sync", None)
    if records is None:
        from services.remote_records.sync import record_sync
        records = record_sync
    return records


def _row(context, entry, *, detail=False):
    from services.history_export import serialize_history_entry
    from services.history_manager import entry_version, kind_of, is_local_entry
    row = serialize_history_entry(entry)
    text = row.get("text") or ""
    remote = bool(getattr(entry, "stored_on", None))
    playback = getattr(context, "_native_history_audio", {}).get(entry.id) if remote else _playback_path(context, row.get("audio_file"))
    if playback and not Path(playback).is_file():
        playback = None
    row.update({"title": row.get("title") or row.get("source_name") or "Untitled transcript",
                "preview": text[:220], "duration": row.get("audio_duration") or 0,
                "kind": kind_of(entry), "words": len(text.split()),
                "audio_available": bool(getattr(entry, "remote_audio", False)) if remote else bool(playback),
                "remote": remote, "stored_on": str(getattr(entry, "stored_on", "") or ""),
                "can_delete": remote or is_local_entry(entry),
                "version": entry_version(entry), "editable": is_local_entry(entry)})
    if detail:
        row["raw"] = row.get("raw_text") or text
        row["playback_path"] = playback
    else:
        # The list payload is bounded even for hours-long recordings.
        row["text"] = text[:220]
        row.pop("raw_text", None)
        row.pop("cleaned_text", None)
    return row


def _playback_path(context, filename):
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        return None
    path = history(context).get_recording_path(filename)
    if not path:
        return None
    path = Path(path)
    root = Path(history(context).recordings_folder).resolve()
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        return None
    return str(path.resolve())


def list_entries(context, params):
    from sqlalchemy import or_
    from services.models import TranscriptionHistory as Model
    offset, limit = page(params)
    needle = string(params.get("query", ""), "search query", maximum=1000).strip()
    kind = params.get("kind")
    if kind and kind not in ("dictation", "file", "command", "transform", "all"):
        raise ValueError("Unknown history kind.")
    with _database(context).get_session() as session:
        query = session.query(Model)
        if needle:
            pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.filter(or_(*(column.ilike(pattern, escape="\\") for column in
                                       (Model.text, Model.raw_text, Model.title, Model.source_name, Model.app_name))))
        if kind and kind != "all":
            # Legacy rows infer dictation from Quick Record, as HistoryManager does.
            legacy = Model.entry_kind.is_(None)
            if kind == "dictation":
                query = query.filter(or_(Model.entry_kind.in_(("dictation", "command", "transform", "action", "walkthrough")), legacy & Model.source_name.startswith("Quick Record")))
            elif kind == "file":
                query = query.filter(or_(Model.entry_kind == kind, legacy & or_(Model.source_name.is_(None), ~Model.source_name.startswith("Quick Record"))))
            else:
                query = query.filter(Model.entry_kind == kind)
        total = query.count()
        entries = query.order_by(Model.timestamp.desc(), Model.id.desc()).offset(offset).limit(limit).all()
        items = [_row(context, item) for item in entries]
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(items) < total, "query": needle, "kind": kind or "all"}


def detail(context, params):
    return _row(context, _entry(context, params), detail=True)


def update(context, params):
    from services.history_manager import is_local_entry
    from services.titles import normalize_title
    entry = _entry(context, params)
    if not is_local_entry(entry):
        raise ValueError("Edit this entry on the computer that recorded it.")
    if "version" in params:
        entry = history(context).use_version(entry.id, params["version"])
        if entry is None:
            raise ValueError("This entry has no alternate text version.")
    changes = {}
    if "text" in params:
        changes["text"] = string(params["text"], "transcript", maximum=5_000_000)
    if "title" in params:
        changes["title"] = normalize_title(params["title"])
    if changes:
        from services.models import TranscriptionHistory
        # DatabaseManager's text-version method intentionally excludes title;
        # this first-party editor validates both and commits them together.
        with _database(context).get_session() as session:
            entry = session.get(TranscriptionHistory, entry.id)
            if entry is None:
                raise ValueError("History entry was not found.")
            for key, value in changes.items():
                setattr(entry, key, value)
        from services.remote_records.sync import record_sync
        record_sync.record_edited("dictation", entry.id)
    elif "version" not in params:
        raise ValueError("Supply text, title or a version to change.")
    _notify(context, entry.id)
    return changed("history.list", id=entry.id, item=_row(context, entry, detail=True))


def _notify(context, record_id):
    if hasattr(context.ui, "_history_generation"):
        context.ui._history_generation += 1
    context.emit("history.changed", {"id": record_id})


def delete(context, params):
    from services.history_manager import is_local_entry
    from .common import boolean
    entry = _entry(context, params)
    if getattr(entry, "stored_on", None):
        if not _records(context).delete_remote("dictation", entry.id):
            raise ValueError("The host could not delete this entry.")
        _notify(context, entry.id)
        return changed("history.list", id=entry.id)
    if not is_local_entry(entry):
        raise ValueError("Delete this entry on the computer that recorded it.")
    delete_audio = boolean(params.get("delete_audio", False), "delete_audio")
    if not history(context).delete_entry(entry.id, delete_audio_file=delete_audio):
        raise ValueError("History entry was not found.")
    _notify(context, entry.id)
    return changed("history.list", id=entry.id)


def export(context, params):
    from services.history_export import serialize_history_entry, render_export_document
    fmt = params.get("format", "txt")
    if fmt not in ("txt", "json", "markdown", "md", "srt"):
        raise ValueError("Choose txt, Markdown, JSON or SRT.")
    if params.get("id"):
        entries = [_entry(context, params)]
    elif "ids" in params:
        ids = params["ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise ValueError("Choose between 1 and 1000 history entries.")
        entries = [_entry(context, {"id": item}) for item in ids]
    else:
        # An explicit export-all may be large; unlike browsing it is an
        # intentional one-time operation.
        entries = history(context).get_history()
    rows = [serialize_history_entry(entry) for entry in entries]
    if fmt == "srt":
        def stamp(seconds):
            ms = round(float(seconds) * 1000)
            return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
        elapsed, lines = 0, []
        for index, row in enumerate(rows, 1):
            end = elapsed + float(row.get("audio_duration") or 1)
            lines.append(f"{index}\n{stamp(elapsed)} --> {stamp(end)}\n{row['text']}\n")
            elapsed = end
        text = "\n".join(lines)
    else:
        text = render_export_document(rows, "markdown" if fmt == "md" else fmt)
    extension = "md" if fmt == "markdown" else fmt
    mime = "application/json" if fmt == "json" else "text/plain"
    return export_result(params, text, f"openwhisper-history.{extension}", mime)


def playback(context, params):
    entry = _entry(context, params)
    path = (getattr(context, "_native_history_audio", {}).get(entry.id) if getattr(entry, "stored_on", None)
            else _playback_path(context, entry.audio_file))
    if not path or not Path(path).is_file():
        raise ValueError("This entry has no saved audio.")
    # Only a database-selected recording can be played, never an arbitrary
    # client-provided path.
    return {"id": entry.id, "path": str(Path(path).resolve())}


def audio_get(context, params):
    entry = _entry(context, params)
    if not getattr(entry, "stored_on", None):
        return playback(context, params)
    if not getattr(entry, "remote_audio", False):
        raise ValueError("This entry has no saved audio on the host.")
    from .calendar import _publish
    def finished(done):
        try:
            path = str(Path(done.result()).resolve())
            if not Path(path).is_file():
                raise ValueError("The host did not provide a recording.")
            context._native_history_audio = {**getattr(context, "_native_history_audio", {}), entry.id: path}
            payload = {"id": entry.id, "path": path}
        except Exception:
            payload = {"id": entry.id, "error": "The recording could not be fetched from the host. Check the connection and try again."}
        _publish(context, "history.audio.ready", payload)
    context.controller.executor.submit(_records(context).audio_for, entry.id).add_done_callback(finished)
    return {"id": entry.id, "loading": True}


def retranscribe(context, params):
    from . import _idle
    _idle(context)
    unavailable = context.controller.transcription_readiness_message()
    if unavailable:
        raise ValueError(unavailable)
    path = playback(context, params)["path"]
    context.controller.retranscribe_audio(path)
    return changed("history.list", id=identifier(params))
