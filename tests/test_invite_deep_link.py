"""Tests for openwhisper://pair URI parsing, .openwhisper-invite files, IPC, and pairing."""

from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace

import pytest

from services.remote_asr import invite
from services.remote_asr.client import RemoteEngineError
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity
from tests.fakes.remote import FakeEngine, ListStore

FP_A = "A" * 64
FP_B = "B" * 64


# ---- URI & JSON Invite Validation ----


def test_build_and_parse_invite_uri_round_trip():
    uri = invite.build_invite_uri(
        "192.168.1.25",
        47821,
        FP_A.lower(),
        "042789",
        name="Studio PC",
        alternates=("100.64.0.12", "studio.local", "192.168.1.25"),
    )
    assert uri.startswith("openwhisper://pair?")
    parsed = invite.parse_invite_uri(uri)
    assert parsed.host == "192.168.1.25"
    assert parsed.port == 47821
    assert parsed.fingerprint == FP_A
    assert parsed.code == "042789"
    assert parsed.name == "Studio PC"
    assert parsed.alternates == ("100.64.0.12", "studio.local")


def test_ipv6_invite_uri_and_file_round_trip(tmp_path):
    link = invite.InviteLink(
        host="fd7a:115c:a1e0::1",
        port=47821,
        fingerprint=FP_A,
        code="123456",
        name="IPv6 Host",
    )
    reparsed = invite.parse_invite_uri(link.uri)
    assert reparsed == link
    assert reparsed.address == "[fd7a:115c:a1e0::1]:47821"

    file_path = tmp_path / "studio.openwhisper-invite"
    file_path.write_text(invite.encode_invite_file(link), encoding="utf-8")
    loaded = invite.load_invite_file(file_path)
    assert loaded == link


@pytest.mark.parametrize(
    "bad_uri",
    [
        "",
        "https://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456",
        "openwhisper://other?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456",
        "openwhisper://pair/extra?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=0&fp=" + FP_A + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=70000&fp=" + FP_A + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=abc&fp=" + FP_A + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=TOOSHORT&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + ("G" * 64) + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=12345",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=12345a",
        "openwhisper://pair?host=bad/host&port=47821&fp=" + FP_A + "&code=123456",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456&name=bad\nname",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456&alt=bad%00host",
        "openwhisper://pair?host=1.2.3.4&port=47821&fp=" + FP_A + "&code=123456#frag",
        "openwhisper://pair?host=1.2.3.4&host=5.6.7.8&port=47821&fp=" + FP_A + "&code=123456",
    ],
)
def test_parse_invite_uri_rejects_malformed_and_control_chars(bad_uri):
    with pytest.raises(invite.InviteValidationError):
        invite.parse_invite_uri(bad_uri)


def test_decode_invite_file_rejects_mismatched_uri_and_bad_schema():
    link = invite.InviteLink("192.168.1.5", 47821, FP_A, "123456", "HostA")
    doc = json.loads(invite.encode_invite_file(link))
    doc["uri"] = invite.build_invite_uri("192.168.1.5", 47821, FP_B, "123456", name="HostA")
    with pytest.raises(invite.InviteValidationError, match="does not match"):
        invite.decode_invite_file(json.dumps(doc))

    doc2 = json.loads(invite.encode_invite_file(link))
    doc2["schema"] = 99
    with pytest.raises(invite.InviteValidationError, match="schema"):
        invite.decode_invite_file(json.dumps(doc2))


# ---- Single-Instance Invite IPC & main.py CLI Handling ----


def test_invite_ipc_forwards_authenticated_invite_and_rejects_wrong_token(tmp_path):
    received: list[invite.InviteLink] = []
    ready = threading.Event()

    def on_invite(link: invite.InviteLink) -> None:
        received.append(link)
        ready.set()

    server = invite.InviteIpcServer(tmp_path, on_invite)
    port = server.start()
    try:
        link = invite.InviteLink("192.168.1.44", 47821, FP_A, "998877", "Workstation")

        # Unauthorized client with wrong token must be rejected.
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
            conn.sendall(
                (json.dumps({"token": "wrong-token", "uri": link.uri}) + "\n").encode("utf-8")
            )
            reply = json.loads(conn.recv(256).decode("utf-8"))
            assert reply["ok"] is False
        assert received == []

        # Authorized forward succeeds.
        assert invite.forward_invite_to_running_instance(tmp_path, link) is True
        assert ready.wait(timeout=3.0)
        assert received == [link]
    finally:
        server.stop()


def test_main_cli_invite_handling_stages_forwards_and_rejects_bad(tmp_path):
    import main as main_mod

    invite.consume_pending_startup_invite()
    link = invite.InviteLink("192.168.1.50", 47821, FP_A, "112233", "DeskHost")

    # Invalid URI returns exit code 2 without staging.
    assert main_mod._handle_invite_cli_argument(["openwhisper://pair?host=bad&port=0"], data_dir=str(tmp_path)) == 2
    assert invite.consume_pending_startup_invite() is None

    # Fresh launch with URI stages pending startup invite and returns None to continue launch.
    assert main_mod._handle_invite_cli_argument([link.uri], data_dir=str(tmp_path)) is None
    assert invite.consume_pending_startup_invite() == link

    # Fresh launch with .openwhisper-invite file also stages pending startup invite.
    inv_file = tmp_path / "desk.openwhisper-invite"
    inv_file.write_text(invite.encode_invite_file(link), encoding="utf-8")
    assert main_mod._handle_invite_cli_argument([str(inv_file)], data_dir=str(tmp_path)) is None
    assert invite.consume_pending_startup_invite() == link

    # When another instance is listening on IPC, forwards and returns 0!
    got: list[invite.InviteLink] = []
    done = threading.Event()
    ipc = invite.InviteIpcServer(tmp_path, lambda inv: (got.append(inv), done.set()))
    ipc.start()
    try:
        assert main_mod._handle_invite_cli_argument([link.uri], data_dir=str(tmp_path)) == 0
        assert done.wait(timeout=3.0)
        assert got == [link]
    finally:
        ipc.stop()


def test_main_self_test_edition_helper_respects_env(monkeypatch):
    import main as main_mod

    monkeypatch.setenv("OPENWHISPER_EDITION", "client")
    assert main_mod._is_client_only_edition() is True
    monkeypatch.setenv("OPENWHISPER_EDITION", "full")
    assert main_mod._is_client_only_edition() is False


# ---- Fingerprint-Verified Invite Pairing ----


def test_pair_with_invite_verifies_tls_fingerprint_and_requires_replace_confirmation(tmp_path):
    store = ListStore()
    speech_host = SpeechHost(
        engine_provider=lambda: FakeEngine(),
        registry=DeviceRegistry(store.load, store.save),
        identity=ensure_host_identity(str(tmp_path / "host")),
        host_name="devbox",
        addresses=lambda: ["127.0.0.1"],
    )
    speech_host.start(port=0, bind="127.0.0.1", discovery_port=0)
    try:
        code = speech_host.open_pairing()
        real_fp = speech_host.identity.fingerprint
        assert code is not None

        # 1. Wrong fingerprint must fail BEFORE sending pairing code.
        bad_invite = invite.InviteLink("127.0.0.1", speech_host.port, FP_B, code, "devbox")
        saved: list = []

        class _FakeService:
            def __init__(self, existing=None):
                self._existing = existing

            def client_pairing(self):
                return self._existing

            def _paired(self, host, port, result):
                saved.append((host, port, result))
                return SimpleNamespace(
                    host=host,
                    port=port,
                    fingerprint=result.fingerprint,
                    host_name=result.host_name,
                    via=result.via,
                )

        with pytest.raises(RemoteEngineError, match="different security identity"):
            invite.pair_with_invite(_FakeService(), bad_invite)
        # Pairing code was never consumed by the MITM attempt!
        assert speech_host.pairing_status() is not None
        assert saved == []

        # 2. Existing different host must NOT be overwritten without confirmation.
        good_invite = invite.InviteLink("127.0.0.1", speech_host.port, real_fp, code, "devbox")
        existing = SimpleNamespace(host_name="OldHost", fingerprint=FP_B)
        with pytest.raises(invite.InvitePairingDeclined):
            invite.pair_with_invite(_FakeService(existing), good_invite, confirm_replace=lambda _e, _i: False)
        assert saved == []

        # 3. Confirmed pairing with matching TLS fingerprint succeeds!
        pairing = invite.pair_with_invite(
            _FakeService(existing), good_invite, confirm_replace=lambda _e, _i: True
        )
        assert pairing.fingerprint == real_fp
        assert pairing.host_name == "devbox"
        assert len(saved) == 1
    finally:
        speech_host.stop()
