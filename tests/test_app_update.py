"""Tests for channel detection, version compare, and GitHub release parsing."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from services import app_update
from services.app_update import (
    AppUpdateError,
    InstallChannel,
    ReleaseAsset,
    ReleaseInfo,
    UpdateStatus,
    can_apply,
    check_for_update,
    compare_versions,
    detect_channel,
    download_installer,
    normalize_version,
    parse_last_check_at,
    parse_release_payload,
    parse_version,
    persist_prompt_choices,
    should_auto_check,
    should_auto_notify,
    source_update_hint,
)
from services.settings import SettingsKey, SettingsManager


# Shape taken from the real /releases/latest payload for v2.1.1 (ids trimmed).
LATEST_RELEASE_FIXTURE = {
    "tag_name": "v2.1.1",
    "html_url": "https://github.com/Knuckles92/OpenWhisper/releases/tag/v2.1.1",
    "body": "### Fixed\n\n- GPU Acceleration component now activates immediately\n",
    "prerelease": False,
    "draft": False,
    "assets": [
        {
            "name": "OpenWhisper-Setup-2.1.1.exe",
            "size": 91717696,
            "digest": (
                "sha256:43d623f9f0a35d3ab7c8366d30bd721fe20c51bd"
                "94f3bc37b453c63fd5f8bd13"
            ),
            "browser_download_url": (
                "https://github.com/Knuckles92/OpenWhisper/releases/"
                "download/v2.1.1/OpenWhisper-Setup-2.1.1.exe"
            ),
        }
    ],
}


def _release(
    version: str = "2.2.0",
    *,
    sha256: str = "ab" * 32,
    url: str = "https://github.com/example/OpenWhisper-Setup-2.2.0.exe",
    size_bytes: int = 100,
) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag_name=f"v{version}",
        html_url=f"https://github.com/Knuckles92/OpenWhisper/releases/tag/v{version}",
        notes="notes",
        asset=ReleaseAsset(
            url=url,
            name=f"OpenWhisper-Setup-{version}.exe",
            size_bytes=size_bytes,
            sha256=sha256,
        ),
    )


class TestDetectChannel:
    def test_frozen_is_installer(self, tmp_path):
        with patch.object(app_update, "is_frozen", return_value=True):
            assert detect_channel(str(tmp_path)) == InstallChannel.INSTALLER

    def test_git_dir_is_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch.object(app_update, "is_frozen", return_value=False):
            assert detect_channel(str(tmp_path)) == InstallChannel.GIT

    def test_loose_tree_is_source(self, tmp_path):
        with patch.object(app_update, "is_frozen", return_value=False):
            assert detect_channel(str(tmp_path)) == InstallChannel.SOURCE


class TestCompareVersions:
    def test_equal_tag_is_up_to_date(self):
        assert compare_versions("2.1.1", "v2.1.1") == UpdateStatus.UP_TO_DATE

    def test_behind_is_update_available(self):
        assert compare_versions("2.1.1", "v2.2.0") == UpdateStatus.UPDATE_AVAILABLE

    def test_ahead_is_development(self):
        assert compare_versions("2.2.0", "v2.1.1") == UpdateStatus.DEVELOPMENT

    def test_prerelease_suffix_is_ignored_for_ordering(self):
        assert parse_version("2.2.0-rc.1") == (2, 2, 0)
        assert compare_versions("2.2.0-rc.1", "v2.2.0") == UpdateStatus.UP_TO_DATE

    def test_normalize_strips_v(self):
        assert normalize_version("v2.1.1") == "2.1.1"
        assert normalize_version("  V2.1.1  ") == "2.1.1"


class TestShouldAutoCheck:
    def test_disabled_never_checks(self):
        settings = {SettingsKey.UPDATE_CHECK_ENABLED: False}
        assert not should_auto_check(settings)

    def test_missing_timestamp_checks(self):
        settings = {SettingsKey.UPDATE_CHECK_ENABLED: True}
        assert should_auto_check(settings)

    def test_recent_check_is_throttled(self):
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        settings = {
            SettingsKey.UPDATE_CHECK_ENABLED: True,
            SettingsKey.UPDATE_LAST_CHECK_AT: now.isoformat(),
        }
        assert not should_auto_check(settings, now=now, interval_s=86400)

    def test_stale_check_runs_again(self):
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        earlier = now - timedelta(hours=25)
        settings = {
            SettingsKey.UPDATE_CHECK_ENABLED: True,
            SettingsKey.UPDATE_LAST_CHECK_AT: earlier.isoformat(),
        }
        assert should_auto_check(settings, now=now, interval_s=86400)

    def test_invalid_timestamp_is_treated_as_missing(self):
        settings = {
            SettingsKey.UPDATE_CHECK_ENABLED: True,
            SettingsKey.UPDATE_LAST_CHECK_AT: "not-a-date",
        }
        assert should_auto_check(settings)
        assert parse_last_check_at("not-a-date") is None


class TestShouldAutoNotify:
    def test_update_available_notifies_by_default(self):
        assert should_auto_notify(UpdateStatus.UPDATE_AVAILABLE, "2.2.0", {})

    def test_up_to_date_does_not_notify(self):
        assert not should_auto_notify(UpdateStatus.UP_TO_DATE, "2.1.1", {})

    def test_development_does_not_notify(self):
        assert not should_auto_notify(UpdateStatus.DEVELOPMENT, "2.1.1", {})

    def test_notify_off(self):
        settings = {SettingsKey.UPDATE_NOTIFY_ENABLED: False}
        assert not should_auto_notify(
            UpdateStatus.UPDATE_AVAILABLE, "2.2.0", settings
        )

    def test_check_off(self):
        settings = {SettingsKey.UPDATE_CHECK_ENABLED: False}
        assert not should_auto_notify(
            UpdateStatus.UPDATE_AVAILABLE, "2.2.0", settings
        )

    def test_skipped_version(self):
        settings = {SettingsKey.UPDATE_SKIPPED_VERSION: "2.2.0"}
        assert not should_auto_notify(
            UpdateStatus.UPDATE_AVAILABLE, "v2.2.0", settings
        )
        assert should_auto_notify(
            UpdateStatus.UPDATE_AVAILABLE, "2.3.0", settings
        )


class TestParseReleasePayload:
    def test_real_latest_shape(self):
        release = parse_release_payload(LATEST_RELEASE_FIXTURE)
        assert release.version == "2.1.1"
        assert release.tag_name == "v2.1.1"
        assert release.asset is not None
        assert release.asset.name == "OpenWhisper-Setup-2.1.1.exe"
        assert release.asset.sha256 == (
            "43d623f9f0a35d3ab7c8366d30bd721fe20c51bd94f3bc37b453c63fd5f8bd13"
        )
        assert release.asset.size_bytes == 91717696

    def test_sidecar_zip_asset_is_ignored(self):
        payload = json.loads(json.dumps(LATEST_RELEASE_FIXTURE))
        payload["assets"].insert(0, {
            "name": "meeting-agent-win_amd64-node22-pi1.zip",
            "size": 4_000_000,
            "digest": "sha256:" + ("ab" * 32),
            "browser_download_url": (
                "https://github.com/Knuckles92/OpenWhisper/releases/"
                "download/v2.1.1/meeting-agent-win_amd64-node22-pi1.zip"
            ),
        })
        release = parse_release_payload(payload)
        assert release.asset is not None
        assert release.asset.name == "OpenWhisper-Setup-2.1.1.exe"

    def test_missing_digest_allows_notify_but_not_apply(self):
        payload = json.loads(json.dumps(LATEST_RELEASE_FIXTURE))
        payload["assets"][0].pop("digest")
        release = parse_release_payload(payload)
        assert release.asset is not None
        assert release.asset.sha256 is None
        with patch.object(app_update, "detect_channel", return_value=InstallChannel.INSTALLER), patch.object(app_update.sys, "platform", "win32"):
            assert not can_apply(InstallChannel.INSTALLER, release)

    def test_missing_tag_raises(self):
        with pytest.raises(AppUpdateError):
            parse_release_payload({"assets": []})


class TestCanApplyAndHints:
    def test_installer_windows_with_digest(self):
        with patch.object(app_update.sys, "platform", "win32"):
            assert can_apply(InstallChannel.INSTALLER, _release())

    def test_git_never_applies(self):
        with patch.object(app_update.sys, "platform", "win32"):
            assert not can_apply(InstallChannel.GIT, _release())

    def test_macos_installer_does_not_apply(self):
        with patch.object(app_update.sys, "platform", "darwin"):
            assert not can_apply(InstallChannel.INSTALLER, _release())

    def test_git_hint_is_copy_paste(self):
        hint = source_update_hint(InstallChannel.GIT)
        assert hint is not None
        assert "git pull --ff-only" in hint
        assert "pip install -r requirements.txt" in hint
        assert source_update_hint(InstallChannel.INSTALLER) is None


class TestFetchLatestRelease:
    def test_requests_github_latest_not_the_website(self, monkeypatch):
        captured = {}

        class _Response:
            def read(self):
                return json.dumps(LATEST_RELEASE_FIXTURE).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _open(url, extra_headers=None):
            captured["url"] = url
            captured["headers"] = extra_headers
            return _Response()

        monkeypatch.setattr(app_update, "_open", _open)
        release = app_update.fetch_latest_release()
        assert captured["url"] == app_update.LATEST_RELEASE_URL
        assert "github.com" in captured["url"]
        assert "fiorilabs" not in captured["url"]
        assert release.version == "2.1.1"


class TestCheckForUpdate:
    def test_builds_result_and_persists_timestamp(self, tmp_path, monkeypatch):
        manager = SettingsManager(str(tmp_path / "settings.json"))
        monkeypatch.setattr(app_update, "settings_manager", manager)
        monkeypatch.setattr(
            app_update, "detect_channel", lambda: InstallChannel.GIT
        )
        monkeypatch.setattr(app_update, "local_git_summary", lambda: "v2.1.1-12-gabc")
        monkeypatch.setattr(
            app_update,
            "fetch_latest_release",
            lambda: parse_release_payload(LATEST_RELEASE_FIXTURE),
        )
        monkeypatch.setattr(app_update, "__version__", "2.1.1")
        result = check_for_update()
        assert result.status == UpdateStatus.UP_TO_DATE
        assert result.channel == InstallChannel.GIT
        assert result.can_apply is False
        assert result.git_summary == "v2.1.1-12-gabc"
        stored = manager.load_all_settings()
        assert SettingsKey.UPDATE_LAST_CHECK_AT in stored

    def test_behind_latest_is_update_available(self, monkeypatch):
        monkeypatch.setattr(
            app_update, "detect_channel", lambda: InstallChannel.SOURCE
        )
        monkeypatch.setattr(app_update, "mark_check_completed", lambda: None)
        monkeypatch.setattr(
            app_update,
            "fetch_latest_release",
            lambda: parse_release_payload(LATEST_RELEASE_FIXTURE),
        )
        monkeypatch.setattr(app_update, "__version__", "2.0.0")
        result = check_for_update(persist=False)
        assert result.status == UpdateStatus.UPDATE_AVAILABLE
        assert result.release.version == "2.1.1"


class TestPersistPromptChoices:
    def test_writes_independent_prefs(self, tmp_path, monkeypatch):
        manager = SettingsManager(str(tmp_path / "settings.json"))
        monkeypatch.setattr(app_update, "settings_manager", manager)
        persist_prompt_choices(
            notify_enabled=False,
            check_enabled=True,
            skipped_version="v2.2.0",
        )
        saved = manager.load_all_settings()
        assert saved[SettingsKey.UPDATE_NOTIFY_ENABLED] is False
        assert saved[SettingsKey.UPDATE_CHECK_ENABLED] is True
        assert saved[SettingsKey.UPDATE_SKIPPED_VERSION] == "2.2.0"


class TestDownloadInstaller:
    def test_refuses_source_channel(self, tmp_path):
        with patch.object(
            app_update, "detect_channel", return_value=InstallChannel.GIT
        ), patch.object(app_update.sys, "platform", "win32"):
            with pytest.raises(AppUpdateError, match="cannot install"):
                download_installer(_release())

    def test_hash_mismatch_discards_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_update, "updates_dir", lambda: str(tmp_path))
        payload = b"not-the-installer"

        class _Response:
            status = 200

            def read(self, _size=-1):
                data = payload if getattr(self, "_sent", False) is False else b""
                self._sent = True
                return data if _size != 0 else payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.object(
            app_update, "detect_channel", return_value=InstallChannel.INSTALLER
        ), patch.object(app_update.sys, "platform", "win32"), patch.object(
            app_update, "_open", return_value=_Response()
        ):
            with pytest.raises(AppUpdateError, match="integrity"):
                download_installer(
                    _release(
                        sha256="aa" * 32,
                        url="https://example/setup.exe",
                        size_bytes=len(payload),
                    )
                )
        assert list(tmp_path.glob("*.exe")) == []
        assert list(tmp_path.glob("*.part")) == []

    def test_matching_hash_keeps_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_update, "updates_dir", lambda: str(tmp_path))
        body = b"verified-setup"
        digest = hashlib.sha256(body).hexdigest()
        existing = tmp_path / "OpenWhisper-Setup-2.2.0.exe"
        existing.write_bytes(body)
        with patch.object(
            app_update, "detect_channel", return_value=InstallChannel.INSTALLER
        ), patch.object(app_update.sys, "platform", "win32"), patch.object(
            app_update, "_open"
        ) as opener:
            path = download_installer(
                _release(
                    sha256=digest,
                    url="https://example/setup.exe",
                    size_bytes=len(body),
                )
            )
        assert path == str(existing)
        opener.assert_not_called()
