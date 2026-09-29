"""Exercise the public agent contract against real, disposable SQLite data."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from services.agent_api.app import create_app
from services.agent_api.store import HistoryStore
from services.database import DatabaseManager
from services.models import MeetingSegment, MeetingSession, TranscriptionHistory

TOKEN = "test-history-token-" + "a" * 32
DATE = "2026-09-01T12:00:00+00:00"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "history # café.db"
    db = DatabaseManager(str(path))
    snapshot = {
        "rolling_summary": "Launch plan agreed.",
        "rolling_summary_evidence": ["sg_a"],
        "participants": {
            "p_a": {
                "id": "p_a",
                "display_name": "Alex",
                "kind": "me",
                "token": "private-marker",
            }
        },
        "cards": {
            "action_items": [
                {
                    "id": "it_a",
                    "text": "Ship the launch",
                    "status": "confirmed",
                    "evidence": ["sg_a"],
                    "data": {
                        "owner_participant_id": "p_a",
                        "private_path": "private-marker",
                    },
                },
                {"id": "it_removed", "text": "Removed note", "status": "removed"},
            ]
        },
        "questions": [
            {
                "id": "q_a",
                "text": "When?",
                "answer": "Friday",
                "status": "resolved",
                "evidence": ["sg_b"],
            }
        ],
        "custom_reports": [
            {
                "id": "r_a",
                "title": "Plan",
                "markdown": "Launch Friday",
                "status": "ready",
                "sources": {"path": "private-marker"},
            }
        ],
        "capture": {"path": "private-marker"},
    }
    with db.get_session() as session:
        session.add_all(
            [
                TranscriptionHistory(
                    id="h_a",
                    text="The launch is Friday",
                    raw_text="raw phrase only",
                    timestamp=DATE,
                    model="test",
                    audio_file="private-marker",
                    source_name="plan.wav",
                ),
                TranscriptionHistory(
                    id="h_b",
                    text="Literal 50%_quote ' OR 1=1 --",
                    timestamp=DATE,
                    model="test",
                ),
                TranscriptionHistory(
                    id="h_c",
                    text="x" * 1000 + " Needle near end",
                    timestamp="2026-09-01T09:00:00-07:00",
                    model="test",
                ),
                TranscriptionHistory(
                    id="h_old",
                    text="launch before",
                    timestamp="2026-08-31T23:00:00",
                    model="test",
                ),
                TranscriptionHistory(
                    id="h_remote",
                    text="launch remote",
                    timestamp=DATE,
                    model="test",
                    origin_device_id="device_a",
                ),
            ]
        )
        for meeting_id, origin in (
            ("m_a", None),
            ("m_remote", "device_a"),
            ("m_empty", None),
        ):
            session.add(
                MeetingSession(
                    id=meeting_id,
                    title="Launch review",
                    status="ended",
                    started_at=DATE,
                    ended_at=DATE,
                    host_token="private-marker",
                    guest_token="private-marker",
                    spool_dir="private-marker",
                    state_seq=7,
                    state_json=json.dumps(snapshot)
                    if meeting_id != "m_empty"
                    else None,
                    agent_endpoint_json=json.dumps({"url": "private-marker"}),
                    origin_device_id=origin,
                )
            )
        session.flush()
        for segment_id, meeting_id, start in (
            ("sg_a", "m_a", 0),
            ("sg_b", "m_a", 0),
            ("sg_c", "m_a", 10),
            ("sg_remote", "m_remote", 0),
        ):
            session.add(
                MeetingSegment(
                    id=segment_id,
                    meeting_id=meeting_id,
                    start_s=start,
                    end_s=start + 3,
                    text="We launch Friday",
                    channel="mic",
                    created_at=DATE,
                    speaker_source="human",
                    speaker_participant_id="p_a",
                    embedding=b"private-marker",
                )
            )
    yield db, path
    db.close()


@pytest.fixture
def client(database):
    with TestClient(
        create_app(database[1], TOKEN),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


def all_pages(client, path, **params):
    items, cursors = [], set()
    while True:
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(page["items"])
        if not page["next_cursor"]:
            return items
        assert page["next_cursor"] not in cursors
        cursors.add(page["next_cursor"])
        params["cursor"] = page["next_cursor"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/status",
        "/v1/transcriptions",
        "/v1/meetings",
        "/v1/search?q=launch",
        "/openapi.json",
    ],
)
@pytest.mark.parametrize("authorization", ["", "Bearer wrong", f"Basic {TOKEN}"])
def test_every_route_requires_token(client, path, authorization):
    response = client.get(path, headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["code"] == "unauthorized"


def test_token_is_not_accepted_in_query_or_cookie(client):
    response = client.get(
        "/v1/status",
        params={"token": TOKEN},
        headers={"Authorization": "", "Cookie": f"token={TOKEN}"},
    )
    assert response.status_code == 401


def test_host_and_browser_origin_are_rejected(client):
    for headers in (
        {"Host": "evil.example"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ):
        assert client.get("/v1/status", headers=headers).status_code == 403
    assert (
        client.get("/v1/status", headers={"Host": "localhost:8766"}).status_code == 200
    )


def test_transcription_pages_have_stable_ties_and_normalized_time(client):
    items = all_pages(client, "/v1/transcriptions", limit=1)
    assert [row["id"] for row in items] == ["h_c", "h_a", "h_b", "h_old"]
    assert all(len(row["preview"]) <= 320 and "text" not in row for row in items)
    assert client.get("/v1/transcriptions/h_a").json()["raw_text"] == "raw phrase only"


def test_cursor_survives_newer_insert_and_deleted_previous_row(client, database):
    first = client.get("/v1/transcriptions", params={"limit": 1}).json()
    db, _ = database
    db.delete_history_entry("h_c")
    db.add_history_entry("h_new", "new", "2026-10-01T00:00:00Z", "test")
    remaining = all_pages(
        client, "/v1/transcriptions", limit=1, cursor=first["next_cursor"]
    )
    assert [row["id"] for row in remaining] == ["h_a", "h_b", "h_old"]


def test_cursor_is_validated_and_bound_to_filters(client):
    cursor = client.get("/v1/transcriptions", params={"limit": 1}).json()["next_cursor"]
    for path, params in [
        ("/v1/transcriptions", {"cursor": "bad"}),
        ("/v1/transcriptions", {"cursor": cursor, "q": "launch"}),
        ("/v1/transcriptions", {"cursor": cursor, "include_remote": True}),
        ("/v1/meetings", {"cursor": cursor}),
        ("/v1/transcriptions", {"cursor": base64.urlsafe_b64encode(b"null").decode()}),
    ]:
        assert client.get(path, params=params).status_code == 400


def test_search_returns_fetchable_citations_and_raw_matches(client):
    items = all_pages(client, "/v1/search", q="launch", limit=1)
    assert {row["kind"] for row in items} == {"transcription", "meeting", "segment"}
    assert len({(row["kind"], row["id"]) for row in items}) == len(items) == 7
    for row in items:
        detail = client.get(row["resource"])
        assert detail.status_code == 200
        assert detail.json()["id"] == row["id"]
    raw = client.get("/v1/search", params={"q": "raw phrase"}).json()["items"]
    assert len(raw) == 1 and raw[0]["matched_field"] == "raw_text"
    assert "raw phrase" in raw[0]["excerpt"]
    assert (
        client.get("/v1/transcriptions", params={"q": "raw phrase"}).json()["items"][0][
            "id"
        ]
        == "h_a"
    )
    late = client.get("/v1/search", params={"q": "needle"}).json()["items"][0]
    assert "Needle" in late["excerpt"] and len(late["excerpt"]) <= 320


@pytest.mark.parametrize("query", ["50%_", "' OR 1=1 --", "%", "_"])
def test_search_treats_wildcards_and_sql_as_literal_text(client, query):
    assert [
        row["id"]
        for row in client.get("/v1/search", params={"q": query}).json()["items"]
    ] == ["h_b"]


def test_date_kind_and_title_filters(client):
    items = all_pages(
        client,
        "/v1/search",
        q="launch",
        kind="transcription",
        since="2026-09-01T00:00:00Z",
        before="2026-09-02T00:00:00Z",
    )
    assert [row["id"] for row in items] == ["h_a"]
    assert client.get("/v1/meetings", params={"q": "review"}).json()["items"]
    assert not client.get("/v1/meetings", params={"q": "Friday"}).json()["items"]
    assert client.get("/v1/search", params={"q": "Friday", "kind": "meeting"}).json()[
        "items"
    ]


def test_non_ascii_excerpt_uses_same_matching_rules_as_search(client, database):
    database[0].add_history_entry("h_unicode", "x" * 900 + " ÉTÉ 東京", DATE, "test")
    for query in ("ÉTÉ", "東京"):
        hit = client.get("/v1/search", params={"q": query}).json()["items"][0]
        assert hit["id"] == "h_unicode" and query in hit["excerpt"]


def test_remote_records_require_explicit_opt_in_on_every_surface(client):
    paths = [
        "/v1/transcriptions/h_remote",
        "/v1/meetings/m_remote",
        "/v1/meetings/m_remote/segments",
        "/v1/meetings/m_remote/segments/sg_remote",
        "/v1/meetings/m_remote/insights",
    ]
    for path in paths:
        assert client.get(path).status_code == 404
        assert client.get(path, params={"include_remote": True}).status_code == 200
    for path, params, remote_id in [
        ("/v1/transcriptions", {}, "h_remote"),
        ("/v1/meetings", {}, "m_remote"),
        ("/v1/search", {"q": "launch"}, "sg_remote"),
    ]:
        assert remote_id not in {r["id"] for r in all_pages(client, path, **params)}
        assert remote_id in {
            r["id"] for r in all_pages(client, path, include_remote=True, **params)
        }
    for hit in all_pages(client, "/v1/search", q="launch", include_remote=True):
        assert client.get(hit["resource"]).status_code == 200


def test_segment_pagination_ranges_and_meeting_scope(client):
    assert [
        s["id"] for s in all_pages(client, "/v1/meetings/m_a/segments", limit=1)
    ] == ["sg_a", "sg_b", "sg_c"]
    assert [
        s["id"]
        for s in all_pages(client, "/v1/meetings/m_a/segments", start_s=0, end_s=10)
    ] == ["sg_a", "sg_b"]
    assert client.get("/v1/meetings/m_empty/segments/sg_a").status_code == 404
    assert client.get("/v1/meetings/m_missing/segments").status_code == 404


def test_insights_preserve_evidence_and_omit_removed_items(client):
    insights = client.get("/v1/meetings/m_a/insights").json()
    assert insights["snapshot_available"] and insights["state_seq"] == 7
    assert insights["rolling_summary_evidence"] == ["sg_a"]
    assert len(insights["cards"]) == 1
    assert insights["cards"][0]["data"]["owner_participant_id"] == "p_a"
    assert insights["cards"][0]["evidence"] == ["sg_a"]
    assert insights["participants"][0]["display_name"] == "Alex"
    assert insights["questions"][0]["answer"] == "Friday"
    assert insights["custom_reports"][0]["markdown"] == "Launch Friday"
    assert not client.get("/v1/meetings/m_empty/insights").json()["snapshot_available"]


def test_corrupt_snapshot_does_not_hide_transcript(client, database):
    with database[0].get_session() as session:
        session.get(MeetingSession, "m_a").state_json = "not json"
    response = client.get("/v1/meetings/m_a/insights")
    assert (
        response.status_code == 503
        and response.json()["error"]["code"] == "snapshot_unavailable"
    )
    assert client.get("/v1/meetings/m_a/segments").status_code == 200


def test_private_fields_are_never_serialized(client):
    for path in [
        "/v1/transcriptions",
        "/v1/transcriptions/h_a",
        "/v1/meetings",
        "/v1/meetings/m_a",
        "/v1/meetings/m_a/insights",
        "/v1/meetings/m_a/segments",
        "/v1/search?q=launch",
    ]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert "private-marker" not in response.text
        for field in (
            "host_token",
            "guest_token",
            "audio_file",
            "spool_dir",
            "embedding",
            "agent_endpoint_json",
        ):
            assert field not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/v1/search?q=",
        "/v1/search",
        "/v1/search?q=x&limit=101",
        "/v1/search?q=x&kind=bad",
        "/v1/transcriptions?limit=0",
        "/v1/meetings?since=bad",
        "/v1/meetings/m_a/segments?start_s=-1",
        "/v1/meetings/m_a/segments?end_s=nan",
        "/v1/meetings/m_a/segments?end_s=inf",
    ],
)
def test_parameter_bounds(client, path):
    assert client.get(path).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/v1/search?q=%20%20",
        "/v1/meetings?since=2026-09-01T00:00:00",
        "/v1/meetings?since=2026-09-02T00:00:00Z&before=2026-09-01T00:00:00Z",
        "/v1/meetings/m_a/segments?start_s=10&end_s=5",
    ],
)
def test_invalid_semantic_parameters(client, path):
    assert client.get(path).status_code == 400


def test_openapi_documents_auth_and_stable_operations(client):
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HistoryToken"]["scheme"] == "bearer"
    for path in schema["paths"].values():
        assert set(path) == {"get"}
        assert path["get"]["security"] == [{"HistoryToken": []}]
    assert schema["paths"]["/v1/search"]["get"]["operationId"] == "search_history"
    assert client.delete("/v1/transcriptions/h_a").status_code == 405
    assert client.get("/v1/transcriptions/h_a").status_code == 200


def test_storage_is_read_only_and_does_not_create_or_migrate(database, tmp_path):
    store = HistoryStore(database[1])
    try:
        with (
            store.engine.connect() as conn,
            pytest.raises(OperationalError, match="readonly"),
        ):
            conn.execute(text("DELETE FROM transcription_history"))
    finally:
        store.close()
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="does not exist"):
        HistoryStore(missing)
    assert not missing.exists()
    with database[0].engine.begin() as conn:
        conn.execute(text("UPDATE schema_version SET version=1"))
    with pytest.raises(ValueError, match="incompatible"):
        HistoryStore(database[1])
    with database[0].engine.connect() as conn:
        assert conn.execute(text("SELECT version FROM schema_version")).scalar() == 1


def test_database_errors_do_not_leak_sql(client, monkeypatch):
    def fail(self):
        raise OperationalError(
            "secret SQL", {"path": "private-marker"}, Exception("private-marker")
        )

    monkeypatch.setattr(HistoryStore, "check", fail)
    response = client.get("/v1/status")
    assert response.status_code == 503
    assert "private-marker" not in response.text and "secret SQL" not in response.text


@pytest.mark.parametrize("token", ["", "short", "a" * 32 + " ", "é" * 32])
def test_invalid_startup_tokens(database, token):
    with pytest.raises(ValueError, match="TOKEN"):
        create_app(database[1], token)


def test_cli_is_headless_and_requires_opt_in(tmp_path):
    env = {
        **os.environ,
        "OPENWHISPER_DATA_DIR": str(tmp_path),
        "OPENWHISPER_API_TOKEN": "",
    }
    code = """
import runpy, sys
sys.argv = ['main.py', '--api', '--help']
try:
    runpy.run_path('main.py', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
assert not any(n.startswith(('PyQt6', 'sounddevice', 'faster_whisper')) for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "main.py", "--api"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "OPENWHISPER_API_TOKEN" in result.stderr
    assert not (tmp_path / "openwhisper.db").exists()


def test_cli_launch_uses_read_only_service_without_desktop_imports(database):
    code = """
import runpy, sys, uvicorn
database = sys.argv[1]
called = []
def serve(app, **options):
    assert options['host'] == '127.0.0.1'
    assert options['port'] == 8767
    assert options['access_log'] is False
    assert options['proxy_headers'] is False
    assert '/v1/search' in app.openapi()['paths']
    called.append(True)
uvicorn.run = serve
sys.argv = ['main.py', '--api', '--database', database, '--port', '8767']
try:
    runpy.run_path('main.py', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
assert called == [True]
assert not any(n.startswith(('PyQt6', 'sounddevice', 'faster_whisper')) for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(database[1])],
        cwd=ROOT,
        env={**os.environ, "OPENWHISPER_API_TOKEN": TOKEN},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
