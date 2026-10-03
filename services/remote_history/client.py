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


class HistoryReader:
    def __init__(self, database, enabled):
        self.store = HistoryStore(database)
        self.enabled = enabled

    def query(self, operation, params):
        if not self.enabled():
            return {"error": "sharing_disabled"}
        try:
            if (
                operation not in _FIELDS
                or not isinstance(params, dict)
                or set(params) - _FIELDS[operation]
            ):
                raise InvalidQuery()
            query = _Query.model_validate(params)
            values = query.model_dump(include=query.model_fields_set, exclude_none=True)
            for name in ("since", "before"):
                if name in values:
                    date = datetime.fromisoformat(values[name])
                    if date.tzinfo is None:
                        raise InvalidQuery()
                    values[name] = date.astimezone(UTC).isoformat()
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
            required = {
                "transcription": ("record_id",),
                "meeting": ("meeting_id",),
                "segments": ("meeting_id",),
                "segment": ("meeting_id", "segment_id"),
                "insights": ("meeting_id",),
                "search": ("q",),
            }.get(operation, ())
            if any(name not in values for name in required):
                raise InvalidQuery()
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
            connection = None
            reader = None
            retry_delay = 10
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
                        while not self._stop.is_set() and not self._wake.is_set():
                            try:
                                frame = connection.receive_history()
                            except TimeoutError:
                                continue
                            if (
                                not isinstance(frame, str)
                                or len(frame.encode("utf-8"))
                                > protocol.MAX_HEADER_BYTES
                            ):
                                break
                            request = json.loads(frame)
                            if (
                                not isinstance(request, dict)
                                or request.get("type") != "history_query"
                            ):
                                break
                            response = reader.query(
                                request.get("operation"), request.get("params")
                            )
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
                            connection.send_history(payload)
                    else:
                        retry_delay = 60
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
            self._wake.wait(retry_delay)
