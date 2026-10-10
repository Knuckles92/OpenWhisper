"""Pairing without Tailscale: discovery, approval on the host, following a moved host.

Hosts and clients talk over real UDP and TLS sockets on 127.0.0.1; the
conftest keeps searches off the real network, so each test names where to
look.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from services.remote_asr import discovery, protocol, reachability, tailscale
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import (
    PairingCanceled,
    PairingResult,
    RemoteConnection,
    RemoteEngineError,
    RemoteUnreachable,
    pair_with_host,
    probe_host,
    request_pairing,
)
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity
from tests.test_remote_engine import FakeEngine, ListStore, _wait_for


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """Keep every test off this computer's real tailnet."""
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def store():
    return ListStore()


@pytest.fixture
def lan_host(engine, store, tmp_path):
    """A host sharing on 127.0.0.1 that answers discovery on a free UDP port."""
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=ensure_host_identity(str(tmp_path / "host")),
        host_name="devbox",
        addresses=lambda: ["192.0.2.10"],
    )
    speech_host.start(port=0, bind="127.0.0.1", discovery_port=0)
    assert speech_host.discovery.running
    yield speech_host
    speech_host.stop()


def _target(host):
    return ("127.0.0.1", host.discovery.port)


def _request_in_background(host, canceled=None):
    """Start a pairing request; returns (thread, outcome dict)."""
    outcome = {"codes": []}

    def run():
        try:
            outcome["result"] = request_pairing(
                "127.0.0.1", host.port, "laptop",
                on_code=outcome["codes"].append, canceled=canceled,
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


# ---- the comparison number ----

def test_both_sides_get_the_same_number_only_through_the_same_certificate():
    host_nonce, client_nonce = b"h" * 32, b"c" * 32
    sas = protocol.pairing_sas(host_nonce, client_nonce, "AB" * 32)
    assert len(sas) == 6 and sas.isdigit()
    assert sas == protocol.pairing_sas(host_nonce, client_nonce, "ab" * 32)
    # A relay in the middle presents its own certificate: the numbers differ.
    assert sas != protocol.pairing_sas(host_nonce, client_nonce, "CD" * 32)
    assert sas != protocol.pairing_sas(host_nonce, b"d" * 32, "AB" * 32)
    assert protocol.format_sas("123456") == "123 456"
    assert protocol.pairing_commitment(host_nonce) != protocol.pairing_commitment(b"x" * 32)


# ---- discovery ----

def test_queries_are_padded_and_answers_never_outgrow_them():
    nonce = b"n" * discovery.NONCE_BYTES
    query = discovery.make_query(nonce)
    assert len(query) == discovery.QUERY_BYTES
    assert discovery.parse_query(query) == nonce
    assert discovery.parse_query(query[:100]) is None  # short: no answer to amplify
    assert discovery.parse_query(b"x" * discovery.QUERY_BYTES) is None

    long_name = "漢" * 60  # three bytes each in UTF-8
    reply = discovery.encode_reply(nonce, {
        "name": long_name, "port": 47821, "fingerprint": "AB" * 32, "approval": True,
        "engine": {"label": "L" * 300, "device": "cuda", "available": True, "family": "whisper"},
    })
    assert 0 < len(reply) <= discovery.QUERY_BYTES
    host = discovery.parse_reply(reply, nonce, "192.168.1.20")
    assert host.fingerprint == "AB" * 32 and host.port == 47821 and host.approval
    assert host.where == "192.168.1.20:47821" and host.compatible


def test_answers_to_another_query_or_with_a_bad_certificate_are_ignored():
    nonce = b"n" * discovery.NONCE_BYTES
    info = {"name": "devbox", "port": 47821, "fingerprint": "AB" * 32}
    assert discovery.parse_reply(discovery.encode_reply(b"o" * 16, info), nonce, "x") is None
    bad = dict(info, fingerprint="not hex")
    assert discovery.parse_reply(discovery.encode_reply(nonce, bad), nonce, "x") is None
    assert discovery.parse_reply(discovery.encode_reply(nonce, dict(info, port=0)), nonce, "x") is None
    assert discovery.parse_reply(b"garbage", nonce, "x") is None


@pytest.mark.parametrize("address, allowed", [
    ("192.168.1.20", True), ("10.0.0.5", True), ("172.16.0.1", True), ("127.0.0.1", True),
    ("169.254.1.1", True), ("100.64.0.5", True), ("8.8.8.8", False), ("224.0.0.1", False),
    ("nonsense", False),
])
def test_only_this_network_gets_answers(address, allowed):
    assert discovery.allowed_sender(address) is allowed


def test_a_sharing_host_answers_a_search_with_where_to_connect(lan_host):
    found = discovery.search(1.0, targets=[_target(lan_host)])
    assert len(found) == 1
    host = found[0]
    assert host.host_name == "devbox" and host.port == lan_host.port
    assert host.fingerprint == lan_host.identity.fingerprint
    assert host.engine["label"] == "Fake Parakeet" and host.approval and host.compatible

    lan_host.stop()
    assert discovery.search(0.5, targets=[("127.0.0.1", host.port)]) == []


def test_find_host_stops_at_the_certificate_it_wants(lan_host, monkeypatch):
    monkeypatch.setattr(discovery, "broadcast_targets", lambda: [_target(lan_host)])
    started = time.monotonic()
    found = discovery.find_host(lan_host.identity.fingerprint, timeout=3.0)
    assert found is not None and found.port == lan_host.port
    assert time.monotonic() - started < 2.0
    assert discovery.find_host("CD" * 32, timeout=0.5) is None


def test_the_responder_limits_how_often_it_answers_one_address():
    responder = discovery.DiscoveryResponder(lambda: {"name": "devbox", "port": 1, "fingerprint": "AB" * 32})
    query = discovery.make_query(b"n" * discovery.NONCE_BYTES)
    answers = [responder.answer(query, "192.168.1.30") for _ in range(30)]
    assert sum(1 for a in answers if a) == discovery._MAX_ANSWERS_PER_SENDER
    assert responder.answer(query, "192.168.1.31")  # someone else still gets one
    assert responder.answer(query, "8.8.8.8") is None


def test_a_taken_discovery_port_leaves_sharing_running(tmp_path, monkeypatch, engine):
    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker, socket.socket() as probe:
        blocker.bind(("127.0.0.1", 0))
        probe.bind(("127.0.0.1", 0))
        tcp_port = probe.getsockname()[1]
        probe.close()
        monkeypatch.setattr(discovery, "DISCOVERY_PORT", blocker.getsockname()[1])
        settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, tcp_port)
        service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
        service._engine = lambda: engine
        service.set_host_enabled(True)
        try:
            state = service.host_state()
            assert state["running"]
            kinds = [note.kind for note in state["notes"]]
            assert "discovery" in kinds
            note = next(note for note in state["notes"] if note.kind == "discovery")
            assert "can't find this one by searching" in note.message
        finally:
            service.set_host_enabled(False)


def test_sweep_knocks_on_each_address_and_keeps_those_that_answer():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert discovery.sweep(port, addresses=["127.0.0.1"], connect_timeout=1.0) == ["127.0.0.1"]
    assert discovery.sweep(port, addresses=["127.0.0.1"], connect_timeout=1.0) == []
    assert discovery.sweep(port, addresses=[]) == []


def test_the_probe_names_the_certificate_and_approval(lan_host):
    result = probe_host("127.0.0.1", lan_host.port)
    assert result.fingerprint == lan_host.identity.fingerprint
    assert result.approval is True


# ---- searching from the service ----

def _service(tmp_path=None):
    from services.remote_asr.service import RemoteEngineService

    identity_dir = str(tmp_path / "client-id") if tmp_path is not None else None
    return RemoteEngineService(lambda: None, identity_dir=identity_dir, bind="127.0.0.1")


def test_finds_on_this_network_and_the_tailnet_are_one_entry(monkeypatch, tmp_path):
    from services.remote_asr.service import TailnetHost, TailnetScan

    jed = "AA" * 32
    peer = tailscale.TailscalePeer(name="jed", dns_name="jed.ts.net", address="100.64.0.9", os="linux",
                                   online=True, owner="me@example.com", tagged=False, mine=True)
    status = tailscale.TailscaleStatus("running", owner="me@example.com", peers=(peer,))
    service = _service(tmp_path)
    monkeypatch.setattr(service, "scan_tailnet", lambda port=None: TailnetScan(status, (TailnetHost(
        peer=peer, port=47821, host_name="jed", engine={"label": "Parakeet", "available": True},
        tailscale_pairing=True, compatible=True, fingerprint=jed, approval=True,
    ),)))
    lan = [
        discovery.LanHost("192.168.1.9", 47821, "jed", jed, {"label": "Parakeet"}, 1, True),
        discovery.LanHost("192.168.1.12", 47821, "nas", "BB" * 32, {}, 1, True),
    ]
    monkeypatch.setattr(discovery, "search", lambda *a, **k: list(lan))
    scan = service.scan_nearby()
    assert [host.name for host in scan.hosts] == ["jed", "nas"]
    both, nas = scan.hosts
    assert both.lan is not None and both.tailnet is not None
    # The owner's own Tailscale computer pairs over the tailnet, without asking.
    assert both.can_pair_without_code and both.address == "100.64.0.9:47821"
    assert not nas.can_pair_without_code and nas.address == "192.168.1.12:47821"
    assert scan.status.running and not scan.swept


def test_a_search_leaves_out_this_computers_own_answer(monkeypatch, tmp_path):
    own = ensure_host_identity(str(tmp_path / "client-id")).fingerprint
    monkeypatch.setattr(discovery, "search", lambda *a, **k: [
        discovery.LanHost("192.168.1.5", 47821, "me", own, {}, 1, True),
    ])
    assert _service(tmp_path).scan_nearby().hosts == ()


def test_checking_every_address_probes_what_answers(lan_host, monkeypatch, tmp_path):
    from services.remote_asr import service as service_module

    monkeypatch.setattr(service_module.protocol, "DEFAULT_PORT", lan_host.port)
    monkeypatch.setattr(discovery, "search", lambda *a, **k: [])
    knocked = []
    monkeypatch.setattr(discovery, "sweep", lambda port, **k: knocked.append(port) or ["127.0.0.1"])
    service = _service(tmp_path)
    assert service.scan_nearby().hosts == ()
    assert knocked == []  # only when asked
    scan = service.scan_nearby(sweep=True)
    assert knocked == [lan_host.port] and scan.swept
    assert [host.name for host in scan.hosts] == ["devbox"]
    assert scan.hosts[0].lan.fingerprint == lan_host.identity.fingerprint
    assert scan.hosts[0].approval


# ---- approval on the host ----

def test_an_allowed_request_pairs_with_the_number_both_screens_showed(lan_host, store):
    thread, outcome = _request_in_background(lan_host)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    pending = lan_host.pending_request()
    assert pending["name"] == "laptop" and pending["address"] == "127.0.0.1"
    assert 100 < pending["seconds_left"] <= 120
    assert _wait_for(lambda: outcome["codes"], 5)
    assert outcome["codes"] == [pending["sas"]]

    assert lan_host.answer_request(pending["id"], True)
    thread.join(5)
    result = outcome["result"]
    assert result.via == "approval" and result.token and result.host_name == "devbox"
    assert result.alternates == ("192.0.2.10",)
    assert store.devices[0]["via"] == "approval" and store.devices[0]["name"] == "laptop"
    assert lan_host.pending_request() is None
    assert not lan_host.answer_request(pending["id"], True)  # answered once
    connection = RemoteConnection("127.0.0.1", lan_host.port, result.token, result.fingerprint)
    assert connection.connect()["host"]["name"] == "devbox"
    connection.close()


def test_a_denied_request_pairs_nothing(lan_host, store):
    thread, outcome = _request_in_background(lan_host)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    lan_host.answer_request(lan_host.pending_request()["id"], False)
    thread.join(5)
    assert "declined" in str(outcome["error"])
    assert store.devices == []


def test_canceling_here_takes_the_question_off_the_host(lan_host, store):
    canceled = threading.Event()
    thread, outcome = _request_in_background(lan_host, canceled)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    canceled.set()
    thread.join(5)
    assert isinstance(outcome["error"], PairingCanceled)
    assert _wait_for(lambda: lan_host.pending_request() is None, 3)
    assert store.devices == []


def test_one_question_at_a_time(lan_host):
    canceled = threading.Event()
    thread, _ = _request_in_background(lan_host, canceled)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    with pytest.raises(RemoteEngineError, match="already asking about another computer"):
        request_pairing("127.0.0.1", lan_host.port, "intruder", on_code=lambda sas: None)
    canceled.set()
    thread.join(5)


def test_an_address_that_keeps_asking_is_turned_away(lan_host, monkeypatch):
    from services.remote_asr import host as host_module

    monkeypatch.setattr(host_module, "MAX_APPROVAL_REQUESTS", 1)
    canceled = threading.Event()
    canceled.set()  # ask, then give up at once
    thread, _ = _request_in_background(lan_host, canceled)
    thread.join(5)
    assert _wait_for(lambda: lan_host.pending_request() is None, 3)
    with pytest.raises(RemoteEngineError, match="Too many pairing requests"):
        request_pairing("127.0.0.1", lan_host.port, "laptop", on_code=lambda sas: None)


def test_requests_from_outside_this_network_are_refused(lan_host, monkeypatch):
    monkeypatch.setattr(discovery, "allowed_sender", lambda address: False)
    with pytest.raises(RemoteEngineError, match="only takes pairing requests from its own network"):
        request_pairing("127.0.0.1", lan_host.port, "laptop", on_code=lambda sas: None)


def test_nobody_answering_turns_the_request_down(lan_host, monkeypatch):
    from services.remote_asr import host as host_module

    monkeypatch.setattr(host_module, "APPROVAL_TIMEOUT_S", 0.5)
    with pytest.raises(RemoteEngineError, match="Nobody answered on devbox"):
        request_pairing("127.0.0.1", lan_host.port, "laptop", on_code=lambda sas: None)


def test_turning_sharing_off_ends_a_waiting_request(lan_host):
    thread, outcome = _request_in_background(lan_host)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    lan_host.stop()
    thread.join(5)
    assert "stopped sharing" in str(outcome["error"])


def test_a_slow_answer_still_beats_the_shutdown(lan_host, monkeypatch):
    """The server's shutdown closes every connection; the request says why first."""
    send = lan_host._send

    def slow_send(ws, message):
        if message.get("code") == "pair_closed":
            time.sleep(0.5)  # long enough for an unwaited shutdown to close it
        send(ws, message)

    monkeypatch.setattr(lan_host, "_send", slow_send)
    thread, outcome = _request_in_background(lan_host)
    assert _wait_for(lambda: lan_host.pending_request() is not None, 5)
    lan_host.stop()
    thread.join(5)
    assert "stopped sharing" in str(outcome["error"])


def test_a_request_that_never_answers_holds_up_stopping_only_briefly(lan_host, monkeypatch):
    from services.remote_asr import host as host_module

    monkeypatch.setattr(host_module, "STOP_ANSWER_GRACE_S", 0.3)
    # Nothing is handling this request, so nothing will ever answer it.
    request = host_module.PairRequest("r1", "laptop", "127.0.0.1", time.monotonic() + 60, sas="123456")
    lan_host._request = request
    started = time.monotonic()
    lan_host.stop()
    assert time.monotonic() - started < 2.0
    assert request.stopped and request.done.is_set()


def test_a_host_whose_reveal_breaks_its_commitment_is_not_trusted(tmp_path):
    """Something answering for the host that changed its nonce after seeing ours."""
    import json

    from websockets.sync.server import serve

    from services.remote_asr.tls import server_context

    identity = ensure_host_identity(str(tmp_path / "liar"))

    def handle(ws):
        ws.recv(timeout=5)
        ws.send(json.dumps({"type": "pair_commit", "commit": protocol.pairing_commitment(b"a" * 32)}))
        ws.recv(timeout=5)
        ws.send(json.dumps({"type": "pair_reveal", "nonce": (b"b" * 32).hex()}))
        try:
            ws.recv(timeout=5)
        except Exception:
            pass

    server = serve(handle, "127.0.0.1", 0, ssl=server_context(identity))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        shown = []
        with pytest.raises(RemoteEngineError, match="didn't check out"):
            request_pairing("127.0.0.1", server.socket.getsockname()[1], "laptop", on_code=shown.append)
        assert shown == []  # no number to compare was ever shown
    finally:
        server.shutdown()


# ---- following a host that moved ----

def _saved_pairing(host="192.168.1.20", port=47821, alternates=("100.64.0.2",), fingerprint="AB" * 32):
    return remote_settings.save_client_pairing(host, port, PairingResult(
        token="t", device_id="d", host_name="devbox", fingerprint=fingerprint,
        alternates=alternates, via="approval",
    ))


def test_a_new_address_from_the_router_replaces_the_old_one():
    _saved_pairing()
    moved = remote_settings.remember_host_addresses(
        "ab" * 32, "192.168.1.37", 47821, ["192.168.1.37", "100.64.0.2"],
    )
    assert moved.host == "192.168.1.37" and moved.alternates == ("100.64.0.2",)
    loaded = remote_settings.load_client_pairing()
    assert loaded.host == "192.168.1.37" and loaded.via == "approval"
    # Nothing new: nothing written.
    assert remote_settings.remember_host_addresses(
        "AB" * 32, "192.168.1.37", 47821, ["192.168.1.37", "100.64.0.2"]) is None


def test_away_from_home_the_home_address_stays_first():
    _saved_pairing()
    assert remote_settings.remember_host_addresses(
        "AB" * 32, "100.64.0.2", 47821, ["192.168.1.20", "100.64.0.2"]) is None
    assert remote_settings.load_client_pairing().host == "192.168.1.20"


def test_a_named_host_keeps_its_name_and_a_tailnet_address_isnt_dropped():
    _saved_pairing(host="devbox.local", alternates=("192.168.1.20", "100.64.0.2"))
    moved = remote_settings.remember_host_addresses(
        "AB" * 32, "192.168.1.37", 47821, ["192.168.1.37"])  # Tailscale is off there for now
    assert moved.host == "devbox.local"
    assert moved.alternates == ("192.168.1.37", "100.64.0.2")


def test_another_hosts_addresses_are_never_saved():
    _saved_pairing()
    assert remote_settings.remember_host_addresses("CD" * 32, "10.0.0.1", 47821, ["10.0.0.1"]) is None
    assert remote_settings.load_client_pairing().host == "192.168.1.20"


def test_a_pairing_is_the_same_one_after_its_address_moves():
    pairing = _saved_pairing()
    moved = remote_settings.remember_host_addresses(
        "AB" * 32, "192.168.1.37", 47821, ["192.168.1.37"])
    assert moved == pairing and moved.host != pairing.host


def test_a_connection_finds_the_host_where_it_answers_now_and_saves_it(lan_host, monkeypatch):
    code = lan_host.open_pairing()
    result = pair_with_host("127.0.0.1", lan_host.port, code, "laptop")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        stale_port = probe.getsockname()[1]
    # The saved pairing points where the host no longer is.
    remote_settings.save_client_pairing("127.0.0.1", stale_port, result)
    monkeypatch.setattr(discovery, "broadcast_targets", lambda: [_target(lan_host)])
    RemoteConnection._searched_at.clear()
    try:
        connection = RemoteConnection("127.0.0.1", stale_port, result.token, result.fingerprint,
                                      timeout=1.0)
        ready = connection.connect()
        assert ready["host"]["name"] == "devbox" and connection.port == lan_host.port
        connection.close()
        saved = remote_settings.load_client_pairing()
        assert saved.port == lan_host.port and saved.alternates == ("192.0.2.10",)

        # A host that is just off isn't searched for on every retry.
        searches = []
        monkeypatch.setattr(discovery, "find_host", lambda fp, timeout=1.2: searches.append(fp))
        lan_host.stop()
        for _ in range(2):
            with pytest.raises(RemoteEngineError):
                RemoteConnection("127.0.0.1", lan_host.port, result.token, result.fingerprint,
                                 timeout=1.0).connect()
        assert len(searches) <= 1
    finally:
        RemoteConnection._searched_at.clear()
        RemoteConnection._last_good.clear()


def test_a_forged_answer_gets_no_token(lan_host, tmp_path, monkeypatch):
    """Discovery says the host is at a stranger's address: the certificate check stops it."""
    code = lan_host.open_pairing()
    result = pair_with_host("127.0.0.1", lan_host.port, code, "laptop")
    stranger = SpeechHost(
        engine_provider=lambda: FakeEngine(),
        registry=DeviceRegistry(ListStore().load, ListStore().save),
        identity=ensure_host_identity(str(tmp_path / "stranger")),
        host_name="stranger",
    )
    tokens = []
    stranger.registry.authenticate = lambda token: tokens.append(token)
    stranger.start(port=0, bind="127.0.0.1")
    monkeypatch.setattr(discovery, "find_host", lambda fp, timeout=1.2: discovery.LanHost(
        "127.0.0.1", stranger.port, "devbox", fp, {}, 1, True))
    RemoteConnection._searched_at.clear()
    lan_host.stop()
    try:
        with pytest.raises(RemoteEngineError):
            RemoteConnection("127.0.0.1", lan_host.port, result.token, result.fingerprint,
                             timeout=1.0).connect()
        time.sleep(0.2)
        assert tokens == []
    finally:
        stranger.stop()
        RemoteConnection._searched_at.clear()


# ---- reachability ----

def test_network_profiles_parse_from_either_powershell():
    assert reachability.parse_profiles(
        '[{"Name":"Home","InterfaceAlias":"Wi-Fi","NetworkCategory":0},'
        '{"Name":"Tailscale","InterfaceAlias":"Tailscale","NetworkCategory":1}]'
    ) == [reachability.NetworkProfile("Home", "Wi-Fi", "public"),
          reachability.NetworkProfile("Tailscale", "Tailscale", "private")]
    assert reachability.parse_profiles(
        '{"Name":"Office","InterfaceAlias":"Ethernet","NetworkCategory":"DomainAuthenticated"}'
    ) == [reachability.NetworkProfile("Office", "Ethernet", "domain")]
    assert reachability.parse_profiles("") == []
    assert reachability.parse_profiles("not json") == []


def test_a_public_network_a_vpn_and_a_taken_port_each_say_what_to_do():
    from services.lan_address import LAN, VPN, LocalAddress

    lan = LocalAddress("192.168.1.20", "Wi-Fi", LAN, 24, True)
    vpn = LocalAddress("10.2.0.2", "ProtonVPN", VPN, 32)
    public = [reachability.NetworkProfile("Home", "Wi-Fi", "public")]
    notes = reachability.host_notes(lan, [lan, vpn], public, "UDP port 47821 is in use")
    assert [note.kind for note in notes] == ["public_network", "vpn", "discovery"]
    assert 'Windows treats "Home" as a public network' in notes[0].message
    assert notes[0].url == "ms-settings:network-wifi" and notes[0].action
    private = [reachability.NetworkProfile("Home", "Wi-Fi", "private")]
    assert reachability.host_notes(lan, [lan], private) == []
    assert reachability.host_notes(lan, [lan], None) == []


# ---- the Settings page ----

def _pump_until(predicate, timeout=10):
    from PyQt6.QtWidgets import QApplication

    return _wait_for(lambda: (QApplication.processEvents() or True) and predicate(), timeout)


@pytest.fixture
def client_page(tmp_path):
    from services.remote_asr.service import RemoteEngineService
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "client-id"), bind="127.0.0.1")
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    section = dialog.remote_section
    section.bind(service, lambda name: None)

    def open_page():
        dialog.show()
        dialog.select_destination(REMOTE_ENGINE)
        assert _pump_until(lambda: section._scan is not None and not section._scan_busy)
        return section

    yield open_page, service
    service.shutdown()
    dialog.close()
    dialog.deleteLater()


def _rows(section):
    from ui_qt.widgets import WrappedLabel

    return [label.text() for label in section.find_list.findChildren(WrappedLabel)
            if label.objectName() == "remoteFindLabel"]


def test_page_pairs_with_a_computer_on_this_network_once_it_is_allowed(client_page, lan_host, monkeypatch):
    from ui_qt.widgets import PrimaryButton

    monkeypatch.setattr(discovery, "broadcast_targets", lambda: [_target(lan_host)])
    open_page, service = client_page
    section = open_page()
    assert _rows(section) == ["devbox · Fake Parakeet on cuda · on this network"]
    connect = [b for b in section.find_list.findChildren(PrimaryButton) if b.text() == "Connect"]
    connect[0].click()

    assert _pump_until(lambda: not section.wait_box.isHidden())
    pending = lan_host.pending_request()
    assert section.wait_code_label.text() == protocol.format_sas(pending["sas"])
    assert "On devbox, click Allow if it shows this number" in section.wait_label.text()
    assert not section.search_button.isEnabled()

    lan_host.answer_request(pending["id"], True)
    assert _pump_until(lambda: not section._pairing_busy)
    pairing = service.client_pairing()
    assert pairing is not None and pairing.via == "approval"
    assert section.client_message.text() == (
        "Paired with devbox. Click \"Use for dictation\" to start using it."
    )
    assert section.find_tile.isHidden() and section.wait_box.isHidden()
    # Paired on this network only: Tailscale is how it works away from home.
    assert not section.tailscale_hint.isHidden()
    assert "away from home" in section.tailscale_hint.text()
    assert "https://tailscale.com/download" in section.tailscale_hint.text()


def test_page_cancels_a_request_still_waiting(client_page, lan_host, monkeypatch):
    from ui_qt.widgets import PrimaryButton

    monkeypatch.setattr(discovery, "broadcast_targets", lambda: [_target(lan_host)])
    open_page, service = client_page
    section = open_page()
    [b for b in section.find_list.findChildren(PrimaryButton) if b.text() == "Connect"][0].click()
    assert _pump_until(lambda: not section.wait_box.isHidden())
    section.wait_cancel_button.click()
    assert _pump_until(lambda: not section._pairing_busy)
    assert section.find_message.text() == "Pairing canceled."
    assert service.client_pairing() is None
    assert section.wait_box.isHidden()
    assert _wait_for(lambda: lan_host.pending_request() is None, 3)


def test_page_offers_a_code_for_a_host_too_old_to_ask(client_page, lan_host, monkeypatch):
    from ui_qt.widgets import Button

    old = discovery.LanHost("127.0.0.1", lan_host.port, "oldbox", "EE" * 32, {"label": "Whisper",
                            "available": True, "device": "cpu"}, 1, False)
    monkeypatch.setattr(discovery, "search", lambda *a, **k: [old])
    open_page, _ = client_page
    section = open_page()
    assert _rows(section) == ["oldbox · Whisper on cpu · on this network"]
    use_code = [b for b in section.find_list.findChildren(Button) if b.text() == "Use a code"]
    use_code[0].click()
    assert not section.pair_row.isHidden()
    assert section.address_edit.text() == f"127.0.0.1:{lan_host.port}"
    assert "On oldbox, click \"Show a pairing code\"" in section.find_message.text()


def test_page_blames_the_firewall_when_a_found_host_wont_connect(client_page):
    from services.remote_asr.service import NearbyHost

    open_page, _ = client_page
    section = open_page()
    found = NearbyHost("jed", "AB" * 32, {}, True, True,
                       lan=discovery.LanHost("192.168.1.9", 47821, "jed", "AB" * 32, {}, 1, True))
    message = section._pair_error(RemoteUnreachable("Couldn't reach 192.168.1.9:47821."), found)
    assert message.startswith("jed answered the search but didn't accept the connection")
    refused = RemoteUnreachable("Couldn't reach 192.168.1.9:47821: nothing there...", refused=True)
    assert section._pair_error(refused, found) == str(refused)
    assert section._pair_error(RemoteUnreachable("Couldn't reach x."), None) == "Couldn't reach x."


def test_page_suggests_checking_every_address_when_nothing_answers(client_page, lan_host, monkeypatch):
    from services.remote_asr import service as service_module

    monkeypatch.setattr(service_module.protocol, "DEFAULT_PORT", lan_host.port)
    monkeypatch.setattr(discovery, "sweep", lambda port, **k: ["127.0.0.1"])
    open_page, _ = client_page
    section = open_page()
    assert _rows(section) == [] and not section.sweep_button.isHidden()
    section.sweep_button.click()
    assert _pump_until(lambda: not section._scan_busy and section._scan.swept)
    assert _rows(section) == ["devbox · Fake Parakeet on cuda · on this network"]
    assert section.sweep_button.isHidden()


def test_host_page_shows_what_may_keep_others_out(tmp_path, monkeypatch, engine):
    from PyQt6.QtWidgets import QApplication

    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog
    from ui_qt.widgets import Button, WrappedLabel

    note = reachability.Note("public_network", "Windows treats \"Home\" as a public network.",
                             action="Open network settings", url="ms-settings:network-wifi")
    monkeypatch.setattr(RemoteEngineService, "_reachability_notes", lambda self, lan, error: [note])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, port)
    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    service._engine = lambda: engine
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    opened = []
    try:
        section = dialog.remote_section
        section.bind(service, lambda name: None)
        monkeypatch.setattr(section, "_open_url", opened.append)
        dialog.select_destination(REMOTE_ENGINE)
        section.share_tile.checkbox.setChecked(True)
        QApplication.processEvents()
        assert not section.host_notes.isHidden()
        texts = [label.text() for label in section.host_notes.findChildren(WrappedLabel)]
        assert texts == ["Windows treats \"Home\" as a public network."]
        [button] = [b for b in section.host_notes.findChildren(Button) if b.text() == "Open network settings"]
        button.click()
        assert opened == ["ms-settings:network-wifi"]
        section.share_tile.checkbox.setChecked(False)
        QApplication.processEvents()
        assert section.host_notes.isHidden()
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()


def test_a_page_closed_mid_search_is_not_held_by_its_threads(monkeypatch):
    # The search worker and the service's listeners used to emit on the page
    # from their own threads, which raced Settings closing; the listeners
    # also kept a closed page alive for as long as the service lived.
    import gc
    import weakref

    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication, QWidget

    from ui_qt.dialogs.settings_remote import RemoteEngineSection

    gate = threading.Event()
    failures = []
    monkeypatch.setattr(threading, "excepthook", failures.append)

    class Listened:
        def __init__(self):
            self.listeners = []

        def add_listener(self, listener):
            self.listeners.append(listener)

        def remove_listener(self, listener):
            self.listeners.remove(listener)

    class Service(Listened):
        def client_pairing(self):
            return None

        def scan_nearby(self, sweep=False):
            gate.wait(5)
            return None

    service, records = Service(), Listened()
    parent = QWidget()
    section = RemoteEngineSection(parent, records=records)
    section.bind(service)
    section.scan_nearby(force=True)
    workers = [t for t in threading.enumerate() if t.name == "remote-engine-nearby-scan"]
    assert workers
    ref = weakref.ref(section)
    del section
    parent.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    del parent
    gc.collect()
    assert ref() is None
    gate.set()
    # The service and the record sync still hold the closed page's listeners.
    listeners = [*service.listeners, *records.listeners]
    told = threading.Thread(target=lambda: [listener("state") for listener in listeners])
    told.start()
    for thread in [*workers, told]:
        thread.join(5)
        assert not thread.is_alive()
    QApplication.processEvents()
    assert failures == []


# ---- the host's question ----

class _AskingService:
    def __init__(self, grants=()):
        self.request = None
        self.answers = []
        self.listeners = []
        self.grants = list(grants)

    def pairing_grants(self):
        return self.grants

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def pending_pair_request(self):
        return self.request

    def answer_pair_request(self, request_id, allow):
        self.answers.append((request_id, allow))
        return True

    def tell(self, kind="pair_request"):
        for listener in list(self.listeners):
            listener(kind)


def _ask(service, request_id="r1"):
    from PyQt6.QtWidgets import QApplication

    service.request = {"id": request_id, "name": "laptop", "address": "192.168.1.30",
                       "sas": "042917", "seconds_left": 90.0}
    service.tell()
    QApplication.processEvents()


def test_the_host_asks_with_the_number_and_passes_the_answer_on():
    from ui_qt.dialogs.pair_request_dialog import PairRequestPrompter

    service = _AskingService()
    prompter = PairRequestPrompter(service)
    try:
        _ask(service)
        dialog = prompter.dialog
        assert dialog is not None and dialog.isVisible()
        assert dialog.code_label.text() == "042 917"
        assert "1:29" in dialog.expiry_label.text() or "1:30" in dialog.expiry_label.text()
        dialog.allow_button.click()
        assert service.answers == [("r1", True)]
    finally:
        prompter.detach()


def test_the_question_says_what_else_allowing_grants(tmp_path):
    from services.settings import SettingsKey, settings_manager
    from ui_qt.dialogs.pair_request_dialog import PairRequestPrompter

    service = _service(tmp_path)
    assert service.pairing_grants() == []
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_KEEP_RECORDS, True)
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_MANAGE_MCP, True)
    grants = service.pairing_grants()
    assert grants[0] == "keep its dictations and meetings here"
    assert "access token" in grants[1]

    asking = _AskingService(grants)
    prompter = PairRequestPrompter(asking)
    try:
        _ask(asking)
        text = prompter.dialog.grants_label.text()
        assert text.startswith("Once allowed, it can dictate and transcribe with the engine "
                               "selected here. It can also keep its dictations and meetings "
                               "here and manage MCP here")
    finally:
        prompter.detach()


def test_closing_the_question_denies_and_a_withdrawn_one_just_goes():
    from PyQt6.QtWidgets import QApplication

    from ui_qt.dialogs.pair_request_dialog import PairRequestPrompter

    service = _AskingService()
    prompter = PairRequestPrompter(service)
    try:
        _ask(service, "r1")
        prompter.dialog.close()
        assert service.answers == [("r1", False)]

        _ask(service, "r2")
        dialog = prompter.dialog
        service.request = None  # canceled on the other computer
        service.tell()
        QApplication.processEvents()
        assert prompter.dialog is None and not dialog.isVisible()
        assert service.answers == [("r1", False)]
    finally:
        prompter.detach()


def test_a_replaced_prompter_is_not_held_by_a_server_thread():
    # The listener used to capture the prompter, which has no parent: a
    # server thread that copied the listeners before detach could drop the
    # last reference (deleting it off the Qt thread) or emit on it as it was
    # deleted, which segfaulted CI.
    import gc
    import weakref

    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication

    from ui_qt.dialogs.pair_request_dialog import PairRequestPrompter

    service = _AskingService()
    prompter = PairRequestPrompter(service)
    listener = service.listeners[0]  # mid-notify on a server thread
    prompter.detach()
    ref = weakref.ref(prompter)
    prompter.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    del prompter
    gc.collect()
    assert ref() is None

    service.request = {"id": "r1", "name": "laptop", "address": "192.168.1.30",
                       "sas": "042917", "seconds_left": 90.0}
    errors = []

    def notify():
        try:
            listener("pair_request")
        except Exception as exc:
            errors.append(exc)

    server = threading.Thread(target=notify, name="pair-request-notify")
    server.start()
    server.join(5)
    assert not server.is_alive()
    QApplication.processEvents()
    assert errors == []
    assert ref() is None
