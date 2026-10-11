"""Only a program on this computer can lease a text filter.

A leased filter is an address on this computer that OpenWhisper sends every
dictation to. A Tailscale peer must not be able to point that at a service
listening here.
"""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import api, filters, permissions
from services.database import DatabaseManager
from tests.test_automation_api_routes import FakeSettings

TOKEN = "remote-filters-token-" + "f" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOST = "100.82.22.3"


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    settings = FakeSettings(**{permissions.TEXT_FILTERS: True})
    filters.drop_all_leases()
    yield create_app(path, TOKEN, enabled=lambda: True, settings=settings, allowed_hosts=(HOST,))
    filters.drop_all_leases()


def test_a_tailscale_peer_cannot_lease_a_filter(app):
    with TestClient(app, base_url=f"http://{HOST}", client=("100.65.148.53", 1234)) as peer:
        response = peer.post(
            "/v1/filters", json={"url": "http://127.0.0.1:9/filter", "stage": "after_cleanup"},
            headers=AUTH,
        )
    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "forbidden",
        "message": "Only a program on this computer can lease a text filter.",
    }
    assert filters.leases() == []


def test_a_program_on_this_computer_still_can(app):
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local:
        response = local.post(
            "/v1/filters", json={"url": "http://127.0.0.1:9/filter", "stage": "after_cleanup"},
            headers=AUTH,
        )
        assert response.status_code == 201
        assert [lease.url for lease in filters.leases()] == ["http://127.0.0.1:9/filter"]


@pytest.mark.parametrize(
    "host, local",
    [
        ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("::ffff:127.0.0.1", True),
        ("100.65.148.53", False), ("192.168.1.10", False), ("testclient", False), ("", False),
    ],
)
def test_who_counts_as_this_computer(host, local):
    class Request:
        client = type("Client", (), {"host": host})() if host else None

    assert api.peer_is_local(Request()) is local
