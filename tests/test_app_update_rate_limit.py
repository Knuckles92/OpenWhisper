"""Public GitHub release fallback, verified assets, and persisted retry limits."""

import io
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from services import app_update
from services.settings import SettingsKey, SettingsManager

TAG = "v2.7.0"
PAGE = f"{app_update.RELEASES_PAGE_URL}/tag/{TAG}"
BASE = f"https://github.com/{app_update.GITHUB_REPO}/releases/download/{TAG}/"
NAMES = (
    "OpenWhisper-Setup-2.7.0.exe",
    "OpenWhisper-2.7.0-win64.tar.xz",
    "OpenWhisper-2.7.0-macos-arm64.dmg",
)
DIGEST = "ab" * 32
CHECKSUMS = "".join(f"{DIGEST}  {name}\n" for name in NAMES).encode()


class Response(io.BytesIO):
    def __init__(self, body=b"", *, url=PAGE, size=123, status=200):
        super().__init__(body)
        self.url = url
        self.status = status
        self.headers = {"Content-Length": str(size)} if size is not None else {}

    def geturl(self):
        return self.url


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    monkeypatch.setattr(app_update, "settings_manager", manager)
    monkeypatch.delenv(app_update.UPDATE_FEED_ENV, raising=False)
    return manager


@pytest.fixture
def network(monkeypatch, manager):
    state = {
        "calls": [],
        "checksums": CHECKSUMS,
        "page": PAGE,
        "size": 123,
        "status": 200,
    }
    state["error"] = urllib.error.HTTPError(
        app_update.LATEST_RELEASE_URL,
        403,
        "Forbidden",
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(
                int((datetime.now(UTC) + timedelta(minutes=30)).timestamp())
            ),
        },
        None,
    )

    def open_url(url, extra_headers=None, *, method="GET"):
        state["calls"].append((url, method))
        if url == app_update.LATEST_RELEASE_URL:
            raise state["error"]
        if url == app_update.LATEST_RELEASE_PAGE_URL:
            return Response(url=state["page"])
        if url == BASE + "SHA256SUMS.txt":
            if isinstance(state["checksums"], Exception):
                raise state["checksums"]
            return Response(state["checksums"])
        assert url in {BASE + name for name in NAMES}
        return Response(size=state["size"], status=state["status"])

    monkeypatch.setattr(app_update, "_open", open_url)
    return state


def test_rate_limited_check_keeps_verified_mac_install_available(
    network, manager, monkeypatch
):
    monkeypatch.setattr(
        app_update, "detect_channel", lambda: app_update.InstallChannel.INSTALLER
    )
    monkeypatch.setattr(app_update, "__version__", "2.6.0")
    monkeypatch.setattr(app_update.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    result = app_update.check_for_update()
    assert result.status == app_update.UpdateStatus.UPDATE_AVAILABLE
    assert result.can_apply and result.apply_mode == app_update.ApplyMode.MACOS_DMG
    assert result.release.html_url == PAGE
    assert result.release.macos_asset.sha256 == DIGEST
    assert result.release.macos_asset.size_bytes == 123
    assert result.release.macos_asset.url == BASE + NAMES[2]
    assert SettingsKey.UPDATE_LAST_CHECK_AT in manager.load_all_settings()
    # Package bodies are never downloaded during an update check.
    assert network["calls"] == [
        (app_update.LATEST_RELEASE_URL, "GET"),
        (app_update.LATEST_RELEASE_PAGE_URL, "HEAD"),
        (BASE + "SHA256SUMS.txt", "GET"),
        *((BASE + name, "HEAD") for name in NAMES),
    ]


def test_fallback_retains_verified_windows_assets(network):
    release = app_update.fetch_latest_release()
    assert release.setup_asset.sha256 == DIGEST
    assert release.native_asset.sha256 == DIGEST
    assert (
        app_update.resolve_release_apply_mode(
            app_update.InstallChannel.INSTALLER,
            release,
            platform_name="win32",
        )
        == app_update.ApplyMode.SETUP
    )


def test_primary_api_cooldown_survives_reloading_settings(
    network, manager, monkeypatch
):
    app_update.fetch_latest_release()
    retry_at = app_update.parse_last_check_at(
        manager.load_all_settings()[SettingsKey.UPDATE_API_RETRY_AT]
    )
    assert retry_at.timestamp() >= int(network["error"].headers["X-RateLimit-Reset"])
    monkeypatch.setattr(
        app_update, "settings_manager", SettingsManager(manager.settings_file)
    )
    app_update.fetch_latest_release()
    assert network["calls"].count((app_update.LATEST_RELEASE_URL, "GET")) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "120"},
        {"Retry-After": "invalid", "X-RateLimit-Reset": "invalid"},
    ],
)
def test_secondary_rate_limit_sets_cooldown_and_uses_fallback(
    network, manager, headers
):
    before = datetime.now(UTC)
    network["error"] = urllib.error.HTTPError(
        app_update.LATEST_RELEASE_URL, 429, "Limited", headers, None
    )
    assert app_update.fetch_latest_release().version == "2.7.0"
    retry_at = app_update.parse_last_check_at(
        manager.load_all_settings()[SettingsKey.UPDATE_API_RETRY_AT]
    )
    assert retry_at >= before + timedelta(
        seconds=120 if headers["Retry-After"] == "120" else 60
    )


def test_expired_api_cooldown_allows_api_retry(network, manager):
    manager.save_setting(
        SettingsKey.UPDATE_API_RETRY_AT,
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    app_update.fetch_latest_release()
    assert network["calls"][0] == (app_update.LATEST_RELEASE_URL, "GET")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(
            app_update.LATEST_RELEASE_URL, 403, "Forbidden", {}, None
        ),
        urllib.error.HTTPError(
            app_update.LATEST_RELEASE_URL, 500, "Server Error", {}, None
        ),
        urllib.error.URLError("offline"),
    ],
)
def test_other_api_errors_do_not_start_rate_limit_fallback(network, error):
    network["error"] = error
    with pytest.raises(app_update.AppUpdateError):
        app_update.fetch_latest_release()
    assert network["calls"] == [(app_update.LATEST_RELEASE_URL, "GET")]


@pytest.mark.parametrize(
    "checksums",
    [
        b"",
        b"garbage",
        CHECKSUMS + CHECKSUMS,
        b"\xff",
        b"x" * (app_update._MAX_CHECKSUM_BYTES + 1),
        urllib.error.HTTPError(BASE + "SHA256SUMS.txt", 404, "Not Found", {}, None),
        (f"{DIGEST}  ../../update.exe\n").encode(),
        (f"{DIGEST}  OpenWhisper-Setup-2.6.0.exe\n").encode(),
    ],
)
def test_missing_or_untrusted_checksums_allow_notification_only(network, checksums):
    network["checksums"] = checksums
    release = app_update.fetch_latest_release()
    assert release.version == "2.7.0"
    assert release.setup_asset is release.native_asset is release.macos_asset is None
    assert not app_update.can_apply(app_update.InstallChannel.INSTALLER, release)
    assert len(network["calls"]) == 3


@pytest.mark.parametrize(
    "size,status",
    [
        (None, 200),
        ("bad", 200),
        (0, 200),
        (-1, 200),
        (app_update._MAX_ASSET_BYTES + 1, 200),
        (123, 206),
    ],
)
def test_unknown_or_invalid_asset_sizes_never_enable_install(network, size, status):
    network.update(size=size, status=status)
    release = app_update.fetch_latest_release()
    assert release.setup_asset is release.native_asset is release.macos_asset is None


@pytest.mark.parametrize(
    "page",
    [
        app_update.LATEST_RELEASE_PAGE_URL,
        PAGE + "-rc.1",
        PAGE.replace("Knuckles92", "attacker"),
        PAGE + "?redirect=1",
        PAGE.replace("github.com", "example.com"),
    ],
)
def test_fallback_requires_exact_repository_stable_tag(network, page):
    network["page"] = page
    with pytest.raises(app_update.AppUpdateError, match="stable release"):
        app_update.fetch_latest_release()
    assert len(network["calls"]) == 2


def test_failed_checks_throttle_auto_retry_without_claiming_success(
    manager, monkeypatch
):
    monkeypatch.setattr(
        app_update,
        "fetch_latest_release",
        lambda: (_ for _ in ()).throw(app_update.AppUpdateError("offline")),
    )
    with pytest.raises(app_update.AppUpdateError):
        app_update.check_for_update()
    settings = manager.load_all_settings()
    assert SettingsKey.UPDATE_LAST_CHECK_AT not in settings
    failed_at = app_update.parse_last_check_at(
        settings[SettingsKey.UPDATE_LAST_FAILED_CHECK_AT]
    )
    assert not app_update.should_auto_check(
        settings, now=failed_at + timedelta(minutes=59)
    )
    assert app_update.should_auto_check(settings, now=failed_at + timedelta(hours=1))


def test_loopback_feed_bypasses_production_cooldown(manager, monkeypatch):
    url = "http://127.0.0.1:8765/releases/latest"
    manager.save_setting(
        SettingsKey.UPDATE_API_RETRY_AT,
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setenv(app_update.UPDATE_FEED_ENV, url)
    calls = []

    def open_url(actual, **kwargs):
        calls.append(actual)
        return Response(b'{"tag_name": "v2.8.0"}')

    monkeypatch.setattr(app_update, "_open", open_url)
    assert app_update.fetch_latest_release().version == "2.8.0"
    assert calls == [url]


def test_head_redirect_preserves_method_and_trust_checks():
    handler = app_update._SafeRedirectHandler(app_update.LATEST_RELEASE_PAGE_URL)
    request = urllib.request.Request(app_update.LATEST_RELEASE_PAGE_URL, method="HEAD")
    redirected = handler.redirect_request(request, None, 302, "Found", {}, PAGE)
    assert redirected.get_method() == "HEAD"
    with pytest.raises(urllib.error.URLError, match="unsafe URL"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://example.com/download"
        )
