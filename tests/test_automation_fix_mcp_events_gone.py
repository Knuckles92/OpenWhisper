"""A callback answering 410 Gone loses that delivery, not the subscription.

Like 413 and other client errors, 410 is not retried; the subscription, its
secret and later deliveries stay, and deliveryStatus shows the error. Only
events/unsubscribe, expiry or Clear end a subscription.
"""

import pytest
from fastapi.testclient import TestClient

from services import credentials
from services.agent_mcp import events_ext
from services.agent_mcp.app import create_app
from services.automation import catalog, events, signing
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_mcp_events import (
    TOKEN,
    Receiver,
    result_of,
    rpc,
    status_eventually,
    subscribe_params,
)


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


@pytest.fixture
def bus():
    replacement = events.EventBus()
    previous = events.set_bus(replacement)
    yield replacement
    events_ext.stop_delivery()
    events.set_bus(previous)
    replacement.close()


@pytest.fixture
def client(tmp_path, bus):
    settings = InMemorySettings({SettingsKey.MCP_ENABLED: True, SettingsKey.MCP_EVENTS_ENABLED: True})
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        events_ext.start_delivery(settings=settings)
        yield test_client


def test_gone_is_an_error_on_that_delivery_only(client, bus, receiver, monkeypatch):
    monkeypatch.setattr(events_ext, "RETRY_DELAYS_S", (0.05, 0.05, 0.05))
    secret = signing.new_secret()
    params = subscribe_params(catalog.HISTORY_SAVED, receiver.url(), secret)
    sub_id = result_of(rpc(client, "events/subscribe", params))["id"]

    receiver.statuses = [410]
    bus.publish(catalog.HISTORY_SAVED, {"job_id": "j1", "record_id": "1", "kind": "dictation"})
    receiver.wait_for_deliveries(1)
    status = status_eventually(client, params, lambda status: "lastError" in status)
    assert status["lastError"] == "http_4xx"

    assert events_ext.subscription_count() == 1
    assert credentials.store().get(events_ext.secret_name(sub_id)) == secret
    later = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j2", "record_id": "2", "kind": "dictation"})
    deliveries = receiver.wait_for_deliveries(2)
    # Not retried: the second delivery is the next event.
    assert deliveries[1][3]["eventId"] == later.id
    assert signing.verify(secret, deliveries[1][1], deliveries[1][2])
