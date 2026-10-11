"""MCP Events from another computer: no reaching into this one, no outliving access.

A subscription remembers whether it was made on this computer or by a
Tailscale peer. A peer's subscriptions are delivered only while Allow agents
over Tailscale is on, and a peer can't point deliveries at this computer (its
Tailscale address included) or its local network, by address or by a name
that resolves there. Each request goes to the address that was checked, and
a peer learns only that a verification or delivery failed, never how.
"""

import datetime
import ipaddress
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from config import config
from services.agent_mcp import events_ext
from services.agent_mcp.app import create_app
from services.automation import catalog, events, signing
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_mcp_events import (
    TOKEN,
    Receiver,
    error_of,
    result_of,
    rpc,
    subscribe_params,
)

HOST = "100.82.22.3"
PEER = ("100.65.148.53", 1234)
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setattr(events_ext, "_registry_instance", None)
    replacement = events.EventBus()
    previous = events.set_bus(replacement)
    yield replacement
    events_ext.stop_delivery()
    events.set_bus(previous)
    replacement.close()


@pytest.fixture
def settings():
    return InMemorySettings({
        SettingsKey.MCP_ENABLED: True,
        SettingsKey.MCP_EVENTS_ENABLED: True,
        SettingsKey.MCP_TAILSCALE_ENABLED: True,
    })


@pytest.fixture
def clients(tmp_path, settings, bus):
    """A Tailscale peer's and a local agent's view of the listener."""
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()

    def app():
        return create_app(path, TOKEN, enabled=lambda: True, settings=settings, allowed_hosts=(HOST,))

    with TestClient(app(), base_url=f"http://{HOST}", client=PEER) as peer, \
            TestClient(app(), base_url="http://127.0.0.1", client=LOCAL) as local:
        events_ext.start_delivery(settings=settings)
        yield peer, local


@pytest.mark.parametrize(
    "url",
    [
        "loopback",  # the receiver on 127.0.0.1
        "https://127.0.0.1:8443/hook",
        "https://localhost/hook",
        "https://[::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://0.0.0.0/hook",
        "https://192.168.1.20/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest",
        "https://printer.local/hook",
    ],
)
def test_a_peer_cannot_point_deliveries_at_this_computer_or_its_network(clients, receiver, url):
    peer, _ = clients
    url = receiver.url() if url == "loopback" else url
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, url, signing.new_secret()
    )))
    assert error["code"] == events_ext.INVALID_PARAMS
    assert "another computer" in error["message"]
    assert receiver.requests == []
    assert events_ext.subscription_count() == 0


def test_handshake_era_clients_are_told_apart_the_same_way(clients, receiver):
    peer, local = clients
    params = subscribe_params(catalog.HISTORY_SAVED, receiver.url(), signing.new_secret())
    assert error_of(rpc(peer, "events/subscribe", params, modern=False))["code"] == (
        events_ext.INVALID_PARAMS
    )
    assert result_of(rpc(local, "events/subscribe", params, modern=False))["id"].startswith("sub_")


def test_a_name_that_resolves_to_this_computer_counts_as_this_computer(clients, monkeypatch):
    peer, _ = clients
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, "https://rebind.example/hook", signing.new_secret()
    )))
    assert error["code"] == events_ext.INVALID_PARAMS


def test_this_computer_may_still_subscribe_its_own_receivers(clients, receiver):
    _, local = clients
    granted = result_of(rpc(local, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, receiver.url(), signing.new_secret()
    )))
    saved = json.loads(Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE).read_text(encoding="utf-8"))
    assert [record["origin"] for record in saved["subscriptions"]] == ["local"]
    assert granted["deliveryStatus"]["active"] is True


def peer_subscription(clients, receiver, monkeypatch, secret):
    """A subscription a peer made; its callback stands in for one on the internet."""
    peer, _ = clients
    url = receiver.url()
    monkeypatch.setattr(
        events_ext, "_destination",
        lambda target: (events_ext.PUBLIC, "127.0.0.1") if target == url else (events_ext.PRIVATE, None),
    )
    granted = result_of(rpc(peer, "events/subscribe", subscribe_params(catalog.HISTORY_SAVED, url, secret)))
    return granted


def handed_out(bus):
    """Wait until the bus has handed every event published so far to its subscribers.

    One thread hands events out in order, so once a later marker arrives,
    MCP Events has seen (and queued or dropped) everything before it.
    """
    arrived = threading.Event()
    marker = bus.subscribe(lambda event: arrived.set(), types=["test.marker"])
    bus.publish("test.marker", {})
    assert arrived.wait(5)
    marker.close()


def saved(n):
    return {"job_id": f"j{n}", "record_id": str(n), "kind": "dictation"}


def test_a_peers_subscription_is_delivered_only_while_tailscale_access_is_on(
    clients, receiver, settings, bus, monkeypatch
):
    secret = signing.new_secret()
    granted = peer_subscription(clients, receiver, monkeypatch, secret)
    records = json.loads(Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE).read_text(encoding="utf-8"))
    assert [record["origin"] for record in records["subscriptions"]] == ["tailscale"]
    assert granted["deliveryStatus"]["active"] is True

    first = bus.publish(catalog.HISTORY_SAVED, saved(1))
    [(_, headers, body, payload)] = receiver.wait_for_deliveries(1)
    assert payload["eventId"] == first.id and signing.verify(secret, headers, body)

    # The user turned Tailscale access off: the peer's subscription goes quiet.
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    bus.publish(catalog.HISTORY_SAVED, saved(2))
    handed_out(bus)
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = True
    third = bus.publish(catalog.HISTORY_SAVED, saved(3))
    deliveries = receiver.wait_for_deliveries(2)
    assert [d[3]["eventId"] for d in deliveries] == [first.id, third.id]


class HeldSends:
    """Counts queued deliveries; workers wait for ``release()`` before each try."""

    def __init__(self, deliver, push):
        self._deliver = deliver
        self._push = push
        self._gate = threading.Event()
        self._cond = threading.Condition()
        self.queued = 0
        self.done = 0

    def push(self, deliverer, *args, **kwargs):
        accepted = self._push(deliverer, *args, **kwargs)
        with self._cond:
            self.queued += bool(accepted)
        return accepted

    def deliver(self, job, client=None):
        try:
            assert self._gate.wait(10)
            return self._deliver(job, client)
        finally:
            with self._cond:
                self.done += 1
                self._cond.notify_all()

    def release(self):
        """Let every try go, and wait until each one queued so far has finished."""
        self._gate.set()
        with self._cond:
            queued = self.queued
            assert self._cond.wait_for(lambda: self.done >= queued, 10)


@pytest.fixture
def held_sends(settings, monkeypatch):
    held = HeldSends(events_ext._deliver, events_ext._Deliverer.push)
    monkeypatch.setattr(events_ext, "_deliver", held.deliver)
    monkeypatch.setattr(events_ext._Deliverer, "push", lambda self, *a, **k: held.push(self, *a, **k))
    # A fresh deliverer, so its workers call the held version.
    events_ext.stop_delivery()
    events_ext.start_delivery(settings=settings)
    return held


def test_an_event_from_while_access_was_off_stays_unsent_when_access_returns_first(
    clients, receiver, settings, bus, monkeypatch, held_sends
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    bus.publish(catalog.HISTORY_SAVED, saved(1))
    handed_out(bus)
    # Access is back before any worker got to send.
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = True
    second = bus.publish(catalog.HISTORY_SAVED, saved(2))
    handed_out(bus)
    held_sends.release()
    assert held_sends.queued == 1
    assert [d[3]["eventId"] for d in receiver.wait_for_deliveries(1)] == [second.id]


def test_access_turned_off_before_the_send_still_stops_it(
    clients, receiver, settings, bus, monkeypatch, held_sends
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    bus.publish(catalog.HISTORY_SAVED, saved(1))
    handed_out(bus)
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    held_sends.release()
    assert held_sends.queued == 1
    assert receiver.deliveries() == []


def test_a_local_subscription_does_not_need_tailscale_access(clients, receiver, settings, bus):
    _, local = clients
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    result_of(rpc(local, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, receiver.url(), signing.new_secret()
    )))
    published = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "dictation"})
    [(_, _, _, payload)] = receiver.wait_for_deliveries(1)
    assert payload["eventId"] == published.id


def test_a_peers_callback_that_later_resolves_here_is_not_delivered(
    clients, receiver, bus, monkeypatch
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    monkeypatch.setattr(events_ext, "_destination", lambda target: (events_ext.PRIVATE, None))
    bus.publish(catalog.HISTORY_SAVED, {"job_id": "j1", "record_id": "1", "kind": "dictation"})
    registry = events_ext._registry()
    [subscription] = registry.active()
    deadline = time.monotonic() + 5
    while "lastError" not in registry.delivery_status(subscription.id, True):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert registry.delivery_status(subscription.id, True)["lastError"] == "address_refused"
    assert receiver.deliveries() == []


def test_saved_subscriptions_without_an_origin_count_as_a_peers():
    record = {
        "id": "sub_" + "0" * 32, "name": catalog.HISTORY_SAVED, "arguments": {},
        "url": "https://example.com/hook", "refresh_before": "2099-01-01T00:00:00Z",
        "created_at": "",
    }
    assert events_ext.EventSubscription.from_record(record).origin == "tailscale"
    assert events_ext.EventSubscription.from_record({**record, "origin": "local"}).origin == "local"


# -- one lookup per try, and nothing about ports -------------------------------

#: Stands for a server on the internet; the fake network never connects to it.
INTERNET = "93.184.215.14"


def is_address(host):
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


class FakeNetwork:
    """Name lookups answered from ``names``; connections only to this computer.

    ``names`` maps a host name to its answers, one per lookup, the last one
    repeating; any other name is unknown. Each connection is recorded as
    (address, port), and one to anything but loopback is refused unsent.
    """

    def __init__(self, monkeypatch):
        self.names = {}
        self.lookups = []
        self.connections = []
        real_lookup, real_connect = socket.getaddrinfo, socket.create_connection

        def getaddrinfo(host, port, *args, **kwargs):
            host = host.decode("ascii") if isinstance(host, bytes) else host
            if host == "localhost" or is_address(host):
                return real_lookup(host, port, *args, **kwargs)
            self.lookups.append(host)
            answers = self.names.get(host)
            if not answers:
                raise socket.gaierror(socket.EAI_NONAME, "unknown name")
            address = answers.pop(0) if len(answers) > 1 else answers[0]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        def create_connection(address, *args, **kwargs):
            host, port = address[:2]
            target = getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][4][0]
            self.connections.append((target, port))
            if not ipaddress.ip_address(target).is_loopback:
                raise ConnectionRefusedError("the test network reaches only this computer")
            return real_connect((target, port), *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", create_connection)


def test_a_peers_callback_is_verified_at_the_address_that_was_checked(clients, receiver, monkeypatch):
    peer, _ = clients
    network = FakeNetwork(monkeypatch)
    port = receiver.server.server_port
    # The first answer passes the check; a second lookup would lead here.
    network.names["rebind.example"] = [INTERNET, "127.0.0.1"]
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, f"https://rebind.example:{port}/hook", signing.new_secret()
    )))
    assert network.lookups == ["rebind.example"]
    assert network.connections == [(INTERNET, port)]
    # Not "connection_refused": the peer can't tell that from a TLS error or
    # a timeout, so it can't map which ports answer.
    assert error["code"] == events_ext.CALLBACK_ENDPOINT_ERROR
    assert error["data"] == {"reason": "challenge_failed"}
    assert receiver.requests == []


def test_each_try_to_deliver_to_a_peer_goes_to_the_address_it_checked(
    bus, settings, receiver, monkeypatch
):
    network = FakeNetwork(monkeypatch)
    monkeypatch.setattr(events_ext, "RETRY_DELAYS_S", ())
    port = receiver.server.server_port
    registry = events_ext._registry()
    subscription = events_ext.EventSubscription(
        "sub_" + "2" * 32, catalog.HISTORY_SAVED, {}, f"https://rebind.example:{port}/hook",
        int(time.time() + 3600), "", events_ext.ORIGIN_TAILSCALE,
    )
    registry.upsert(subscription, signing.new_secret())
    events_ext.start_delivery(settings=settings)
    network.names["rebind.example"] = [INTERNET, "127.0.0.1"]

    bus.publish(catalog.HISTORY_SAVED, saved(1))
    deadline = time.monotonic() + 5
    while "lastError" not in registry.delivery_status(subscription.id, True):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert network.connections == [(INTERNET, port)]
    assert registry.delivery_status(subscription.id, True)["lastError"] == "delivery_failed"
    assert receiver.requests == []


@pytest.mark.parametrize("address", [HOST, "100.100.100.100"])
def test_this_computers_own_tailscale_addresses_count_as_this_computer(
    clients, monkeypatch, address
):
    """Its Tailscale address, and the service address its own Tailscale answers."""
    peer, _ = clients
    network = FakeNetwork(monkeypatch)
    real = events_ext._is_this_computer
    # This computer holds HOST, the listener's Tailscale address.
    monkeypatch.setattr(
        events_ext, "_is_this_computer", lambda found: str(found) == HOST or real(found)
    )
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, f"https://{address}:8443/hook", signing.new_secret()
    )))
    assert error["code"] == events_ext.INVALID_PARAMS
    assert "another computer" in error["message"]
    assert network.connections == []


def test_another_computer_on_the_tailnet_is_the_peers_own_business(clients, monkeypatch):
    peer, _ = clients
    network = FakeNetwork(monkeypatch)
    # Only HOST is this computer, whatever Tailscale address the test machine has.
    monkeypatch.setattr(events_ext, "_is_this_computer", lambda found: str(found) == HOST)
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, f"https://{PEER[0]}:8443/hook", signing.new_secret()
    )))
    assert network.connections == [(PEER[0], 8443)]
    assert error["data"] == {"reason": "challenge_failed"}


def test_this_computers_addresses_are_the_ones_it_can_bind():
    from services import lan_address

    def mine(text):
        return events_ext._is_this_computer(ipaddress.ip_address(text))

    assert mine("127.0.0.1")
    assert mine("100.100.100.100")
    assert not mine("192.0.2.1")  # a documentation address, assigned nowhere
    assert not mine(INTERNET)
    route = lan_address._route_address()  # sends nothing
    if route is not None:
        assert mine(route)


def certificates(folder, name):
    """A test CA and a certificate it issued for ``name``: (ca, certificate, key) paths."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenWhisper test CA")])

    def builder(subject, public_key):
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_name)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
        )

    ca = (
        builder(ca_name, ca_key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]), key.public_key())
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = [folder / "ca.pem", folder / "cert.pem", folder / "key.pem"]
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return [str(path) for path in paths]


class TlsReceiver:
    """An https endpoint on 127.0.0.1 with a certificate for ``name``.

    Records the TLS server name and Host header of each request, and keeps
    connections open between requests (HTTP/1.1).
    """

    def __init__(self, folder, name):
        self.ca, certificate, key = certificates(folder, name)
        self.server_names = []
        self.hosts = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        context.sni_callback = lambda sock, server_name, ctx: self.server_names.append(server_name)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                receiver.hosts.append(self.headers.get("Host"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def client(self):
        """An HTTP client that trusts this receiver's CA instead of the system's."""
        context = ssl.create_default_context(cafile=self.ca)
        return httpx.Client(verify=context, timeout=5.0, follow_redirects=False, trust_env=False)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_pinned_request_keeps_the_host_name_for_tls_and_the_host_header(tmp_path, monkeypatch):
    network = FakeNetwork(monkeypatch)  # no name has an answer: none may be looked up
    receiver = TlsReceiver(tmp_path, "callback.example")
    monkeypatch.setattr(events_ext, "_new_client", lambda timeout_s=5.0: receiver.client())
    port = receiver.port
    headers = {"content-type": "application/json"}
    try:
        reply = events_ext._post(
            f"https://callback.example:{port}/hook", b"{}", headers, 5.0, connect_to="127.0.0.1"
        )
        # The certificate is still checked against the URL's name, also when
        # the client already holds a connection to that address under another.
        with receiver.client() as pooled:
            assert pooled.post(
                f"https://127.0.0.1:{port}/hook", content=b"{}",
                headers={"host": f"callback.example:{port}"},
                extensions={"sni_hostname": "callback.example"},
            ).status_code == 200
            wrong = events_ext._post(
                f"https://other.example:{port}/hook", b"{}", headers, 5.0,
                client=pooled, connect_to="127.0.0.1",
            )
    finally:
        receiver.close()
    assert reply.ok
    assert receiver.server_names[0] == "callback.example"
    assert receiver.hosts[0] == f"callback.example:{port}"
    assert wrong.category == "tls_error"
    assert len(receiver.hosts) == 2  # the wrong name never got a request through
    assert network.lookups == []
