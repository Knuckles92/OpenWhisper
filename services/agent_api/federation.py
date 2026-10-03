from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlencode

from pydantic import ValidationError

from services.agent_api.models import (
    ClientStatus,
    Meeting,
    MeetingInsights,
    Page,
    SearchHit,
    Segment,
    Transcription,
    TranscriptionSummary,
)
from services.agent_api.store import InvalidQuery, NotFound
from services.remote_history.channel import ClientUnavailable

_MODELS = {
    "transcriptions": TranscriptionSummary,
    "meetings": Meeting,
    "search": SearchHit,
    "transcription": Transcription,
    "meeting": Meeting,
    "segments": Segment,
    "segment": Segment,
    "insights": MeetingInsights,
}
_CURSOR_TTL = 600
_MAX_CURSORS = 128


def _order(item):
    timestamp = getattr(item, "timestamp", getattr(item, "started_at", ""))
    try:
        date = datetime.fromisoformat(timestamp)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        stamp = date.timestamp()
    except (ValueError, OverflowError, OSError):
        stamp = -float("inf")
    return (
        -stamp,
        getattr(item, "kind", ""),
        item.id,
        getattr(item, "origin_device_id", None) or "",
    )


def _identity(item):
    return (getattr(item, "origin_device_id", None), getattr(item, "kind", ""), item.id)


class FederatedHistory:
    """Merge bounded source pages; cursors retain their original device set for ten minutes."""

    def __init__(self, store, clients=None):
        self.store = store
        self.remote = clients
        self._lock = threading.Lock()
        self._cursors = OrderedDict()

    def close(self):
        with self._lock:
            self._cursors.clear()
        self.store.close()

    def check(self):
        self.store.check()

    def clients(self):
        return (
            [ClientStatus.model_validate(item) for item in self.remote.clients()]
            if self.remote
            else []
        )

    def _remote(self, device, operation, params):
        if self.remote is None:
            raise ClientUnavailable()
        data = self.remote.query(device, operation, params)
        try:
            model = _MODELS[operation]
            result = (
                Page[model].model_validate(data)
                if operation in {"transcriptions", "meetings", "search", "segments"}
                else model.model_validate(data)
            )
            items = result.items if isinstance(result, Page) else [result]
            name = next((c.name for c in self.clients() if c.device_id == device), "")
            for item in items:
                item.device_id = device
                if hasattr(item, "origin_device_id"):
                    item.origin_device_id = device
                if hasattr(item, "origin_device_name"):
                    item.origin_device_name = name
                if isinstance(item, SearchHit):
                    from urllib.parse import quote

                    if item.kind == "transcription":
                        item.resource = f"/v1/transcriptions/{quote(item.id, safe='')}"
                    else:
                        item.resource = (
                            f"/v1/meetings/{quote(item.meeting_id or '', safe='')}"
                        )
                        if item.kind == "segment":
                            item.resource += f"/segments/{quote(item.id, safe='')}"
                    item.resource += "?" + urlencode(
                        {"device_id": device, "include_remote": "true"}
                    )
            if isinstance(result, Page) and len(result.items) > params.get("limit", 20):
                raise ValueError()
            return result
        except (ValidationError, ValueError, TypeError) as exc:
            raise ClientUnavailable() from exc

    def _read(self, operation, params, include_remote=False, device_id=None):
        if device_id is None:
            return getattr(self.store, operation)(
                **params, include_remote=include_remote
            )
        try:
            return self._remote(device_id, operation, params)
        except ClientUnavailable as exc:
            unavailable = exc
        if include_remote:
            try:
                if operation in {"transcription", "meeting"}:
                    result = getattr(self.store, operation)(
                        **params, include_remote=True
                    )
                    if result.origin_device_id == device_id:
                        return result
                else:
                    parent = self.store.meeting(
                        params["meeting_id"], include_remote=True
                    )
                    if parent.origin_device_id == device_id:
                        return getattr(self.store, operation)(
                            **params, include_remote=True
                        )
            except NotFound:
                pass
        raise unavailable

    def transcription(self, record_id, **options):
        return self._read("transcription", {"record_id": record_id}, **options)

    def meeting(self, meeting_id, **options):
        return self._read("meeting", {"meeting_id": meeting_id}, **options)

    def segments(self, meeting_id, *, include_remote=False, device_id=None, **params):
        return self._read(
            "segments", dict(meeting_id=meeting_id, **params), include_remote, device_id
        )

    def segment(self, meeting_id, segment_id, **options):
        return self._read(
            "segment", dict(meeting_id=meeting_id, segment_id=segment_id), **options
        )

    def insights(self, meeting_id, **options):
        return self._read("insights", {"meeting_id": meeting_id}, **options)

    def transcriptions(self, **params):
        return self._page("transcriptions", **params)

    def meetings(self, **params):
        return self._page("meetings", **params)

    def search(self, q, **params):
        return self._page("search", q=q, **params)

    def _page(
        self,
        operation,
        *,
        include_clients=False,
        device_id=None,
        cursor=None,
        limit=20,
        **params,
    ):
        if not include_clients and device_id is None:
            return getattr(self.store, operation)(**params, cursor=cursor, limit=limit)
        if self.remote is None and include_clients:
            raise ClientUnavailable("unsupported")
        scope = hashlib.sha256(
            json.dumps([operation, params, device_id, limit], sort_keys=True).encode()
        ).hexdigest()
        if not 1 <= limit <= 100:
            raise InvalidQuery("limit must be between 1 and 100.")
        statuses = self.clients()
        if device_id:
            statuses = [s for s in statuses if s.device_id == device_id]
            if not statuses:
                if not params.get("include_remote"):
                    raise ClientUnavailable()
                statuses = [
                    ClientStatus(
                        device_id=device_id,
                        name="Paired computer",
                        status="unavailable",
                    )
                ]
        status_map = {s.device_id: s for s in statuses}
        if cursor:
            with self._lock:
                saved = self._cursors.get(cursor)
                if saved is None or saved[0] < time.monotonic() or saved[1] != scope:
                    raise InvalidQuery(
                        "Client history cursor expired or used with different filters. Restart the query."
                    )
                sources, seen = copy.deepcopy(saved[2])
            for source, state in sources.items():
                if source is not None and source not in status_map:
                    status_map[source] = ClientStatus(
                        device_id=source, name=state["name"], status="unavailable"
                    )
            statuses = [status_map[source] for source in sources if source is not None]
        else:
            seen = set()
            sources = {
                s.device_id: {
                    "items": [],
                    "cursor": None,
                    "started": False,
                    "name": s.name,
                }
                for s in statuses
            }
            if device_id is None or params.get("include_remote"):
                sources[None] = {"items": [], "cursor": None, "started": False}

        def fetch(source):
            state = sources[source]
            if source is not None:
                status = status_map.get(source)
                if status is None or status.status != "online":
                    state.update(items=[], cursor=None, started=True)
                    return
                # Recheck client consent even when a cursor has buffered previews.
                try:
                    self.remote.query(source, "check", {})
                except ClientUnavailable as exc:
                    status.status = (
                        "sharing_disabled"
                        if exc.code == "sharing_disabled"
                        else "unavailable"
                    )
                    state.update(items=[], cursor=None, started=True)
                    return
            if state["items"] or (state["started"] and state["cursor"] is None):
                return
            try:
                if source is None:
                    local_params = dict(params)
                    if device_id is not None:
                        local_params["origin_device_id"] = device_id
                    page = getattr(self.store, operation)(
                        **local_params, cursor=state["cursor"], limit=limit
                    )
                else:
                    remote_params = {
                        k: v
                        for k, v in params.items()
                        if k != "include_remote" and v is not None
                    }
                    page = self._remote(
                        source,
                        operation,
                        dict(remote_params, cursor=state["cursor"], limit=limit),
                    )
                state.update(
                    items=list(page.items), cursor=page.next_cursor, started=True
                )
            except ClientUnavailable as exc:
                status_map[source].status = (
                    "sharing_disabled"
                    if exc.code == "sharing_disabled"
                    else "unavailable"
                )
                state.update(items=[], cursor=None, started=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, sources))
        items = []
        while len(items) < limit:
            candidates = [
                (source, state["items"][0])
                for source, state in sources.items()
                if state["items"]
            ]
            if not candidates:
                break
            source, item = min(
                candidates, key=lambda pair: (_order(pair[1]), pair[0] is None)
            )
            identity = _identity(item)
            # Copies saved by Both and their live originals produce one citation.
            for other, state in sources.items():
                if state["items"] and _identity(state["items"][0]) == identity:
                    state["items"].pop(0)
                    if not state["items"] and state["cursor"]:
                        fetch(other)
            if identity not in seen:
                items.append(item)
                seen.add(identity)
        next_cursor = None
        if any(s["items"] or s["cursor"] for s in sources.values()):
            next_cursor = "clients-" + uuid.uuid4().hex
            with self._lock:
                now = time.monotonic()
                for key in list(self._cursors):
                    if self._cursors[key][0] < now:
                        self._cursors.pop(key)
                while len(self._cursors) >= _MAX_CURSORS:
                    self._cursors.popitem(last=False)
                self._cursors[next_cursor] = (now + _CURSOR_TTL, scope, (sources, seen))
        return Page[_MODELS[operation]](
            items=items, next_cursor=next_cursor, clients=statuses
        )
