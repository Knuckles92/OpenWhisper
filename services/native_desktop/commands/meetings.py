"""Saved meeting records and their native report/transcript views."""
from __future__ import annotations

from .common import boolean, changed, export_result, identifier, page, repository, string


def _get(context, params):
    item = repository(context).get_meeting(identifier(params))
    if item is None:
        raise ValueError("Meeting was not found.")
    return item


def _summary(item):
    from meeting.stored import compact_finalization_list_fields
    from meeting.time_utils import meeting_duration_s
    content = item.get("content_summary") or {}
    return {"id": item["id"], "title": item.get("title") or "Untitled meeting",
            "started_at": item.get("started_at") or "", "timestamp": item.get("started_at") or "",
            "duration": meeting_duration_s(item) or 0, "status": item.get("status") or "ended",
            "preview": content.get("preview_text", ""),
            "cloud_enabled": bool(item.get("cloud_enabled")),
            "audio_available": bool(content.get("has_audio")),
            **compact_finalization_list_fields(item)}


def list_meetings(context, params):
    offset, limit = page(params)
    needle = string(params.get("query", ""), "search query", maximum=1000)
    repo = repository(context)
    # Reuse the repository's bounded queries, including legacy content and
    # transcript search. Advancing an offset never loads full state snapshots.
    skipped, cursor_started, cursor_id = 0, None, None
    while skipped < offset:
        batch = repo.list_past_meeting_summaries(limit=min(offset - skipped, 200), query=needle,
                                                cursor_started_at=cursor_started, cursor_id=cursor_id)
        if not batch:
            return {"items": [], "offset": offset, "limit": limit, "total": skipped, "has_more": False}
        skipped += len(batch)
        cursor_started, cursor_id = batch[-1]["started_at"], batch[-1]["id"]
    rows = repo.list_past_meeting_summaries(limit=limit + 1, query=needle,
                                           cursor_started_at=cursor_started, cursor_id=cursor_id)
    from services.models import MeetingSession
    from meeting.persist.repository import _search_filter
    with repo._db.get_session() as session:
        query = session.query(MeetingSession).filter(~MeetingSession.status.in_(("active", "paused", "ending")))
        if needle.strip():
            query = _search_filter(session, query, needle.strip())
        total = query.count()
    more = len(rows) > limit
    result = {"items": [_summary(item) for item in rows[:limit]], "offset": offset,
              "limit": limit, "has_more": more, "total": total}
    if more:
        last = rows[limit - 1]
        result["next_cursor"] = {"started_at": last["started_at"], "id": last["id"]}
    return result


def detail(context, params):
    from meeting.stored import load_state
    from meeting.export.transcript_txt import export_transcript_txt
    from meeting.export.markdown import export_markdown
    item = _get(context, params)
    owned = _existing_store(context, item["id"], item)
    state = owned.snapshot() if owned else load_state(item, historical=True).to_dict()
    segments = repository(context).get_segments(item["id"])
    result = _summary(item)
    # Build from the typed public state, never the repository row which also
    # contains host/guest capability tokens and endpoint internals.
    result.update({"transcript": export_transcript_txt(item, state, segments),
                   "report": export_markdown(item, state, segments),
                   "summary": state.get("rolling_summary", ""), "segments": segments,
                   "cards": state.get("cards", {}), "participants": state.get("participants", []),
                   "questions": state.get("questions", []), "finalization": state.get("finalization", {}),
                   "custom_reports": state.get("custom_reports", []),
                   "insight_review": state.get("insight_review", {}), "state": state})
    from config import config
    from pathlib import Path
    from meeting.persist.data_lifecycle import _validated_spool_path
    audio_files = []
    try:
        spool = _validated_spool_path(str(item.get("spool_dir") or ""), config.MEETINGS_FOLDER)
    except ValueError:
        spool = None
    if spool and Path(spool).is_dir():
        root = Path(spool).resolve()
        for chunk in repository(context).get_audio_chunks(item["id"]):
            path = Path(chunk.get("file_path") or "")
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                audio_files.append({"path": str(path.resolve()), "channel": chunk.get("channel"),
                                    "start_s": chunk.get("start_s", 0), "duration": chunk.get("duration_s", 0)})
    result["audio_files"] = audio_files
    result["playback_path"] = audio_files[0]["path"] if audio_files else None
    return result


def current(context, _params):
    engine = getattr(context.controller.meeting_runtime, "_engine", None)
    meeting_id = getattr(engine, "meeting_id", None)
    if not meeting_id:
        return {"id": None, "active": False}
    return detail(context, {"id": meeting_id})


def update(context, params):
    from services.titles import normalize_title
    item = _get(context, params)
    context.controller.meeting_runtime.retitle_saved_meeting(item["id"], normalize_title(params.get("title")))
    owned = _existing_store(context, item["id"])
    if owned is not None:
        owned.refresh_title()
    return changed("meetings.list", id=item["id"])


def delete(context, params):
    item = _get(context, params)
    runtime = context.controller.meeting_runtime
    if runtime.meeting_busy(item["id"]) or _review_work_running(_existing_store(context, item["id"], item)):
        raise ValueError("Finish this meeting before deleting it.")
    delete_audio = boolean(params.get("delete_audio", True), "delete_audio")
    from config import config
    from meeting.persist.data_lifecycle import delete_meeting_data
    delete_meeting_data(repository(context), item["id"], meetings_root=config.MEETINGS_FOLDER,
                        delete_spool=delete_audio)
    getattr(context, "_meeting_review_stores", {}).pop(item["id"], None)
    context.emit("meetings.changed", {"id": item["id"]})
    return changed("meetings.list", id=item["id"])


def export(context, params):
    from meeting.stored import load_state
    from meeting.export.transcript_txt import export_transcript_txt
    from meeting.export.markdown import export_markdown
    from meeting.export.json_export import export_json
    item = _get(context, params)
    owned = _existing_store(context, item["id"], item)
    state = owned.snapshot() if owned else load_state(item, historical=True).to_dict()
    segments = repository(context).get_segments(item["id"])
    fmt = params.get("format", "md")
    if fmt not in ("txt", "md", "markdown", "json"):
        raise ValueError("Choose Markdown, text or JSON.")
    renderer = export_json if fmt == "json" else export_transcript_txt if fmt == "txt" else export_markdown
    text = renderer(item, state, segments)
    return export_result(params, text, f"meeting-{item['id']}.{'md' if fmt == 'markdown' else fmt}",
                         "application/json" if fmt == "json" else "text/plain")


def retry(context, params):
    item = _get(context, params)
    step = params.get("step", "failed")
    if step not in ("failed", "redecode", "speaker_id", "polish", "consolidation"):
        raise ValueError("Unknown finalization step.")
    if context.controller.meeting_runtime.meeting_busy(item["id"]) or _review_work_running(_existing_store(context, item["id"], item)):
        raise ValueError("This meeting is already busy.")
    getattr(context, "_meeting_review_stores", {}).pop(item["id"], None)
    context.controller.meeting_runtime.retry_finalization(step, meeting_id=item["id"])
    return changed("meetings.list", id=item["id"])


def _review_work_running(store):
    if store is None:
        return False
    snapshot = store.snapshot()
    return (snapshot.get("insight_review", {}).get("status") == "running"
            or any(report.get("status") == "running" for report in snapshot.get("custom_reports", [])))


def _existing_store(context, meeting_id, item=None):
    runtime = context.controller.meeting_runtime
    background = getattr(runtime, "_background_engines", {})
    owners = [getattr(runtime, "_engine", None), getattr(runtime, "_archive_dashboard", None)]
    if isinstance(background, dict):
        owners.extend(background.values())
    for owner in owners:
        if owner is not None and getattr(owner, "meeting_id", None) == meeting_id:
            return getattr(owner, "store", None)
    store = getattr(context, "_meeting_review_stores", {}).get(meeting_id)
    if store is not None and item is not None:
        def refresh(state):
            # Keep subscribers and active workers on the same object after an
            # external MCP/sync edit. Compare and replace under its state lock
            # so a worker cannot advance between these two operations.
            from meeting.stored import load_state
            if int(item.get("state_seq") or 0) > state.seq:
                store.replace_document(load_state(item))
        store.with_state(refresh)
        if str(item.get("title") or "") != store.snapshot().get("title", ""):
            store.refresh_title()
    return store


def _store(context, item):
    from meeting.stored import open_store
    store = _existing_store(context, item["id"], item)
    if store is not None:
        return store
    if item.get("status") in ("active", "paused", "ending") or context.controller.meeting_runtime.meeting_busy(item["id"]):
        raise ValueError("Wait for this meeting's current work to finish.")
    store = open_store(repository(context), item["id"], item, historical=True)
    if not hasattr(context, "_meeting_review_stores"):
        context._meeting_review_stores = {}
    context._meeting_review_stores[item["id"]] = store
    store.subscribe(lambda seq, applied: context.emit("meetings.changed", {"id": item["id"], "seq": seq}))
    return store


_HUMAN_OPS = frozenset({"add_item", "update_item", "remove_item", "pin_item", "unpin_item", "confirm_item",
                        "set_topic", "set_rolling_summary", "set_title", "set_meeting_intent",
                        "upsert_participant", "rename_participant", "reassign_segment_speaker",
                        "ask_question", "answer_question", "dismiss_question", "reopen_question"})


def patch(context, params):
    from dataclasses import asdict
    item = _get(context, params)
    ops = params.get("ops")
    if not isinstance(ops, list) or not 1 <= len(ops) <= 50 or any(not isinstance(op, dict) or op.get("op") not in _HUMAN_OPS for op in ops):
        raise ValueError("Supply between 1 and 50 supported meeting edits.")
    results = _store(context, item).apply("host", None, ops)
    failed = [result.reason for result in results if not result.ok]
    return {"ok": not failed, "results": [asdict(result) for result in results],
            "refresh": ["meetings.list"], "id": item["id"], "item": detail(context, params)}


def review(context, params):
    from dataclasses import asdict
    item = _get(context, params)
    store = _store(context, item)
    operation = params.get("op", "start")
    if operation == "start":
        from meeting.insight_review import start_review
        result = start_review(store, repository(context))
    elif operation in ("review_answer", "review_skip", "review_reopen"):
        op = {key: value for key, value in params.items() if key != "id"}
        results = store.apply("host", None, [op])
        result = {"ok": all(value.ok for value in results), "results": [asdict(value) for value in results]}
    else:
        raise ValueError("Unknown insight review operation.")
    return {**result, "id": item["id"], "refresh": ["meetings.list"], "item": detail(context, params)}


def request_report(context, params):
    from meeting.custom_report import start_custom_report
    from meeting.state.custom_reports import MAX_REQUEST_CHARS
    from services.meeting_agent_route import resolve_meeting_agent_route
    from .common import settings
    item = _get(context, params)
    request = string(params.get("request"), "report instructions", required=True, maximum=MAX_REQUEST_CHARS)
    route = resolve_meeting_agent_route(settings(context).load_all_settings(), item)
    result = start_custom_report(_store(context, item), repository(context), request,
                                 provider=route.provider, model=route.model, endpoint=route.endpoint)
    if not result["ok"]:
        raise ValueError("The report could not start: " + str(result.get("error") or "unavailable"))
    return {**result, "id": item["id"], "refresh": ["meetings.list"], "item": detail(context, params)}


def remove_report(context, params):
    item = _get(context, params)
    report_id = string(params.get("report_id"), "report ID", required=True, maximum=200)
    results = _store(context, item).apply("host", None, [{"op": "remove_custom_report", "report_id": report_id}])
    if not results or not results[0].ok:
        raise ValueError("The report could not be removed.")
    return changed("meetings.list", id=item["id"], item=detail(context, params))
