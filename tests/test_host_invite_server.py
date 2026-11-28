"""Tests for the pure-Python QR SVG generator and HostInviteServer."""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from services.remote_asr import invite, qr

FP = "A1B2C3D4" * 8
VERSION = "2.9.2"


# ---- Pure-Python QR Code SVG generator ----


def test_qr_matrix_dimensions_finders_and_determinism():
    short_matrix = qr.encode_qr_matrix("http://192.168.1.20:47822/join/abc", ecc="M")
    n = len(short_matrix)
    assert n >= 21 and (n - 21) % 4 == 0
    assert all(len(row) == n for row in short_matrix)

    # Top-left, top-right, and bottom-left 7x7 finder borders must be solid dark.
    for top, left in ((0, 0), (0, n - 7), (n - 7, 0)):
        for i in range(7):
            assert short_matrix[top][left + i] is True
            assert short_matrix[top + 6][left + i] is True
            assert short_matrix[top + i][left] is True
            assert short_matrix[top + i][left + 6] is True
        # 3x3 center block is solid dark.
        for r in range(2, 5):
            for c in range(2, 5):
                assert short_matrix[top + r][left + c] is True

    # Dark module at (4*V + 9, 8) must be True.
    version = (n - 17) // 4
    assert short_matrix[4 * version + 9][8] is True

    # Deterministic across calls.
    assert qr.encode_qr_matrix("http://192.168.1.20:47822/join/abc", ecc="M") == short_matrix


def test_qr_supports_versions_up_to_10_and_renders_valid_svg():
    # ~240 byte openwhisper://pair URI fits in Version 10 ECC-L via render_invite_qr_svg.
    long_uri = invite.build_invite_uri(
        "192.168.1.42",
        47821,
        FP,
        "123456",
        name="Studio-Workstation-Main",
        alternates=("100.64.10.22", "studio.tailnet.ts.net"),
    )
    svg = qr.render_invite_qr_svg(long_uri, module_size=4, quiet_zone=2)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.attrib["shape-rendering"] == "crispEdges"
    # Verify version >= 7 matrix (with version info blocks) builds cleanly.
    v8_matrix = qr.encode_qr_matrix("X" * 150, ecc="M")
    assert len(v8_matrix) >= 45


def test_qr_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        qr.encode_qr_matrix("")
    with pytest.raises(ValueError):
        qr.encode_qr_matrix("hello", ecc="Z")
    with pytest.raises(ValueError):
        qr.encode_qr_matrix("A" * 400, ecc="L")
    with pytest.raises(ValueError):
        qr.render_qr_svg("hello", dark="javascript:alert(1)")
    with pytest.raises(ValueError):
        qr.render_qr_svg("hello", module_size=0)


# ---- HostInviteServer ----


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _http_get(url: str, *, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


def _make_server(tmp_path, *, clock=None, cache_enabled=False, host_name="Studio-PC"):
    caps = {
        "speech": True,
        "text_cleanup": True,
        "fast_judgments": True,
        "walkthrough_writeup": False,
        "meeting_ai": True,
        "records": True,
        "agent_ask": False,
    }
    cache_flag = {"enabled": cache_enabled}

    def host_info():
        return {
            "host_name": host_name,
            "version": VERSION,
            "address": "127.0.0.1",
            "port": 47821,
            "fingerprint": FP,
            "alternates": ["100.64.0.9"],
            "engine": {"label": "Parakeet V3", "device": "cuda", "available": True},
            "capabilities": caps,
        }

    srv = invite.HostInviteServer(
        host_info_provider=host_info,
        pairing_code_provider=lambda: "654321",
        cache_enabled_provider=lambda: cache_flag["enabled"],
        cache_dir=tmp_path / "cache",
        clock=clock or _FakeClock(),
    )
    port = srv.start(port=0, bind="127.0.0.1")
    return srv, port, cache_flag


def test_join_page_serves_capabilities_os_buttons_and_security_headers(tmp_path):
    clock = _FakeClock()
    srv, port, _cache_flag = _make_server(tmp_path, clock=clock)
    try:
        session = srv.create_invite(ttl_seconds=900)
        url = srv.invite_url("127.0.0.1", session.token)
        assert url == f"http://127.0.0.1:{port}/join/{session.token}"

        # Windows User-Agent gets Windows primary button + all 4 platform links.
        status, headers, raw = _http_get(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        assert status == 200
        body = raw.decode("utf-8")
        assert "Connect to Studio-PC" in body
        assert "Parakeet V3 (cuda)" in body
        assert "AI Cleanup &amp; Rewrites" in body
        assert "Meeting AI &amp; Speaker ID" in body
        assert f"OpenWhisper-Client-Setup-{VERSION}.exe" in body
        assert f"OpenWhisper-Client-{VERSION}-macos-arm64.dmg" in body
        assert f"OpenWhisper-Client-{VERSION}-linux-amd64.deb" in body
        assert f"OpenWhisper-Client-{VERSION}-linux-x86_64.pkg.tar.zst" in body
        assert "Download OpenWhisper Client for Windows (.exe)" in body
        assert "openwhisper://pair?" in body
        assert "654 321" in body
        assert f"/invite/{session.token}.openwhisper-invite" in body
        assert "data:image/svg+xml;base64," in body

        # Security headers strictly enforced.
        lower_headers = {k.lower(): v for k, v in headers.items()}
        assert lower_headers["x-content-type-options"] == "nosniff"
        assert lower_headers["referrer-policy"] == "no-referrer"
        assert lower_headers["x-frame-options"] == "DENY"
        assert lower_headers["cache-control"] == "no-store"
        csp = lower_headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp

        # macOS User-Agent switches primary CTA to macOS DMG.
        status_mac, _h, raw_mac = _http_get(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"}
        )
        assert status_mac == 200
        assert "Download OpenWhisper Client for macOS (.dmg)" in raw_mac.decode("utf-8")

        # Downloadable .openwhisper-invite file validates cleanly.
        inv_status, _inv_h, inv_raw = _http_get(
            f"http://127.0.0.1:{port}/invite/{session.token}.openwhisper-invite"
        )
        assert inv_status == 200
        parsed = invite.decode_invite_file(inv_raw.decode("utf-8"))
        assert parsed.host == "127.0.0.1"
        assert parsed.port == 47821
        assert parsed.fingerprint == FP
        assert parsed.code == "654321"
        assert "100.64.0.9" in parsed.alternates
    finally:
        srv.stop()


def test_join_page_escapes_html_in_host_name(tmp_path):
    srv, _port, _flag = _make_server(tmp_path, host_name='<script>alert("xss")</script>')
    try:
        session = srv.create_invite()
        status, _h, raw = _http_get(srv.invite_url("127.0.0.1", session.token))
        assert status == 200
        body = raw.decode("utf-8")
        assert "<script>alert" not in body
        assert "&lt;script&gt;alert" in body
    finally:
        srv.stop()


def test_join_page_ignores_untrusted_host_header_and_promotes_known_tailscale(tmp_path):
    srv, port, _flag = _make_server(tmp_path)
    try:
        session = srv.create_invite()
        inv_url = f"http://127.0.0.1:{port}/invite/{session.token}.openwhisper-invite"

        # Untrusted Host header must never appear in host or alt.
        status, _h, raw = _http_get(inv_url, headers={"Host": f"evil.example.com:{port}"})
        assert status == 200
        parsed = invite.decode_invite_file(raw.decode("utf-8"))
        assert parsed.host == "127.0.0.1"
        assert "evil.example.com" not in parsed.alternates

        # Known alternate (Tailscale 100.64.0.9) is safely promoted to primary host.
        status_ts, _h2, raw_ts = _http_get(inv_url, headers={"Host": f"100.64.0.9:{port}"})
        assert status_ts == 200
        parsed_ts = invite.decode_invite_file(raw_ts.decode("utf-8"))
        assert parsed_ts.host == "100.64.0.9"
        assert "127.0.0.1" in parsed_ts.alternates
    finally:
        srv.stop()


def test_invite_token_rejects_unknown_expired_and_revoked(tmp_path):
    clock = _FakeClock(1000.0)
    srv, port, _flag = _make_server(tmp_path, clock=clock)
    try:
        # Unknown token -> 404.
        status, headers, _ = _http_get(f"http://127.0.0.1:{port}/join/nonexistent_token_123456")
        assert status == 404
        assert {k.lower(): v for k, v in headers.items()}["x-content-type-options"] == "nosniff"

        # Valid token expires after TTL -> 410 Gone.
        s1 = srv.create_invite(ttl_seconds=60)
        status, _, _ = _http_get(srv.invite_url("127.0.0.1", s1.token))
        assert status == 200
        clock.advance(61.0)
        status, _, _ = _http_get(srv.invite_url("127.0.0.1", s1.token))
        assert status == 410

        # Explicit revoke -> 410 Gone immediately.
        s2 = srv.create_invite(ttl_seconds=900)
        assert srv.revoke_invite(s2.token) is True
        status, _, _ = _http_get(srv.invite_url("127.0.0.1", s2.token))
        assert status == 410
    finally:
        srv.stop()


def test_cached_installer_fetch_verify_serve_and_traversal_blocking(tmp_path):
    asset_name = f"OpenWhisper-Client-Setup-{VERSION}.exe"
    payload = b"MZ-openwhisper-client-installer-binary-payload"
    good_sha = hashlib.sha256(payload).hexdigest()
    sums_text = f"{good_sha}  {asset_name}\n"

    class _FakeResp(io.BytesIO):
        def __init__(self, data: bytes):
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_opener(url: str):
        if url.endswith("/SHA256SUMS.txt"):
            return _FakeResp(sums_text.encode("utf-8"))
        if url.endswith("/" + asset_name):
            return _FakeResp(payload)
        raise AssertionError(f"Unexpected URL: {url}")

    cache_dir = tmp_path / "cache"
    cached_path = invite.fetch_and_verify_client_installer(
        asset_name, version=VERSION, cache_dir=cache_dir, opener=fake_opener
    )
    assert cached_path.is_file()
    assert cached_path.read_bytes() == payload

    # Tampered download is rejected and not left in cache.
    bad_dir = tmp_path / "bad_cache"

    def bad_opener(url: str):
        if url.endswith("/SHA256SUMS.txt"):
            return _FakeResp(sums_text.encode("utf-8"))
        return _FakeResp(b"corrupted-bytes")

    with pytest.raises(invite.InviteValidationError, match="SHA-256 mismatch"):
        invite.fetch_and_verify_client_installer(
            asset_name, version=VERSION, cache_dir=bad_dir, opener=bad_opener
        )
    assert not (bad_dir / asset_name).exists()

    # Disallowed filename rejected before network access.
    with pytest.raises(invite.InviteValidationError):
        invite.fetch_and_verify_client_installer(
            "../../etc/passwd", version=VERSION, cache_dir=cache_dir, opener=fake_opener
        )

    # Server refuses /download when cache_enabled=False, serves when True.
    srv, port, cache_flag = _make_server(tmp_path, cache_enabled=False)
    try:
        session = srv.create_invite()
        dl_url = f"http://127.0.0.1:{port}/download/{session.token}/{asset_name}"
        status, _, _ = _http_get(dl_url)
        assert status == 404

        cache_flag["enabled"] = True
        # Join page now links Windows installer to /download/<token>/<asset>.
        j_status, _, j_raw = _http_get(srv.invite_url("127.0.0.1", session.token))
        assert j_status == 200
        assert f"/download/{session.token}/{asset_name}" in j_raw.decode("utf-8")

        status, headers, body = _http_get(dl_url)
        assert status == 200
        assert body == payload
        assert {k.lower(): v for k, v in headers.items()}["x-content-type-options"] == "nosniff"

        # Path traversal and non-allowlisted filenames are strictly blocked.
        for bad_path in (
            f"/download/{session.token}/../secret.txt",
            f"/download/{session.token}/%2e%2e/secret.txt",
            f"/download/{session.token}/SHA256SUMS.txt",
            f"/download/{session.token}/evil.exe",
        ):
            bad_status, _, _ = _http_get(f"http://127.0.0.1:{port}{bad_path}")
            assert bad_status == 404, bad_path

        # If cached file is tampered on disk, server refuses to stream it.
        cached_path.write_bytes(b"tampered")
        tampered_status, _, _ = _http_get(dl_url)
        assert tampered_status == 404
    finally:
        srv.stop()
