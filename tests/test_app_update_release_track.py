"""The opt-in beta release track: who is offered GitHub prereleases, and how."""

import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from services import app_update
from services.app_update import (
    AppUpdateError,
    ApplyMode,
    InstallChannel,
    ReleaseTrack,
    UpdateStatus,
    parse_release_payload,
    select_newest_release,
)
from services.settings import SettingsKey, SettingsManager
from tests.fakes.http import Response

DIGEST = "cd" * 32
DOWNLOAD = f"https://github.com/{app_update.GITHUB_REPO}/releases/download"


def _release(version, *, prerelease=False, draft=False, digest=DIGEST, url=None):
    tag = f"v{version}"
    name = f"OpenWhisper-Setup-{version}.exe"
    return {
        "tag_name": tag,
        "html_url": f"{app_update.RELEASES_PAGE_URL}/tag/{tag}",
        "body": f"Notes for {version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "id": 1000,
                "name": name,
                "size": 4096,
                "state": "uploaded",
                "digest": f"sha256:{digest}" if digest else None,
                "browser_download_url": url or f"{DOWNLOAD}/{tag}/{name}",
            }
        ],
    }


STABLE = _release("2.8.9")
BETA = _release("2.9.0", prerelease=True)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    monkeypatch.setattr(app_update, "settings_manager", manager)
    monkeypatch.delenv(app_update.UPDATE_FEED_ENV, raising=False)
    return manager


@pytest.fixture
def github(monkeypatch, manager):
    """GitHub as seen by the updater: ``latest`` is stable, the list has all."""
    state = {"latest": STABLE, "list": [BETA, STABLE], "calls": [], "error": None}

    def open_url(url, extra_headers=None, *, method="GET"):
        state["calls"].append(url)
        if state["error"] is not None and url in (
            app_update.LATEST_RELEASE_URL,
            app_update.RELEASE_LIST_URL,
        ):
            raise state["error"]
        if url == app_update.LATEST_RELEASE_URL:
            return Response(json.dumps(state["latest"]).encode(), url=url)
        if url == app_update.RELEASE_LIST_URL:
            return Response(json.dumps(state["list"]).encode(), url=url)
        # API-free fallback: the latest redirect names the stable tag only.
        if url == app_update.LATEST_RELEASE_PAGE_URL:
            tag = state["latest"]["tag_name"]
            return Response(url=f"{app_update.RELEASES_PAGE_URL}/tag/{tag}")
        if url.endswith("/SHA256SUMS.txt"):
            tag = state["latest"]["tag_name"]
            name = f"OpenWhisper-Setup-{tag[1:]}.exe"
            return Response(f"{DIGEST}  {name}\n".encode(), url=url)
        if url.startswith(DOWNLOAD):
            return Response(url=url, headers={"Content-Length": "4096"})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(app_update, "_open", open_url)
    return state


def _rate_limit_error(url):
    reset = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp())
    return urllib.error.HTTPError(
        url,
        403,
        "Forbidden",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        None,
    )


def _opt_in(manager, enabled=True):
    manager.save_setting(SettingsKey.UPDATE_BETA_TRACK_ENABLED, enabled)


class TestReleaseTrackSetting:
    def test_default_is_stable(self, manager):
        assert app_update.release_track() == ReleaseTrack.STABLE

    def test_opt_in_selects_beta(self, manager):
        _opt_in(manager)
        assert app_update.release_track() == ReleaseTrack.BETA

    def test_non_bool_value_is_stable(self):
        assert (
            app_update.release_track({SettingsKey.UPDATE_BETA_TRACK_ENABLED: "yes"})
            == ReleaseTrack.STABLE
        )

    def test_unreadable_settings_are_stable(self, monkeypatch):
        class Broken:
            def load_all_settings(self):
                raise OSError("disk")

        monkeypatch.setattr(app_update, "settings_manager", Broken())
        assert app_update.release_track() == ReleaseTrack.STABLE


class TestStableTrackNeverSeesPrereleases:
    def test_stable_reads_only_latest_endpoint(self, github, manager):
        release = app_update.fetch_latest_release()
        assert release.version == "2.8.9"
        assert release.prerelease is False
        assert github["calls"] == [app_update.LATEST_RELEASE_URL]

    def test_stable_ignores_a_newer_prerelease(self, github, manager, monkeypatch):
        monkeypatch.setattr(app_update, "detect_channel", lambda: InstallChannel.SOURCE)
        monkeypatch.setattr(app_update, "__version__", "2.8.9")
        result = app_update.check_for_update(persist=False)
        assert result.status == UpdateStatus.UP_TO_DATE
        assert result.release_track == ReleaseTrack.STABLE
        assert app_update.RELEASE_LIST_URL not in github["calls"]

    def test_prerelease_payload_is_not_installable_off_the_beta_track(self):
        release = parse_release_payload(BETA)
        assert release.prerelease is True
        assert release.setup_asset is None

    def test_drafts_are_never_installable(self):
        draft = _release("3.0.0", draft=True, prerelease=True)
        assert parse_release_payload(draft, allow_prerelease=True).setup_asset is None
        assert parse_release_payload(draft).setup_asset is None


class TestBetaTrackSelection:
    def test_newer_prerelease_wins(self):
        release = select_newest_release([STABLE, BETA])
        assert release.version == "2.9.0"
        assert release.prerelease is True
        assert release.setup_asset.sha256 == DIGEST

    def test_newer_stable_wins_whatever_the_list_order(self):
        newer_stable = _release("2.9.1")
        for order in ([BETA, newer_stable], [newer_stable, BETA]):
            release = select_newest_release(order)
            assert release.version == "2.9.1"
            assert release.prerelease is False

    def test_drafts_and_suffixed_tags_are_skipped(self):
        draft = _release("3.0.0", draft=True)
        suffixed = _release("3.0.0", prerelease=True)
        suffixed["tag_name"] = "v3.0.0-rc.1"
        assert select_newest_release([draft, suffixed, BETA]).version == "2.9.0"

    def test_stable_wins_a_version_tie(self):
        # The 2.6 line used two-digit patches: v2.6.01 and v2.6.1 compare equal.
        legacy_beta = _release("2.6.01", prerelease=True)
        stable = _release("2.6.1")
        release = select_newest_release([legacy_beta, stable])
        assert release.tag_name == "v2.6.1"
        assert release.prerelease is False

    @pytest.mark.parametrize("payload", [[], {"message": "x"}, [{"draft": True}]])
    def test_no_usable_release_is_an_error(self, payload):
        with pytest.raises(AppUpdateError):
            select_newest_release(payload)

    def test_prerelease_assets_keep_url_allow_listing(self):
        hostile = _release(
            "2.9.0", prerelease=True, url="https://example.com/OpenWhisper-Setup-2.9.0.exe"
        )
        assert select_newest_release([hostile]).setup_asset is None

    def test_prerelease_without_digest_is_notify_only(self):
        release = select_newest_release([_release("2.9.0", prerelease=True, digest=None)])
        assert release.setup_asset.sha256 is None
        assert (
            app_update.resolve_release_apply_mode(
                InstallChannel.INSTALLER, release, platform_name="win32"
            )
            == ApplyMode.NOTIFY_ONLY
        )

    def test_verified_prerelease_can_be_applied(self):
        release = select_newest_release([BETA])
        assert (
            app_update.resolve_release_apply_mode(
                InstallChannel.INSTALLER, release, platform_name="win32"
            )
            == ApplyMode.SETUP
        )

    def test_beta_reads_the_release_list(self, github, manager):
        _opt_in(manager)
        release = app_update.fetch_latest_release()
        assert release.version == "2.9.0"
        assert github["calls"] == [app_update.RELEASE_LIST_URL]


class TestBetaRateLimitFallback:
    def test_rate_limited_beta_check_falls_back_to_stable(self, github, manager):
        _opt_in(manager)
        github["error"] = _rate_limit_error(app_update.RELEASE_LIST_URL)
        release = app_update.fetch_latest_release()
        # GitHub has no API-free prerelease pointer: the newest stable, verified.
        assert release.version == "2.8.9"
        assert release.prerelease is False
        assert release.setup_asset.sha256 == DIGEST
        assert github["calls"][:2] == [
            app_update.RELEASE_LIST_URL,
            app_update.LATEST_RELEASE_PAGE_URL,
        ]
        assert SettingsKey.UPDATE_API_RETRY_AT in manager.load_all_settings()

    def test_cooldown_skips_the_api_until_it_expires(self, github, manager):
        _opt_in(manager)
        github["error"] = _rate_limit_error(app_update.RELEASE_LIST_URL)
        app_update.fetch_latest_release()
        github["calls"].clear()
        github["error"] = None
        assert app_update.fetch_latest_release().version == "2.8.9"
        assert app_update.RELEASE_LIST_URL not in github["calls"]

        manager.save_setting(
            SettingsKey.UPDATE_API_RETRY_AT,
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        assert app_update.fetch_latest_release().version == "2.9.0"

    def test_other_http_errors_are_not_masked(self, github, manager):
        _opt_in(manager)
        github["error"] = urllib.error.HTTPError(
            app_update.RELEASE_LIST_URL, 500, "Server Error", {}, None
        )
        with pytest.raises(AppUpdateError):
            app_update.fetch_latest_release()
        assert app_update.LATEST_RELEASE_PAGE_URL not in github["calls"]


class TestSwitchingTracks:
    @pytest.fixture(autouse=True)
    def _source_copy(self, monkeypatch):
        monkeypatch.setattr(app_update, "detect_channel", lambda: InstallChannel.SOURCE)

    def _check(self, monkeypatch, version):
        monkeypatch.setattr(app_update, "__version__", version)
        return app_update.check_for_update(persist=False)

    def test_join_then_leave(self, github, manager, monkeypatch):
        assert self._check(monkeypatch, "2.8.9").status == UpdateStatus.UP_TO_DATE

        _opt_in(manager)
        joined = self._check(monkeypatch, "2.8.9")
        assert joined.status == UpdateStatus.UPDATE_AVAILABLE
        assert joined.release.version == "2.9.0"
        assert joined.release_track == ReleaseTrack.BETA

        # After installing the beta and leaving the track, nothing is offered:
        # no downgrade to 2.8.9, and no further prereleases.
        _opt_in(manager, False)
        github["list"] = [_release("2.9.1", prerelease=True), BETA, STABLE]
        left = self._check(monkeypatch, "2.9.0")
        assert left.status == UpdateStatus.DEVELOPMENT
        assert left.release.version == "2.8.9"
        assert left.release_track == ReleaseTrack.STABLE

    def test_former_beta_user_rejoins_stable_on_promotion(
        self, github, manager, monkeypatch
    ):
        # The maintainer promotes the same 2.9.0 build: equal, so up to date.
        github["latest"] = _release("2.9.0")
        assert self._check(monkeypatch, "2.9.0").status == UpdateStatus.UP_TO_DATE
        # The next stable release is offered as usual.
        github["latest"] = _release("2.9.1")
        result = self._check(monkeypatch, "2.9.0")
        assert result.status == UpdateStatus.UPDATE_AVAILABLE
        assert result.release.version == "2.9.1"

    def test_switch_makes_the_next_automatic_check_due(self, manager):
        now = datetime.now(UTC)
        retry_at = (now + timedelta(minutes=5)).isoformat()
        manager.update_settings({
            SettingsKey.UPDATE_LAST_CHECK_AT: now.isoformat(),
            SettingsKey.UPDATE_API_RETRY_AT: retry_at,
        })
        assert app_update.seconds_until_auto_check(
            now=now, channel=InstallChannel.INSTALLER
        ) > 0
        app_update.release_track_changed()
        settings = manager.load_all_settings()
        assert SettingsKey.UPDATE_LAST_CHECK_AT not in settings
        # The rate-limit cooldown is kept: switching is no way around it.
        assert settings[SettingsKey.UPDATE_API_RETRY_AT] == retry_at
        assert (
            app_update.seconds_until_auto_check(
                settings, now=now, channel=InstallChannel.INSTALLER
            )
            == 0
        )


class TestLoopbackFeed:
    def test_feed_prerelease_installable_only_on_beta(self, monkeypatch, manager):
        feed = "http://127.0.0.1:8765/releases/latest"
        monkeypatch.setenv(app_update.UPDATE_FEED_ENV, feed)
        name = "OpenWhisper-Setup-2.9.0.exe"
        payload = dict(BETA, assets=[dict(
            BETA["assets"][0], browser_download_url=f"http://127.0.0.1:8765/download/{name}"
        )])
        monkeypatch.setattr(
            app_update,
            "_open",
            lambda url, extra_headers=None, method="GET": Response(
                json.dumps(payload).encode(), url=url
            ),
        )
        assert app_update.fetch_latest_release(ReleaseTrack.STABLE).setup_asset is None
        beta = app_update.fetch_latest_release(ReleaseTrack.BETA)
        assert beta.setup_asset is not None and beta.prerelease


def test_soak_feed_can_serve_a_prerelease(tmp_path):
    from scripts.serve_update_feed import build_payload

    (tmp_path / "OpenWhisper-Setup-2.9.0.exe").write_bytes(b"setup")
    base = "http://127.0.0.1:8765"
    assert build_payload(tmp_path, "2.9.0", base, "n")["prerelease"] is False
    payload = build_payload(tmp_path, "2.9.0", base, "n", prerelease=True)
    assert payload["prerelease"] is True
    assert parse_release_payload(payload).setup_asset is None
