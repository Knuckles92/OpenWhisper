"""Unknown /v1 paths and wrong methods get the API's JSON errors, not the MCP app's text.

The MCP app is mounted at the root, after the API routes, so anything the
API didn't match used to fall through to it and come back as text/plain.
"""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.database import DatabaseManager

TOKEN = "not-found-token-" + "n" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/v1/nope"), ("POST", "/v1/nope/run"), ("GET", "/v1"), ("PUT", "/v1/transcriptions/x/y/z")],
)
def test_unknown_paths_are_json_404s(client, method, path):
    response = client.request(method, path, headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "method, path, allowed",
    [
        ("GET", "/v1/actions/a1/run", {"POST"}),
        ("POST", "/v1/actions", {"GET"}),
        ("DELETE", "/v1/live/state", {"GET"}),
        ("PUT", "/v1/filters/l1", {"DELETE"}),
        ("POST", "/v1/status", {"GET"}),
    ],
)
def test_known_paths_with_the_wrong_method_are_json_405s(client, method, path, allowed):
    response = client.request(method, path, headers=AUTH)
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert set(response.headers["allow"].split(", ")) == allowed


def test_the_token_is_still_checked_first(client):
    response = client.get("/v1/nope")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_mcp_and_real_routes_are_untouched(client):
    assert client.get("/v1/status", headers=AUTH).status_code == 200
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }},
        headers={**AUTH, "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "OpenWhisper"
