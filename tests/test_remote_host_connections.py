"""Paired admission stays available when anonymous WebSockets idle over real TLS."""
from __future__ import annotations

# Imported pytest fixtures intentionally share their names with test arguments.
# ruff: noqa: F811
import json
import logging
import threading
import time

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from services.remote_asr import client, protocol
from services.remote_asr.client import (
    RemoteConnection,
    RemoteEngineError,
)
from services.remote_asr.host import MAX_PENDING_HANDSHAKES, DeviceRegistry, SpeechHost
from services.remote_asr.tls import (
    client_context,
    ensure_host_identity,
    transport_logger,
)
from tests.test_remote_engine import (  # noqa: F401
    FakeEngine,
    ListStore,
    _connect,
    _pair,
    _wait_for,
    engine,
    host,
    identity,
    no_real_tailscale,
    store,
)


def _hello(ws, token, **extra):
    ws.send(json.dumps({"type": "hello", "protocol": protocol.PROTOCOL_VERSION,
                        "token": token, **extra}))
    return json.loads(ws.recv(timeout=2))


def test_anonymous_handshakes_cannot_take_paired_connection_slots(host):
    pairing = _pair(host)
    sockets = []
    connection = None
    try:
        sockets = [client._open("127.0.0.1", host.port, 2)
                   for _ in range(MAX_PENDING_HANDSHAKES)]
        assert _wait_for(lambda: host._handshake_slots._value == 0)
        assert host._connection_slots._value == 12
        connection = _connect(host, pairing)
        assert connection.request("transcribe", audio=np.zeros(160, dtype=np.float32))["text"]
        excess = client._open("127.0.0.1", host.port, 2)
        sockets.append(excess)
        assert json.loads(excess.recv(timeout=2))["code"] == "busy"
        assert host._connection_slots._value == 11
    finally:
        if connection is not None:
            connection.close()
        for ws in sockets:
            ws.close()
    assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_legacy_hello_promotes_without_holding_anonymous_slot(host):
    pairing = _pair(host)
    ws = client._open("127.0.0.1", host.port, 2)
    try:
        assert _hello(ws, pairing.token)["type"] == "ready"
        assert host._handshake_slots._value == MAX_PENDING_HANDSHAKES
        assert host._connection_slots._value == 11
    finally:
        ws.close()
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_history_and_speech_still_count_as_two_authenticated_connections(host):
    pairing = _pair(host)
    speech = _connect(host, pairing)
    history = RemoteConnection("127.0.0.1", host.port, pairing.token,
                               pairing.fingerprint, history_enabled=True)
    try:
        assert history.connect()["capabilities"]["client_history"]
        assert host._connection_slots._value == 10
        assert host._handshake_slots._value == MAX_PENDING_HANDSHAKES
    finally:
        speech.close()
        history.close()
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_wrong_pin_sends_no_http_upgrade_or_credentials(tmp_path, monkeypatch):
    received = []
    authenticated = []
    identity = ensure_host_identity(str(tmp_path / "stranger"))
    registry = DeviceRegistry(lambda: [], lambda _: None)
    monkeypatch.setattr(registry, "authenticate", lambda value: authenticated.append(value))
    stranger = SpeechHost(engine_provider=FakeEngine, registry=registry, identity=identity)
    check = stranger._check_path

    def observe(connection, request):
        received.append(request)
        return check(connection, request)

    monkeypatch.setattr(stranger, "_check_path", observe)
    stranger.start(port=0, bind="127.0.0.1")
    try:
        with pytest.raises(RemoteEngineError, match="different certificate"):
            client._open("127.0.0.1", stranger.port, 2,
                         token="must-not-reach-stranger", fingerprint="AB" * 32)
        time.sleep(0.1)
        assert received == []
        assert authenticated == []
    finally:
        stranger.stop()


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer invalid-token"},
    {"Authorization": "Basic invalid-token"},
    [("Authorization", "Bearer invalid"), ("Authorization", "Bearer duplicate")],
])
def test_invalid_upgrade_auth_never_enters_either_pool(host, headers):
    with pytest.raises(InvalidStatus) as rejected:
        connect(client._uri("127.0.0.1", host.port), ssl=client_context(),
                additional_headers=headers, logger=transport_logger())
    assert rejected.value.response.status_code == 401
    assert host._connection_slots._value == 12
    assert host._handshake_slots._value == MAX_PENDING_HANDSHAKES


def test_hello_cannot_switch_authenticated_upgrade_identity(host):
    first = _pair(host)
    second = _pair(host)
    ws = client._open("127.0.0.1", host.port, 2, token=first.token,
                      fingerprint=first.fingerprint)
    try:
        with pytest.raises(ConnectionClosed) as rejected:
            _hello(ws, second.token)
        assert rejected.value.rcvd.code == protocol.CLOSE_UNAUTHORIZED
        assert host.connected_clients() == []
    finally:
        ws.close()
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_revoked_between_upgrade_and_hello_is_rejected(host):
    pairing = _pair(host)
    ws = client._open("127.0.0.1", host.port, 2, token=pairing.token,
                      fingerprint=pairing.fingerprint)
    try:
        assert host.registry.remove(pairing.device_id)
        assert _hello(ws, pairing.token)["code"] == "unauthorized"
        assert host.connected_clients() == []
    finally:
        ws.close()
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_transport_debug_does_not_log_tokens_or_transcripts(host, caplog):
    caplog.set_level(logging.DEBUG, logger="websockets.remote_engine")
    pairing = _pair(host)
    connection = _connect(host, pairing)
    try:
        result = connection.request("transcribe", audio=np.zeros(160, dtype=np.float32))
        assert result["text"] == "heard 160"
    finally:
        connection.close()
    assert pairing.token not in caplog.text
    assert "Authorization:" not in caplog.text
    assert "heard 160" not in caplog.text


def test_anonymous_timeout_releases_handshake_without_reserving_client(host, monkeypatch):
    from services.remote_asr import host as host_module

    monkeypatch.setattr(host_module, "HANDSHAKE_TIMEOUT_S", 0.1)
    ws = client._open("127.0.0.1", host.port, 2)
    try:
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=2)
        assert closed.value.rcvd.code == protocol.CLOSE_TIMEOUT
    finally:
        ws.close()
    assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
    assert host._connection_slots._value == 12


def test_revoked_upgrade_does_not_search_for_another_host(host, monkeypatch):
    pairing = _pair(host)
    assert host.remove_device(pairing.device_id)
    connection = RemoteConnection("127.0.0.1", host.port, pairing.token, pairing.fingerprint)
    searched = []
    monkeypatch.setattr(connection, "_search_network", lambda _: searched.append(True))
    with pytest.raises(client.RemoteUnauthorized, match="isn't paired"):
        connection.connect()
    assert searched == []


def test_upgrade_registry_error_does_not_expose_credentials(host, monkeypatch, caplog):
    pairing = _pair(host)

    def unavailable(_):
        raise RuntimeError(pairing.token)

    monkeypatch.setattr(host.registry, "authenticate", unavailable)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RemoteEngineError, match="HTTP 503"):
        client._open("127.0.0.1", host.port, 2, token=pairing.token,
                     fingerprint=pairing.fingerprint)
    assert pairing.token not in caplog.text
    assert host._connection_slots._value == 12
    assert host._handshake_slots._value == MAX_PENDING_HANDSHAKES


def test_repeated_wrong_pins_leave_no_server_connections(host):
    baseline = threading.active_count()
    for _ in range(20):
        with pytest.raises(RemoteEngineError, match="different certificate"):
            client._open("127.0.0.1", host.port, 2,
                         token="wrong-pin-secret", fingerprint="AB" * 32)
    assert _wait_for(lambda: threading.active_count() <= baseline, timeout=5)
    assert host.connected_clients() == []
    assert host._connection_slots._value == 12
    assert host._handshake_slots._value == MAX_PENDING_HANDSHAKES


def test_credentials_cannot_be_sent_without_a_pin(monkeypatch):
    def unexpected_connect(*args, **kwargs):
        pytest.fail("Transport must not open without a certificate pin")

    monkeypatch.setattr("websockets.sync.client.connect", unexpected_connect)
    with pytest.raises(client._WrongPlace):
        client._open("127.0.0.1", 1, 1, token="synthetic-test-token")


@pytest.mark.parametrize("route", ["probe", "tailscale"])
def test_anonymous_budget_covers_blocked_routing(host, monkeypatch, route):
    pairing = _pair(host)
    release = threading.Event()
    entered = []
    lock = threading.Lock()

    def blocked(*args):
        with lock:
            entered.append(True)
        assert release.wait(5)
        return None

    if route == "probe":
        monkeypatch.setattr(host, "_probe", blocked)
        message = {"type": "probe"}
    else:
        monkeypatch.setattr(host, "_tailscale_owner", lambda: "synthetic-owner")
        monkeypatch.setattr("services.remote_asr.host.tailscale.own_device", blocked)
        message = {"type": "pair", "tailscale": True,
                   "protocol": protocol.PROTOCOL_VERSION}
    sockets = []
    connection = None
    try:
        for _ in range(MAX_PENDING_HANDSHAKES):
            ws = client._open("127.0.0.1", host.port, 2)
            sockets.append(ws)
            ws.send(json.dumps(message))
        assert _wait_for(lambda: len(entered) == MAX_PENDING_HANDSHAKES)
        assert host._handshake_slots._value == 0
        for _ in range(8):
            excess = client._open("127.0.0.1", host.port, 2)
            sockets.append(excess)
            assert json.loads(excess.recv(timeout=2))["code"] == "busy"
        assert len(entered) == MAX_PENDING_HANDSHAKES
        connection = _connect(host, pairing)
        assert connection.request("transcribe", audio=np.zeros(160, dtype=np.float32))["text"]
    finally:
        release.set()
        if connection:
            connection.close()
        for ws in sockets:
            ws.close()
    assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
    assert _wait_for(lambda: host._connection_slots._value == 12)


@pytest.mark.parametrize("stage", ["authenticate", "serve"])
def test_legacy_admission_exceptions_release_each_reservation_once(host, monkeypatch, stage):
    pairing = _pair(host)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic routing failure")

    if stage == "authenticate":
        monkeypatch.setattr(host.registry, "authenticate", fail)
    else:
        monkeypatch.setattr(host, "_serve_authenticated", fail)
    ws = client._open("127.0.0.1", host.port, 2)
    try:
        with pytest.raises(ConnectionClosed):
            _hello(ws, pairing.token)
    finally:
        ws.close()
    assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
    assert _wait_for(lambda: host._connection_slots._value == 12)


def test_legacy_capacity_refusal_returns_anonymous_reservation(host, monkeypatch):
    pairing = _pair(host)
    monkeypatch.setattr(host, "_connection_slots", threading.BoundedSemaphore(1))
    connection = _connect(host, pairing)
    ws = client._open("127.0.0.1", host.port, 2)
    try:
        assert _hello(ws, pairing.token)["code"] == "busy"
        assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
        assert host._connection_slots._value == 0
    finally:
        ws.close()
        connection.close()
    assert _wait_for(lambda: host._connection_slots._value == 1)


@pytest.mark.parametrize("message", ["not json", "[]", b"binary hello"])
def test_invalid_first_message_returns_anonymous_reservation(host, message):
    ws = client._open("127.0.0.1", host.port, 2)
    try:
        ws.send(message)
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)
    finally:
        ws.close()
    assert _wait_for(lambda: host._handshake_slots._value == MAX_PENDING_HANDSHAKES)
    assert host._connection_slots._value == 12
