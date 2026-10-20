from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.agent_api.store import (
    HistoryStore,
    InvalidQuery,
    NotFound,
    SnapshotUnavailable,
)
from services.remote_asr import protocol, settings as remote_settings
from services.remote_asr.client import RemoteConnection


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    q: str | None = Field(default=None, min_length=1, max_length=500)
    kind: Literal["all", "meeting", "transcription"] = "all"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    since: str | None = Field(default=None, max_length=80)
    before: str | None = Field(default=None, max_length=80)
    record_id: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[^/\\?#\x00-\x1f]+$"
    )
    meeting_id: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[^/\\?#\x00-\x1f]+$"
    )
    segment_id: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[^/\\?#\x00-\x1f]+$"
    )
    start_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)


_FIELDS = {
    "check": set(),
    "transcriptions": {"q", "limit", "cursor", "since", "before"},
    "meetings": {"q", "limit", "cursor", "since", "before"},
    "search": {"q", "kind", "limit", "cursor", "since", "before"},
    "transcription": {"record_id"},
    "meeting": {"meeting_id"},
    "segments": {"meeting_id", "limit", "cursor", "start_s", "end_s"},
    "segment": {"meeting_id", "segment_id"},
    "insights": {"meeting_id"},
}
_REQUIRED = {
    "transcription": ("record_id",),
    "meeting": ("meeting_id",),
    "segments": ("meeting_id",),
    "segment": ("meeting_id", "segment_id"),
    "insights": ("meeting_id",),
    "search": ("q",),
}
# Seconds before reconnecting, and before asking again a host without client history.
_RETRY_S = 10
_RETRY_UNSUPPORTED_S = 60


def _query_values(operation, params):
    """The store arguments for a host's query, or InvalidQuery/ValidationError."""
    if (
        operation not in _FIELDS
        or not isinstance(params, dict)
        or set(params) - _FIELDS[operation]
    ):
        raise InvalidQuery()
    query = _Query.model_validate(params)
    values = query.model_dump(include=query.model_fields_set, exclude_none=True)
    _dates_to_utc(values)
    if (
        values.get("since")
        and values.get("before")
        and values["since"] >= values["before"]
    ):
        raise InvalidQuery()
    if (
        query.start_s is not None
        and query.end_s is not None
        and query.start_s >= query.end_s
    ):
        raise InvalidQuery()
    if any(name not in values for name in _REQUIRED.get(operation, ())):
        raise InvalidQuery()
    return values


def _dates_to_utc(values):
    for name in ("since", "before"):
        if name in values:
            date = datetime.fromisoformat(values[name])
            if date.tzinfo is None:
                raise InvalidQuery()
            values[name] = date.astimezone(UTC).isoformat()


class HistoryReader:
    def __init__(self, database, enabled):
        self.store = HistoryStore(database)
        self.enabled = enabled

    def query(self, operation, params):
        if not self.enabled():
            return {"error": "sharing_disabled"}
        try:
            values = _query_values(operation, params)
            result = getattr(self.store, operation)(**values)
            data = result.model_dump(mode="json") if result is not None else {}
            return {"result": data} if self.enabled() else {"error": "sharing_disabled"}
        except NotFound:
            return {"error": "not_found"}
        except SnapshotUnavailable:
            return {"error": "snapshot_unavailable"}
        except (InvalidQuery, ValidationError, ValueError, TypeError):
            return {"error": "invalid_query"}
        except Exception:
            return {"error": "unavailable"}


class HistoryClient:
    def __init__(self, database=None, *, pairing=None, token=None, enabled=None):
        self.database = database
        self.pairing = pairing or remote_settings.load_client_pairing
        self.token = token or remote_settings.load_client_token
        self.enabled = enabled or remote_settings.client_shares_history
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._connection = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="client-history", daemon=True
            )
            self._thread.start()

    def refresh(self):
        self._wake.set()
        with self._lock:
            connection = self._connection
        if connection:
            connection.close()

    def stop(self):
        self._stop.set()
        self.refresh()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self._wake.wait(self._connect_once())

    def _connect_once(self):
        """Serve the host's queries over one connection; returns the seconds to wait after."""
        connection = None
        reader = None
        retry_delay = _RETRY_S
        try:
            pairing = self.pairing()
            token = self.token() if pairing else None
            if pairing and token:
                connection = RemoteConnection(
                    pairing.host,
                    pairing.port,
                    token,
                    pairing.fingerprint,
                    alternates=pairing.alternates,
                    history_enabled=self.enabled(),
                )
                with self._lock:
                    self._connection = connection
                ready = connection.connect()
                if ready.get("capabilities", {}).get("client_history") is True:
                    from config import config

                    reader = HistoryReader(
                        self.database or config.DATABASE_FILE, self.enabled
                    )
                    self._answer_queries(connection, reader)
                else:
                    retry_delay = _RETRY_UNSUPPORTED_S
        except Exception:
            # Reconnection never interrupts speech or moves saved records.
            pass
        finally:
            if connection:
                connection.close()
            if reader:
                reader.store.close()
            with self._lock:
                self._connection = None
        return retry_delay

    def _answer_queries(self, connection, reader):
        while not self._stop.is_set() and not self._wake.is_set():
            try:
                frame = connection.receive_history()
            except TimeoutError:
                continue
            request = _history_query(frame)
            if request is None:
                break
            connection.send_history(_history_reply(reader, request))


def _history_query(frame):
    """The host's query in ``frame``, or None for anything else (which ends the session)."""
    if (
        not isinstance(frame, str)
        or len(frame.encode("utf-8")) > protocol.MAX_HEADER_BYTES
    ):
        return None
    request = json.loads(frame)
    if not isinstance(request, dict) or request.get("type") != "history_query":
        return None
    return request


def _history_reply(reader, request):
    response = reader.query(request.get("operation"), request.get("params"))
    response.update(type="history_result", id=request.get("id"))
    payload = json.dumps(response)
    if len(payload.encode("utf-8")) > protocol.MAX_REPLY_BYTES:
        payload = json.dumps(
            {
                "type": "history_result",
                "id": request.get("id"),
                "error": "unavailable",
            }
        )
    return payload
