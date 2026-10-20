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
        statuses = self._client_statuses(device_id, params)
        status_map = {s.device_id: s for s in statuses}
        if cursor:
            sources, seen = self._resume(cursor, scope)
            statuses = _resumed_statuses(sources, status_map)
        else:
            seen = set()
            sources = _fresh_sources(statuses, device_id is None or params.get("include_remote"))
        merge = _PageMerge(self, operation, params, device_id, limit, sources, status_map)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(merge.fetch, sources))
        items = merge.take(seen)
        next_cursor = None
        if merge.has_more():
            next_cursor = self._save_cursor(scope, sources, seen)
        return Page[_MODELS[operation]](
            items=items, next_cursor=next_cursor, clients=statuses
        )

    def _client_statuses(self, device_id, params):
        """The clients a page reads from: all of them, or the one ``device_id`` names."""
        statuses = self.clients()
        if not device_id:
            return statuses
        statuses = [s for s in statuses if s.device_id == device_id]
        if statuses:
            return statuses
        if not params.get("include_remote"):
            raise ClientUnavailable()
        return [
            ClientStatus(
                device_id=device_id,
                name="Paired computer",
                status="unavailable",
            )
        ]

    def _resume(self, cursor, scope):
        """A copy of the sources and seen identities a cursor saved."""
        with self._lock:
            saved = self._cursors.get(cursor)
            if saved is None or saved[0] < time.monotonic() or saved[1] != scope:
                raise InvalidQuery(
                    "Client history cursor expired or used with different filters. "
                    "Restart the query."
                )
            return copy.deepcopy(saved[2])

    def _save_cursor(self, scope, sources, seen):
        next_cursor = "clients-" + uuid.uuid4().hex
        with self._lock:
            now = time.monotonic()
            for key in list(self._cursors):
                if self._cursors[key][0] < now:
                    self._cursors.pop(key)
            while len(self._cursors) >= _MAX_CURSORS:
                self._cursors.popitem(last=False)
            self._cursors[next_cursor] = (now + _CURSOR_TTL, scope, (sources, seen))
        return next_cursor


def _resumed_statuses(sources, status_map):
    """Statuses for a resumed cursor's clients; ones no longer paired read as unavailable."""
    for source, state in sources.items():
        if source is not None and source not in status_map:
            status_map[source] = ClientStatus(
                device_id=source, name=state["name"], status="unavailable"
            )
    return [status_map[source] for source in sources if source is not None]


def _fresh_sources(statuses, include_local):
    sources = {
        s.device_id: {
            "items": [],
            "cursor": None,
            "started": False,
            "name": s.name,
        }
        for s in statuses
    }
    if include_local:
        sources[None] = {"items": [], "cursor": None, "started": False}
    return sources


def _unavailable_status(exc):
    return "sharing_disabled" if exc.code == "sharing_disabled" else "unavailable"


def _exhaust(state):
    state.update(items=[], cursor=None, started=True)


class _PageMerge:
    """One page merged from each source's buffered items, newest first.

    ``sources`` maps a device id (None for this computer) to its buffered
    items, its next cursor and whether it was read yet.
    """

    def __init__(self, history, operation, params, device_id, limit, sources, status_map):
        self.history = history
        self.operation = operation
        self.params = params
        self.device_id = device_id
        self.limit = limit
        self.sources = sources
        self.status_map = status_map

    def fetch(self, source):
        state = self.sources[source]
        if source is not None and not self._client_ready(source, state):
            return
        if state["items"] or (state["started"] and state["cursor"] is None):
            return
        try:
            page = self._read_page(source, state["cursor"])
            state.update(
                items=list(page.items), cursor=page.next_cursor, started=True
            )
        except ClientUnavailable as exc:
            self.status_map[source].status = _unavailable_status(exc)
            _exhaust(state)

    def _client_ready(self, source, state):
        status = self.status_map.get(source)
        if status is None or status.status != "online":
            _exhaust(state)
            return False
        # Recheck client consent even when a cursor has buffered previews.
        try:
            self.history.remote.query(source, "check", {})
        except ClientUnavailable as exc:
            status.status = _unavailable_status(exc)
            _exhaust(state)
            return False
        return True

    def _read_page(self, source, cursor):
        if source is None:
            local_params = dict(self.params)
            if self.device_id is not None:
                local_params["origin_device_id"] = self.device_id
            return getattr(self.history.store, self.operation)(
                **local_params, cursor=cursor, limit=self.limit
            )
        remote_params = {
            k: v
            for k, v in self.params.items()
            if k != "include_remote" and v is not None
        }
        return self.history._remote(
            source,
            self.operation,
            dict(remote_params, cursor=cursor, limit=self.limit),
        )

    def take(self, seen):
        """Up to ``limit`` items in order, each identity once across pages."""
        items = []
        while len(items) < self.limit:
            item = self._next_item()
            if item is None:
                break
            identity = _identity(item)
            self._drop_heads(identity)
            if identity not in seen:
                items.append(item)
                seen.add(identity)
        return items

    def _next_item(self):
        candidates = [
            (source, state["items"][0])
            for source, state in self.sources.items()
            if state["items"]
        ]
        if not candidates:
            return None
        _source, item = min(
            candidates, key=lambda pair: (_order(pair[1]), pair[0] is None)
        )
        return item

    def _drop_heads(self, identity):
        # Copies saved by Both and their live originals produce one citation.
        for other, state in self.sources.items():
            if state["items"] and _identity(state["items"][0]) == identity:
                state["items"].pop(0)
                if not state["items"] and state["cursor"]:
                    self.fetch(other)

    def has_more(self):
        return any(s["items"] or s["cursor"] for s in self.sources.values())
