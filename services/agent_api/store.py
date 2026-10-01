"""Bounded retrieval using read-only connections, with no app startup or writes.

The HTTP layer and future MCP adapters can share this store. Search v1 uses
literal substring matching, independent of optional FTS indexes or cloud models.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy import and_, case, create_engine, func, literal, or_, select, union_all
from sqlalchemy.pool import NullPool

from services.agent_api.models import (
    Insight,
    Meeting,
    MeetingInsights,
    Page,
    Participant,
    Question,
    Report,
    SearchHit,
    Segment,
    Transcription,
    TranscriptionSummary,
)
from services.models import MeetingSegment, MeetingSession, TranscriptionHistory

H = TranscriptionHistory.__table__
M = MeetingSession.__table__
S = MeetingSegment.__table__
CARDS = {
    "key_points",
    "decisions",
    "action_items",
    "risks",
    "timeline",
    "live_notes",
    "user_notes",
}


class InvalidQuery(ValueError):
    pass


class NotFound(LookupError):
    pass


class SnapshotUnavailable(ValueError):
    pass


def _columns(table, model):
    return [table.c[name] for name in model.model_fields if name in table.c]


def _time(column):
    # SQLite normalizes offsets; legacy naive timestamps are interpreted as UTC.
    return func.coalesce(func.julianday(column), 0.0)


def _match(column, query):
    return column.icontains(query, autoescape=True)


def _excerpt(column, query):
    # Bound text in SQL before loading rows. instr/lower and LIKE share SQLite's
    # ASCII case folding; non-ASCII characters match literally.
    start = func.max(1, func.instr(func.lower(column), func.lower(literal(query))) - 80)
    return func.substr(column, start, 320)


def _filters(table, timestamp, include_remote=False, since=None, before=None, origin_device_id=None):
    clauses = []
    if not include_remote:
        clauses.append(table.c.origin_device_id.is_(None))
    if origin_device_id is not None:
        clauses.append(table.c.origin_device_id == origin_device_id)
    if since is not None:
        clauses.append(_time(timestamp) >= func.julianday(since))
    if before is not None:
        clauses.append(_time(timestamp) < func.julianday(before))
    return clauses


class HistoryStore:
    def __init__(self, database: str | Path):
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise ValueError(
                "Database does not exist. Open OpenWhisper first or select --database."
            )
        uri = path.as_uri() + "?mode=ro"

        def connect():
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
            conn.execute("PRAGMA query_only=ON")
            return conn

        self.engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)
        try:
            # Never run migrations from a second process against a live app.
            from services.database import SCHEMA_VERSION

            with self.engine.connect() as conn:
                version = conn.exec_driver_sql(
                    "SELECT version FROM schema_version"
                ).scalar()
                if version != SCHEMA_VERSION:
                    raise ValueError(
                        "Database schema is incompatible. Open it with this version of OpenWhisper first."
                    )
                for table in (H, M, S):
                    conn.execute(select(table).limit(0))
        except Exception:
            self.close()
            raise

    def close(self):
        self.engine.dispose()

    def check(self):
        with self.engine.connect() as conn:
            conn.execute(select(H.c.id).limit(1))

    def _one(self, statement):
        with self.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
        if row is None:
            raise NotFound("Record not found.")
        return dict(row)

    def _page(
        self,
        statement,
        model,
        *,
        scope,
        keys,
        descending,
        limit=20,
        cursor=None,
        transform=None,
    ):
        if not 1 <= limit <= 100:
            raise InvalidQuery("limit must be between 1 and 100.")
        fingerprint = hashlib.sha256(
            json.dumps(scope, sort_keys=True).encode()
        ).hexdigest()[:24]
        query = statement.subquery()
        statement = select(query)
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError
                decoded = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                values = decoded["keys"]
                if decoded["scope"] != fingerprint or len(values) != len(keys):
                    raise ValueError
                if not isinstance(values, list) or any(
                    not isinstance(v, (str, int, float)) for v in values
                ):
                    raise ValueError
                # First key is always a finite timestamp/start time, then IDs.
                import math

                if (
                    isinstance(values[0], bool)
                    or not isinstance(values[0], (int, float))
                    or not math.isfinite(values[0])
                ):
                    raise ValueError
                if any(not isinstance(v, str) for v in values[1:]):
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise InvalidQuery(
                    "Invalid cursor or cursor used with different filters."
                ) from exc
            terms = []
            for i, (name, desc) in enumerate(zip(keys, descending, strict=True)):
                column = query.c[name]
                comparison = column < values[i] if desc else column > values[i]
                terms.append(
                    and_(*(query.c[keys[j]] == values[j] for j in range(i)), comparison)
                )
            statement = statement.where(or_(*terms))
        statement = statement.order_by(
            *(
                query.c[name].desc() if desc else query.c[name].asc()
                for name, desc in zip(keys, descending, strict=True)
            )
        ).limit(limit + 1)
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(statement).mappings()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            payload = {"scope": fingerprint, "keys": [rows[-1][k] for k in keys]}
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(payload).encode()
            ).decode()
        items = [
            model.model_validate(transform(row) if transform else row) for row in rows
        ]
        return Page[model](items=items, next_cursor=next_cursor)

    def transcriptions(self, *, q=None, limit=20, cursor=None, **filters):
        statement = select(
            *_columns(H, TranscriptionSummary),
            func.substr(H.c.text, 1, 320).label("preview"),
            _time(H.c.timestamp).label("sort_time"),
        )
        statement = statement.where(*_filters(H, H.c.timestamp, **filters))
        if q:
            statement = statement.where(
                or_(
                    *(
                        _match(H.c[field], q)
                        for field in ("text", "raw_text", "source_name", "title")
                    )
                )
            )
        return self._page(
            statement,
            TranscriptionSummary,
            scope=["transcriptions", q, filters],
            keys=["sort_time", "id"],
            descending=[True, False],
            limit=limit,
            cursor=cursor,
        )

    def transcription(self, record_id, *, include_remote=False):
        row = self._one(
            select(
                *_columns(H, Transcription),
                func.substr(H.c.text, 1, 320).label("preview"),
            ).where(H.c.id == record_id, *_filters(H, H.c.timestamp, include_remote))
        )
        return Transcription.model_validate(row)

    def meetings(self, *, q=None, limit=20, cursor=None, **filters):
        statement = select(
            *_columns(M, Meeting), _time(M.c.started_at).label("sort_time")
        )
        statement = statement.where(*_filters(M, M.c.started_at, **filters))
        if q:
            statement = statement.where(_match(M.c.title, q))
        return self._page(
            statement,
            Meeting,
            scope=["meetings", q, filters],
            keys=["sort_time", "id"],
            descending=[True, False],
            limit=limit,
            cursor=cursor,
        )

    def meeting(self, meeting_id, *, include_remote=False):
        return Meeting.model_validate(
            self._one(
                select(*_columns(M, Meeting)).where(
                    M.c.id == meeting_id, *_filters(M, M.c.started_at, include_remote)
                )
            )
        )

    def segments(
        self,
        meeting_id,
        *,
        start_s=None,
        end_s=None,
        limit=20,
        cursor=None,
        include_remote=False,
    ):
        self.meeting(meeting_id, include_remote=include_remote)
        statement = select(*_columns(S, Segment)).where(S.c.meeting_id == meeting_id)
        if start_s is not None:
            statement = statement.where(S.c.start_s >= start_s)
        if end_s is not None:
            statement = statement.where(S.c.start_s < end_s)
        return self._page(
            statement,
            Segment,
            scope=["segments", meeting_id, start_s, end_s, include_remote],
            keys=["start_s", "id"],
            descending=[False, False],
            limit=limit,
            cursor=cursor,
        )

    def segment(self, meeting_id, segment_id, *, include_remote=False):
        return Segment.model_validate(
            self._one(
                select(*_columns(S, Segment))
                .select_from(S.join(M))
                .where(
                    S.c.meeting_id == meeting_id,
                    S.c.id == segment_id,
                    *_filters(M, M.c.started_at, include_remote),
                )
            )
        )

    def insights(self, meeting_id, *, include_remote=False):
        row = self._one(
            select(M.c.state_json, M.c.state_seq).where(
                M.c.id == meeting_id, *_filters(M, M.c.started_at, include_remote)
            )
        )
        base = dict(
            meeting_id=meeting_id,
            state_seq=row["state_seq"],
            snapshot_available=bool(row["state_json"]),
        )
        if not row["state_json"]:
            return MeetingInsights(**base)
        try:
            state = json.loads(row["state_json"])
            return MeetingInsights(
                **base,
                rolling_summary=state.get("rolling_summary", ""),
                rolling_summary_evidence=state.get("rolling_summary_evidence", []),
                participants=[
                    Participant.model_validate(p)
                    for p in state.get("participants", {}).values()
                ],
                cards=[
                    Insight.model_validate({**item, "card": card})
                    for card, items in state.get("cards", {}).items()
                    if card in CARDS
                    for item in items
                    if item.get("status") != "removed"
                ],
                questions=[
                    Question.model_validate(q)
                    for q in state.get("questions", [])
                    if q.get("status") != "dismissed"
                ],
                custom_reports=[
                    Report.model_validate(r) for r in state.get("custom_reports", [])
                ],
            )
        except (ValueError, TypeError, AttributeError, ValidationError) as exc:
            raise SnapshotUnavailable(
                "Stored meeting insights could not be read; the transcript is still available."
            ) from exc

    def search(self, q, *, kind="all", limit=20, cursor=None, **filters):
        if not q or not q.strip() or len(q) > 500:
            raise InvalidQuery("q must contain between 1 and 500 characters.")
        if kind not in {"all", "transcription", "meeting"}:
            raise InvalidQuery("Unknown search kind.")
        statements = []
        if kind in {"all", "transcription"}:
            field = case(
                (_match(H.c.text, q), literal("text")),
                (_match(H.c.raw_text, q), literal("raw_text")),
                (_match(H.c.title, q), literal("title")),
                else_=literal("source_name"),
            )
            content = case(
                (_match(H.c.text, q), H.c.text),
                (_match(H.c.raw_text, q), H.c.raw_text),
                (_match(H.c.title, q), H.c.title),
                else_=H.c.source_name,
            )
            statements.append(
                select(
                    literal("transcription").label("kind"),
                    H.c.id,
                    literal(None).label("meeting_id"),
                    func.coalesce(H.c.title, H.c.source_name, literal("Transcription")).label(
                        "title"
                    ),
                    H.c.timestamp,
                    _excerpt(content, q).label("excerpt"),
                    field.label("matched_field"),
                    literal(None).label("start_s"),
                    literal(None).label("end_s"),
                    H.c.origin_device_id,
                    _time(H.c.timestamp).label("sort_time"),
                ).where(
                    or_(
                        *(
                            _match(H.c[name], q)
                            for name in ("text", "raw_text", "source_name", "title")
                        )
                    ),
                    *_filters(H, H.c.timestamp, **filters),
                )
            )
        if kind in {"all", "meeting"}:
            statements.append(
                select(
                    literal("meeting").label("kind"),
                    M.c.id,
                    M.c.id.label("meeting_id"),
                    M.c.title,
                    M.c.started_at.label("timestamp"),
                    _excerpt(M.c.title, q).label("excerpt"),
                    literal("title").label("matched_field"),
                    literal(None).label("start_s"),
                    literal(None).label("end_s"),
                    M.c.origin_device_id,
                    _time(M.c.started_at).label("sort_time"),
                ).where(_match(M.c.title, q), *_filters(M, M.c.started_at, **filters))
            )
            statements.append(
                select(
                    literal("segment").label("kind"),
                    S.c.id,
                    S.c.meeting_id,
                    M.c.title,
                    M.c.started_at.label("timestamp"),
                    _excerpt(S.c.text, q).label("excerpt"),
                    literal("text").label("matched_field"),
                    S.c.start_s,
                    S.c.end_s,
                    M.c.origin_device_id,
                    _time(M.c.started_at).label("sort_time"),
                )
                .select_from(S.join(M))
                .where(_match(S.c.text, q), *_filters(M, M.c.started_at, **filters))
            )

        def with_resource(row):
            record_id = quote(row["id"], safe="")
            if row["kind"] == "transcription":
                row["resource"] = f"/v1/transcriptions/{record_id}"
            else:
                meeting_id = quote(row["meeting_id"], safe="")
                row["resource"] = f"/v1/meetings/{meeting_id}"
                if row["kind"] == "segment":
                    row["resource"] += f"/segments/{record_id}"
            if row["origin_device_id"] is not None:
                row["resource"] += "?include_remote=true"
            return row

        return self._page(
            union_all(*statements),
            SearchHit,
            scope=["search", q, kind, filters],
            keys=["sort_time", "kind", "id"],
            descending=[True, False, False],
            limit=limit,
            cursor=cursor,
            transform=with_resource,
        )
