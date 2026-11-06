"""Local-day navigation of the existing history/meeting/paired-host calendar."""
from __future__ import annotations

import calendar as month_names
import time
from dataclasses import asdict
from datetime import date
from uuid import uuid4

from .common import boolean, identifier, integer, page, repository, string


def _source(context):
    from services.history_calendar import CalendarSource
    from .history import _database
    return CalendarSource(db=_database(context), repository=repository(context),
        records=getattr(context, "record_sync", None), tz=getattr(context, "calendar_timezone", None))


def _selection(params):
    from services.history_calendar import KINDS
    today = date.today()
    choose_today = boolean(params.get("today", False), "today")
    year = integer(today.year if choose_today else params.get("year", today.year), "year", 2, 9998)
    month = integer(today.month if choose_today else params.get("month", today.month), "month", 1, 12)
    first_weekday = integer(params.get("first_weekday", 6), "first weekday", 0, 6)
    kinds = params.get("kinds", list(KINDS))
    if (not isinstance(kinds, list) or not kinds or len(kinds) > 3
            or any(kind not in KINDS for kind in kinds) or len(set(kinds)) != len(kinds)):
        raise ValueError("Choose at least one supported recording kind.")
    place = params.get("place")
    if place is not None:
        if (not isinstance(place, list) or len(place) != 2 or place[0] not in ("here", "from", "on")
                or not isinstance(place[1], str) or len(place[1]) > 200
                or (place[0] == "here" and place[1])):
            raise ValueError("Choose a supported record location.")
        place = tuple(place)
    selected = today if choose_today else None
    if not choose_today and params.get("day"):
        try:
            selected = date.fromisoformat(string(params["day"], "selected day", maximum=10))
        except ValueError:
            raise ValueError("Choose a day in YYYY-MM-DD format.") from None
        if (selected.year, selected.month) != (year, month):
            raise ValueError("Choose a day in the displayed month.")
    return year, month, first_weekday, frozenset(kinds), place, selected


def _build(context, source, marks, params, request_id):
    from services.history_calendar import CalendarIndex, month_weeks, month_key, milestone_text, place_label, shift_month
    year, month, first_weekday, kinds, place, selected = _selection(params)
    index = CalendarIndex(marks)
    if place is not None and place not in index.places():
        place = None
    days = index.days(year, month, kinds, place)
    today = date.today()
    if selected is None:
        active = sorted(days)
        if (year, month) == (today.year, today.month):
            earlier = [day for day in active if day <= today]
            selected = today if today in days or not active else max(earlier) if earlier else min(active)
        else:
            selected = max(active) if active else date(year, month, 1)
    month_stats = index.month_stats(year, month, kinds, place)
    stats = asdict(month_stats)
    stats["by_kind"] = dict(month_stats.by_kind)
    if stats["busiest"]:
        stats["busiest"] = {"day": stats["busiest"][0].isoformat(), "count": stats["busiest"][1]}
    milestones = index.milestone_days(year, month)
    weeks = []
    for week in month_weeks(year, month, first_weekday):
        cells = []
        for day in week:
            summary = days.get(day)
            cells.append({"date": day.isoformat(), "day": day.day, "year": day.year, "month": day.month,
                "current_month": day.month == month, "today": day == today, "selected": day == selected,
                "count": summary.count if summary else 0, "by_kind": dict(summary.by_kind) if summary else {},
                "milestone": milestones.get(day)})
        weeks.append(cells)
    rows = []
    for item in source.load_month(year, month):
        if item.day != selected or item.kind not in kinds or (place and item.place != place):
            continue
        ordinal = index.milestone(item.record_id)
        chip, tooltip = item.location()
        rows.append({"id": item.record_id, "kind": item.kind, "title": item.title or item.label(),
            "preview": item.preview[:220], "when": item.when.isoformat(), "time": item.when.strftime("%H:%M"),
            "duration": item.seconds, "place": list(item.place), "location": chip, "location_hint": tooltip,
            "milestone": milestone_text(ordinal) if ordinal else ""})
    offset, limit = page(params)
    months = index.month_counts(kinds, place)
    nearest = index.nearest_month((year, month), kinds, place)
    return {"request_id": request_id, "year": year, "month": month,
        "label": f"{month_names.month_name[month]} {year}", "today": today.isoformat(),
        "selected": selected.isoformat(), "selected_label": selected.strftime("%A, %d %B %Y"),
        "first_weekday": first_weekday, "kinds": sorted(kinds), "place": list(place) if place else None,
        "places": [{"value": list(p), "label": place_label(p)} for p in index.places()],
        "weeks": weeks, "stats": stats, "streak": index.streak(today), "total_recordings": len(index),
        "months": [{"year": y, "month": m, "count": count} for (y, m), count in sorted(months.items())],
        "previous": list(shift_month((year, month), -1)), "next": list(shift_month((year, month), 1)),
        "nearest": list(nearest) if nearest else None, "notice": source.notice,
        "items": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit,
        "has_more": offset + limit < len(rows), "month_key": list(month_key(selected))}


def _publish(context, event, payload):
    callback = lambda: context.emit(event, payload)
    on_owner = getattr(context.ui, "call_on_owner", None)
    if callable(on_owner):
        on_owner(callback)
    else:
        callback()


def get(context, params):
    _selection(params)
    page(params)
    source = _source(context)
    local = source.load_index()
    request_id = uuid4().hex
    context._native_calendar_request = request_id
    wanted = source.remote_wanted()
    cached = getattr(context, "_native_calendar_remote_cache", None) if wanted else None
    if cached:
        source._remote = cached["remote"]
        source.notice = cached["notice"]
    known = {mark.record_id for mark in local}
    marks = local + [mark for mark in (cached or {}).get("marks", []) if mark.record_id not in known]
    result = _build(context, source, marks, params, request_id)
    loading = wanted and (not cached or params.get("refresh") or time.monotonic() - cached["at"] > 30)
    result["remote_loading"] = bool(loading)
    if not loading:
        return result
    future = getattr(context, "_native_calendar_remote_future", None)
    if future is None or future.done():
        def load():
            remote_source = _source(context)
            remote = remote_source.load_remote(known)
            cache = {"remote": remote_source._remote, "marks": remote, "notice": remote_source.notice, "at": time.monotonic()}
            context._native_calendar_remote_cache = cache
            return cache
        future = context.controller.executor.submit(load)
        context._native_calendar_remote_future = future
    def finished(done):
        if context._native_calendar_request != request_id:
            return
        try:
            loaded = done.result()
            source._remote = loaded["remote"]
            source.notice = loaded["notice"]
            all_marks = local + [mark for mark in loaded["marks"] if mark.record_id not in known]
            payload = _build(context, source, all_marks, params, request_id)
            payload["remote_loading"] = False
        except Exception:
            payload = {**result, "remote_loading": False, "notice": "The paired host could not be asked. Your local recordings are available."}
        _publish(context, "history.calendar.loaded", payload)
    future.add_done_callback(finished)
    return result


def open_item(context, params):
    from . import history, meetings
    from services.history_calendar import MEETING
    from services.history_manager import remote_history_entry
    item_id = identifier(params)
    kind = params.get("kind")
    if kind not in ("dictation", "file", MEETING):
        raise ValueError("Choose a supported calendar item.")
    request_id = string(params.get("request_id", uuid4().hex), "open request", required=True, maximum=150)
    if kind == MEETING:
        local = repository(context).get_meeting(item_id)
        item = meetings.detail(context, {"id": item_id}) if local else None
    else:
        local = history._database(context).get_history_entry_by_id(item_id)
        item = history._row(context, local, detail=True) if local else None
    if item is not None:
        return {"request_id": request_id, "id": item_id, "kind": kind, "item": item}
    source = _source(context)
    records = source._sync()
    remote_kind = MEETING if kind == MEETING else "dictation"
    remote = records.remote_item(remote_kind, item_id)
    if remote is None:
        raise ValueError("Refresh the calendar; this recording is no longer available.")
    if kind != MEETING:
        return {"request_id": request_id, "id": item_id, "kind": kind,
                "item": history._row(context, remote_history_entry(remote), detail=True)}
    def load():
        records.check_out(MEETING, item_id)
        return meetings.detail(context, {"id": item_id})
    def finished(done):
        try:
            payload = {"request_id": request_id, "id": item_id, "kind": kind, "item": done.result()}
        except Exception:
            payload = {"request_id": request_id, "error": "The meeting could not be opened from the paired host. Check the connection and try again."}
        _publish(context, "history.calendar.opened", payload)
    if not hasattr(context, "_native_calendar_opens"):
        context._native_calendar_opens = {}
    future = context._native_calendar_opens.get(item_id)
    if future is None or future.done():
        future = context.controller.executor.submit(load)
        context._native_calendar_opens[item_id] = future
    future.add_done_callback(finished)
    return {"request_id": request_id, "id": item_id, "kind": kind, "loading": True}


COMMANDS = {"history.calendar.get": get, "history.calendar.open": open_item}
