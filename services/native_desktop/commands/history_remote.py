"""Nonblocking paired-host full-text history search with bounded pagination."""
from __future__ import annotations

from uuid import uuid4

from .common import page, string


def get(context, params):
    from .calendar import _publish
    from .history import _database, _records, _row
    from services.history_calendar import classify_source
    from services.history_manager import remote_history_entry
    offset, limit = page(params)
    query = string(params.get("query", ""), "search query", maximum=1000).strip()
    kind = params.get("kind", "all")
    if kind not in ("all", "dictation", "file", "command", "transform"):
        raise ValueError("Choose a supported history kind.")
    request_id = uuid4().hex
    context._native_remote_history_request = request_id
    records = _records(context)
    initial = {"request_id": request_id, "query": query, "kind": kind, "offset": offset,
        "limit": limit, "items": [], "total": 0, "has_more": False, "search_limit": 500,
        "loading": False, "active": False, "notice": ""}
    try:
        wanted = records.listing_wanted("dictation")
    except Exception:
        return {**initial, "active": True, "notice": "The paired host could not be asked. Your local history is available."}
    if not wanted:
        return initial
    initial.update(active=True, loading=True)
    def work():
        from services.models import TranscriptionHistory
        remote = records.list_remote("dictation", query, 500)
        ids = [item["id"] for item in remote]
        with _database(context).get_session() as session:
            local_ids = {row[0] for row in session.query(TranscriptionHistory.id).filter(TranscriptionHistory.id.in_(ids)).all()} if ids else set()
        items = []
        for item in remote:
            if item["id"] in local_ids:
                continue
            entry_kind = item.get("entry_kind")
            group, _label = classify_source(item.get("source_name"), entry_kind)
            if kind not in ("all", group, entry_kind):
                continue
            items.append(_row(context, remote_history_entry(item)))
        host = str(getattr(records.status(), "host_name", "") or "your paired host")
        return {**initial, "loading": False, "host": host, "limited": len(remote) >= 500,
            "items": items[offset:offset + limit], "total": len(items), "has_more": offset + limit < len(items)}
    def finished(done):
        if context._native_remote_history_request != request_id:
            return
        try:
            payload = done.result()
        except Exception as exc:
            from services.remote_records.sync import RecordsUnavailable
            message = str(exc) if isinstance(exc, RecordsUnavailable) else "The paired host could not be searched. Check its connection and try Refresh."
            payload = {**initial, "loading": False, "notice": message}
        _publish(context, "history.remote.loaded", payload)
    context.controller.executor.submit(work).add_done_callback(finished)
    return initial


COMMANDS = {"history.remote.get": get}
