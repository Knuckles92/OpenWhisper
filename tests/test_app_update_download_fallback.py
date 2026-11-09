"""Recover release-download gateway failures through the same GitHub asset."""

import hashlib
import io
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from services import app_update

BODY = b"verified release installer"
ASSET_ID = 123456
API_URL = f"https://api.github.com/repos/{app_update.GITHUB_REPO}/releases/assets/{ASSET_ID}"
CDN_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?signature=test"


class Response(io.BytesIO):
    def __init__(self, body=BODY, *, status=200, headers=None, url=CDN_URL):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self.url = url

    def geturl(self):
        return self.url


@pytest.fixture
def asset():
    name = "OpenWhisper-2.8.6-macos-arm64.dmg"
    return app_update.ReleaseAsset(
        url=f"https://github.com/{app_update.GITHUB_REPO}/releases/download/v2.8.6/{name}",
        name=name,
        size_bytes=len(BODY),
        sha256=hashlib.sha256(BODY).hexdigest(),
        github_asset_id=ASSET_ID,
    )


def download(asset, tmp_path, cancel=None):
    return app_update.download_release_asset(
        asset, destination_dir=str(tmp_path), cancel=cancel
    )


@pytest.mark.parametrize("kind,name", [
    ("macos_asset", "OpenWhisper-2.8.6-macos-arm64.dmg"),
    ("setup_asset", app_update.setup_asset_name("2.8.6")),
    ("native_asset", app_update.archive_asset_name("2.8.6")),
])
def test_release_metadata_retains_asset_id_for_each_installer(kind, name):
    release = app_update.parse_release_payload({
        "tag_name": "v2.8.6",
        "assets": [{
            "id": ASSET_ID,
            # The API URL is constructed from the ID, never taken from this field.
            "url": "https://evil.example/download",
            "name": name,
            "size": len(BODY),
            "digest": "sha256:" + hashlib.sha256(BODY).hexdigest(),
            "browser_download_url": app_update._expected_asset_url("v2.8.6", name),
        }],
    })
    assert getattr(release, kind).github_asset_id == ASSET_ID


@pytest.mark.parametrize("asset_id", [None, True, False, 0, -1, "123", "../latest", 1.5])
def test_invalid_asset_ids_do_not_enable_fallback(asset, asset_id):
    parsed = app_update._parse_named_asset(
        [{"id": asset_id, "name": asset.name, "size": len(BODY),
          "browser_download_url": asset.url}],
        expected_name=asset.name, tag_name="v2.8.6",
    )
    assert parsed.github_asset_id is None


def test_loopback_feed_never_enables_production_fallback(asset, monkeypatch):
    monkeypatch.setenv(app_update.UPDATE_FEED_ENV, "http://127.0.0.1:8765/releases/latest")
    parsed = app_update._parse_named_asset(
        [{"id": ASSET_ID, "name": asset.name, "size": len(BODY),
          "browser_download_url": f"http://127.0.0.1:8765/{asset.name}"}],
        expected_name=asset.name, tag_name="v2.8.6",
    )
    assert parsed.github_asset_id is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_gateway_error_downloads_and_verifies_via_api(asset, tmp_path, monkeypatch, status):
    calls = []
    error_body = io.BytesIO(b"gateway error")

    def open_url(url, headers=None):
        calls.append((url, headers))
        if url == asset.url:
            raise urllib.error.HTTPError(url, status, "Gateway failure", {}, error_body)
        return Response()

    monkeypatch.setattr(app_update, "_open", open_url)
    path = download(asset, tmp_path)
    assert (tmp_path / asset.name).read_bytes() == BODY
    assert path == str(tmp_path / asset.name)
    assert calls == [(asset.url, None), (API_URL, {"Accept": "application/octet-stream"})]
    assert error_body.closed
    assert not (tmp_path / (asset.name + ".part")).exists()


def test_successful_primary_download_does_not_call_api(asset, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app_update, "_open", lambda *args: calls.append(args) or Response())
    download(asset, tmp_path)
    assert calls == [(asset.url, None)]


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("url", 403, "Forbidden", {}, None),
    urllib.error.HTTPError("url", 404, "Not found", {}, None),
    urllib.error.HTTPError("url", 429, "Rate limited", {}, None),
    urllib.error.URLError("unsafe redirect or certificate failure"),
])
def test_other_failures_do_not_switch_routes(asset, tmp_path, monkeypatch, error):
    calls = []

    def fail(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(app_update, "_open", fail)
    with pytest.raises(app_update.AppUpdateError):
        download(asset, tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("asset_id,attempts", [(ASSET_ID, 2), (None, 1)])
def test_failures_are_bounded(asset, tmp_path, monkeypatch, asset_id, attempts):
    calls = []

    def fail(url, headers=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 504, "Gateway Time-out", {}, None)

    monkeypatch.setattr(app_update, "_open", fail)
    with pytest.raises(app_update.AppUpdateError, match="504"):
        download(replace(asset, github_asset_id=asset_id), tmp_path)
    assert len(calls) == attempts
    assert not (tmp_path / asset.name).exists()


def test_fallback_preserves_validated_range_resume(asset, tmp_path, monkeypatch):
    (tmp_path / (asset.name + ".part")).write_bytes(BODY[:8])
    calls = []

    def open_url(url, headers=None):
        calls.append((url, headers))
        if url == asset.url:
            raise urllib.error.HTTPError(url, 504, "Gateway Time-out", {}, None)
        return Response(BODY[8:], status=206, headers={
            "Content-Range": f"bytes 8-{len(BODY) - 1}/{len(BODY)}",
            "Content-Length": str(len(BODY) - 8),
        })

    monkeypatch.setattr(app_update, "_open", open_url)
    download(asset, tmp_path)
    assert calls == [
        (asset.url, {"Range": "bytes=8-"}),
        (API_URL, {"Range": "bytes=8-", "Accept": "application/octet-stream"}),
    ]
    assert (tmp_path / asset.name).read_bytes() == BODY


def test_range_restart_stays_on_api(asset, tmp_path, monkeypatch):
    (tmp_path / (asset.name + ".part")).write_bytes(BODY[:8])
    calls = []

    def open_url(url, headers=None):
        calls.append((url, headers))
        if url == asset.url or "Range" in headers:
            raise urllib.error.HTTPError(url, 504 if url == asset.url else 416, "Error", {}, None)
        return Response()

    monkeypatch.setattr(app_update, "_open", open_url)
    download(asset, tmp_path)
    assert [url for url, _ in calls] == [asset.url, API_URL, API_URL]
    assert calls[-1][1] == {"Accept": "application/octet-stream"}
    assert (tmp_path / asset.name).read_bytes() == BODY


def test_cancel_after_gateway_error_stops_before_api(asset, tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def fail(url, headers=None):
        calls.append(url)
        cancel.set()
        raise urllib.error.HTTPError(url, 504, "Gateway Time-out", {}, None)

    monkeypatch.setattr(app_update, "_open", fail)
    with pytest.raises(app_update.AppUpdateError, match="canceled"):
        download(asset, tmp_path, cancel)
    assert calls == [asset.url]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body", [b"x" * len(BODY), b"too short"])
def test_fallback_cannot_bypass_digest_or_size(asset, tmp_path, monkeypatch, body):
    def open_url(url, headers=None):
        if url == asset.url:
            raise urllib.error.HTTPError(url, 504, "Gateway Time-out", {}, None)
        return Response(body)

    monkeypatch.setattr(app_update, "_open", open_url)
    with pytest.raises(app_update.AppUpdateError, match="integrity|size"):
        download(asset, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_asset_api_redirect_uses_existing_trust_checks(monkeypatch):
    def open_url(url, headers=None, **kwargs):
        handler = kwargs["handlers"][0]
        request = urllib.request.Request(url, headers=headers)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, CDN_URL)
        assert redirected.get_header("Accept") == "application/octet-stream"
        return Response(url=CDN_URL)

    monkeypatch.setattr(app_update, "open_url", open_url)
    with app_update._open(API_URL, {"Accept": "application/octet-stream"}) as response:
        assert response.read() == BODY


@pytest.mark.parametrize("initial,target", [
    (app_update.LATEST_RELEASE_URL, CDN_URL),
    (API_URL.replace(app_update.GITHUB_REPO, "other/repo"), CDN_URL),
    (API_URL + "?redirect=1", CDN_URL),
    (API_URL, "https://evil.example/file"),
    (API_URL, CDN_URL.replace("https:", "http:")),
    (API_URL, "https://release-assets.githubusercontent.com/other/file"),
    (API_URL, CDN_URL.replace(".com/", ".com:444/")),
    (API_URL, CDN_URL.replace("https://", "https://user@")),
])
def test_asset_api_does_not_broaden_other_redirects(initial, target):
    handler = app_update._SafeRedirectHandler(initial)
    with pytest.raises(urllib.error.URLError, match="unsafe URL"):
        handler.redirect_request(urllib.request.Request(initial), None, 302, "Found", {}, target)


def test_final_response_url_is_also_checked(monkeypatch):
    response = Response(url="https://evil.example/file")
    monkeypatch.setattr(app_update, "open_url", lambda *args, **kwargs: response)
    with pytest.raises(urllib.error.URLError, match="unsafe URL"):
        app_update._open(API_URL)
    assert response.closed
