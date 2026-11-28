"""Tests for edition-aware auto-updater contract, release parsing, and manifest safety."""

from __future__ import annotations

import ast
import hashlib
import io
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import pack_update_archive
from scripts import windows_signing as ws
from services import app_update
from services import app_update_apply as apply_mod
from services import edition as edition_mod
from services.app_update import (
    ApplyMode,
    InstallChannel,
    UpdateStatus,
    check_for_update,
    parse_release_payload,
    select_newest_release,
)
from services.edition import (
    EDITION_CLIENT,
    EDITION_ENV_VAR,
    EDITION_FULL,
    write_edition_manifest,
)
from services.settings import SettingsManager
from services.update_contract import (
    APP_EXE_NAME,
    MANIFEST_NAME,
    UPDATER_EXE_NAME,
    archive_asset_name,
    macos_asset_name,
    normalize_edition,
    setup_asset_name,
)

ROOT = Path(__file__).resolve().parents[1]
DIGEST_FULL = "11" * 32
DIGEST_CLIENT = "22" * 32
TAG = "v2.10.0"
BASE = f"https://github.com/{app_update.GITHUB_REPO}/releases/download/{TAG}/"


def _github_asset(name: str, *, version: str = "2.10.0", digest: str = DIGEST_FULL, size: int = 4096) -> dict:
    return {
        "id": abs(hash(name)) % 100_000 + 1,
        "name": name,
        "size": size,
        "state": "uploaded",
        "digest": f"sha256:{digest}",
        "browser_download_url": (
            f"https://github.com/{app_update.GITHUB_REPO}/releases/download/v{version}/{name}"
        ),
    }


def _dual_edition_release(version: str = "2.10.0", *, prerelease: bool = False) -> dict:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/{app_update.GITHUB_REPO}/releases/tag/v{version}",
        "body": f"Release notes for {version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            _github_asset(f"OpenWhisper-Setup-{version}.exe", version=version, digest=DIGEST_FULL),
            _github_asset(f"OpenWhisper-{version}-win64.tar.xz", version=version, digest=DIGEST_FULL),
            _github_asset(f"OpenWhisper-{version}-macos-arm64.dmg", version=version, digest=DIGEST_FULL),
            _github_asset(f"OpenWhisper-Client-Setup-{version}.exe", version=version, digest=DIGEST_CLIENT),
            _github_asset(f"OpenWhisper-Client-{version}-win64.tar.xz", version=version, digest=DIGEST_CLIENT),
            _github_asset(f"OpenWhisper-Client-{version}-macos-arm64.dmg", version=version, digest=DIGEST_CLIENT),
        ],
    }


def _make_dist(root: Path, edition: str | None = None, payload: bytes = b"binary") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / APP_EXE_NAME).write_bytes(payload)
    (root / UPDATER_EXE_NAME).write_bytes(b"helper")
    internal = root / "_internal"
    internal.mkdir(exist_ok=True)
    (internal / "app.dat").write_bytes(payload)
    if edition is not None:
        write_edition_manifest(internal, edition)
    return root


class TestUpdateContractStdlibAndNames:
    def test_update_contract_is_strictly_stdlib_only(self):
        source = (ROOT / "services" / "update_contract.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        stdlib = sys.stdlib_module_names
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    assert top in stdlib, f"Non-stdlib import in update_contract.py: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "Relative import not expected in update_contract.py"
                assert node.module is not None
                top = node.module.split(".", 1)[0]
                assert top in stdlib, f"Non-stdlib import in update_contract.py: {node.module}"

    def test_asset_names_for_full_and_client(self):
        assert setup_asset_name("2.10.0") == "OpenWhisper-Setup-2.10.0.exe"
        assert setup_asset_name("v2.10.0", EDITION_FULL) == "OpenWhisper-Setup-2.10.0.exe"
        assert setup_asset_name("v2.10.0", edition=EDITION_CLIENT) == "OpenWhisper-Client-Setup-2.10.0.exe"
        assert setup_asset_name("2.6.01", "CLIENT") == "OpenWhisper-Client-Setup-2.6.01.exe"

        assert archive_asset_name("2.10.0") == "OpenWhisper-2.10.0-win64.tar.xz"
        assert archive_asset_name("v2.10.0", edition=EDITION_FULL) == "OpenWhisper-2.10.0-win64.tar.xz"
        assert archive_asset_name("v2.10.0", edition=EDITION_CLIENT) == "OpenWhisper-Client-2.10.0-win64.tar.xz"

        assert macos_asset_name("2.10.0") == "OpenWhisper-2.10.0-macos-arm64.dmg"
        assert macos_asset_name("v2.10.0", edition=EDITION_FULL) == "OpenWhisper-2.10.0-macos-arm64.dmg"
        assert macos_asset_name("v2.10.0", edition=EDITION_CLIENT) == "OpenWhisper-Client-2.10.0-macos-arm64.dmg"

    def test_normalize_edition_rejects_unknown(self):
        assert normalize_edition(None) == EDITION_FULL
        assert normalize_edition("  Client ") == EDITION_CLIENT
        with pytest.raises(ValueError, match="Unsupported"):
            normalize_edition("unknown")
        with pytest.raises(ValueError, match="Unsupported"):
            setup_asset_name("2.10.0", edition="unknown")


class TestEditionAwareReleaseParsing:
    def test_full_build_selects_only_full_assets(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_FULL)
        release = parse_release_payload(_dual_edition_release())
        assert release.setup_asset is not None
        assert release.setup_asset.name == "OpenWhisper-Setup-2.10.0.exe"
        assert release.setup_asset.sha256 == DIGEST_FULL
        assert release.native_asset is not None
        assert release.native_asset.name == "OpenWhisper-2.10.0-win64.tar.xz"
        assert release.native_asset.sha256 == DIGEST_FULL
        assert release.macos_asset is not None
        assert release.macos_asset.name == "OpenWhisper-2.10.0-macos-arm64.dmg"
        assert release.macos_asset.sha256 == DIGEST_FULL

    def test_client_build_selects_only_client_assets_via_env(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        release = parse_release_payload(_dual_edition_release())
        assert release.setup_asset is not None
        assert release.setup_asset.name == "OpenWhisper-Client-Setup-2.10.0.exe"
        assert release.setup_asset.sha256 == DIGEST_CLIENT
        assert release.native_asset is not None
        assert release.native_asset.name == "OpenWhisper-Client-2.10.0-win64.tar.xz"
        assert release.native_asset.sha256 == DIGEST_CLIENT
        assert release.macos_asset is not None
        assert release.macos_asset.name == "OpenWhisper-Client-2.10.0-macos-arm64.dmg"
        assert release.macos_asset.sha256 == DIGEST_CLIENT

    def test_client_build_selects_client_assets_via_edition_manifest(self, tmp_path, monkeypatch):
        monkeypatch.delenv(EDITION_ENV_VAR, raising=False)
        write_edition_manifest(tmp_path, EDITION_CLIENT)
        monkeypatch.setattr(edition_mod, "bundle_root", lambda: str(tmp_path))

        release = parse_release_payload(_dual_edition_release())
        assert release.setup_asset is not None
        assert release.setup_asset.name == "OpenWhisper-Client-Setup-2.10.0.exe"
        assert release.native_asset is not None
        assert release.native_asset.name == "OpenWhisper-Client-2.10.0-win64.tar.xz"
        assert release.macos_asset is not None
        assert release.macos_asset.name == "OpenWhisper-Client-2.10.0-macos-arm64.dmg"

    def test_client_build_never_falls_back_to_full_assets(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        full_only = {
            "tag_name": TAG,
            "html_url": f"https://github.com/{app_update.GITHUB_REPO}/releases/tag/{TAG}",
            "assets": [
                _github_asset("OpenWhisper-Setup-2.10.0.exe"),
                _github_asset("OpenWhisper-2.10.0-win64.tar.xz"),
                _github_asset("OpenWhisper-2.10.0-macos-arm64.dmg"),
            ],
        }
        release = parse_release_payload(full_only)
        assert release.setup_asset is None
        assert release.native_asset is None
        assert release.macos_asset is None

    def test_explicit_edition_argument_overrides_active_edition(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_FULL)
        client_release = parse_release_payload(_dual_edition_release(), edition=EDITION_CLIENT)
        assert client_release.setup_asset is not None
        assert client_release.setup_asset.name == "OpenWhisper-Client-Setup-2.10.0.exe"

        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        full_release = parse_release_payload(_dual_edition_release(), edition=EDITION_FULL)
        assert full_release.setup_asset is not None
        assert full_release.setup_asset.name == "OpenWhisper-Setup-2.10.0.exe"

    def test_beta_track_select_newest_release_respects_client_edition(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        older = _dual_edition_release("2.9.0", prerelease=False)
        beta = _dual_edition_release("2.10.0", prerelease=True)
        selected = select_newest_release([older, beta])
        assert selected.version == "2.10.0"
        assert selected.prerelease is True
        assert selected.setup_asset is not None
        assert selected.setup_asset.name == "OpenWhisper-Client-Setup-2.10.0.exe"
        assert selected.native_asset is not None
        assert selected.native_asset.name == "OpenWhisper-Client-2.10.0-win64.tar.xz"
        assert selected.macos_asset is not None
        assert selected.macos_asset.name == "OpenWhisper-Client-2.10.0-macos-arm64.dmg"

    def test_rate_limited_public_fallback_selects_client_assets(self, tmp_path, monkeypatch):
        manager = SettingsManager(str(tmp_path / "settings.json"))
        monkeypatch.setattr(app_update, "settings_manager", manager)
        monkeypatch.delenv(app_update.UPDATE_FEED_ENV, raising=False)
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        monkeypatch.setattr(app_update, "detect_channel", lambda: InstallChannel.INSTALLER)
        monkeypatch.setattr(app_update, "__version__", "2.9.0")
        monkeypatch.setattr(app_update.sys, "platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")

        checksum_lines = [
            f"{DIGEST_FULL}  OpenWhisper-Setup-2.10.0.exe",
            f"{DIGEST_FULL}  OpenWhisper-2.10.0-win64.tar.xz",
            f"{DIGEST_FULL}  OpenWhisper-2.10.0-macos-arm64.dmg",
            f"{DIGEST_CLIENT}  OpenWhisper-Client-Setup-2.10.0.exe",
            f"{DIGEST_CLIENT}  OpenWhisper-Client-2.10.0-win64.tar.xz",
            f"{DIGEST_CLIENT}  OpenWhisper-Client-2.10.0-macos-arm64.dmg",
        ]
        checksums_body = ("\n".join(checksum_lines) + "\n").encode("utf-8")
        page = f"{app_update.RELEASES_PAGE_URL}/tag/{TAG}"

        class _Resp(io.BytesIO):
            def __init__(self, body=b"", *, url=page, size=2048):
                super().__init__(body)
                self._url = url
                self.status = 200
                self.headers = {"Content-Length": str(size)}

            def geturl(self):
                return self._url

        head_urls: list[str] = []

        def fake_open(url, extra_headers=None, *, method="GET"):
            if url == app_update.LATEST_RELEASE_URL:
                raise urllib.error.HTTPError(
                    url,
                    403,
                    "Forbidden",
                    {
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int((datetime.now(UTC) + timedelta(minutes=30)).timestamp())),
                    },
                    None,
                )
            if url == app_update.LATEST_RELEASE_PAGE_URL:
                return _Resp(url=page)
            if url == BASE + "SHA256SUMS.txt":
                return _Resp(checksums_body)
            if method == "HEAD":
                head_urls.append(url)
                return _Resp(size=2048)
            raise AssertionError(f"Unexpected request: {method} {url}")

        monkeypatch.setattr(app_update, "_open", fake_open)
        result = check_for_update()
        assert result.status == UpdateStatus.UPDATE_AVAILABLE
        assert result.can_apply is True
        assert result.apply_mode == ApplyMode.MACOS_DMG
        assert result.release is not None
        assert result.release.setup_asset is not None
        assert result.release.setup_asset.name == "OpenWhisper-Client-Setup-2.10.0.exe"
        assert result.release.native_asset is not None
        assert result.release.native_asset.name == "OpenWhisper-Client-2.10.0-win64.tar.xz"
        assert result.release.macos_asset is not None
        assert result.release.macos_asset.name == "OpenWhisper-Client-2.10.0-macos-arm64.dmg"
        assert result.release.macos_asset.sha256 == DIGEST_CLIENT
        assert head_urls == [
            BASE + "OpenWhisper-Client-Setup-2.10.0.exe",
            BASE + "OpenWhisper-Client-2.10.0-win64.tar.xz",
            BASE + "OpenWhisper-Client-2.10.0-macos-arm64.dmg",
        ]


class TestManifestAndArchiveEditionSafety:
    def test_manifest_records_and_validates_edition(self, tmp_path):
        client_dist = _make_dist(tmp_path / "client", edition=EDITION_CLIENT)
        full_dist = _make_dist(tmp_path / "full", edition=EDITION_FULL)
        legacy_dist = _make_dist(tmp_path / "legacy", edition=None)

        assert apply_mod.read_dist_edition(str(client_dist)) == EDITION_CLIENT
        assert apply_mod.read_dist_edition(str(full_dist)) == EDITION_FULL
        assert apply_mod.read_dist_edition(str(legacy_dist)) == EDITION_FULL

        client_manifest = apply_mod.build_update_manifest(str(client_dist), "2.10.0")
        assert client_manifest["edition"] == EDITION_CLIENT
        apply_mod.validate_manifest(client_manifest, expected_version="2.10.0", expected_edition=EDITION_CLIENT)
        with pytest.raises(apply_mod.UpdateApplyError, match="different edition"):
            apply_mod.validate_manifest(client_manifest, expected_version="2.10.0", expected_edition=EDITION_FULL)

        full_manifest = apply_mod.build_update_manifest(str(full_dist), "2.10.0")
        assert full_manifest["edition"] == EDITION_FULL
        apply_mod.validate_manifest(full_manifest, expected_version="2.10.0", expected_edition=EDITION_FULL)
        with pytest.raises(apply_mod.UpdateApplyError, match="different edition"):
            apply_mod.validate_manifest(full_manifest, expected_version="2.10.0", expected_edition=EDITION_CLIENT)

        # Legacy manifests without an "edition" key are treated as "full":
        # accepted for full installs, rejected for client installs.
        legacy_manifest = dict(full_manifest)
        legacy_manifest.pop("edition")
        apply_mod.validate_manifest(legacy_manifest, expected_version="2.10.0", expected_edition=EDITION_FULL)
        with pytest.raises(apply_mod.UpdateApplyError, match="different edition"):
            apply_mod.validate_manifest(legacy_manifest, expected_version="2.10.0", expected_edition=EDITION_CLIENT)

        bad_edition = dict(full_manifest, edition="bogus")
        with pytest.raises(apply_mod.UpdateApplyError, match="unknown edition"):
            apply_mod.validate_manifest(bad_edition, expected_version="2.10.0")

    def test_pack_update_archive_client_round_trip(self, tmp_path, monkeypatch):
        dist = _make_dist(tmp_path / "dist-client", edition=EDITION_CLIENT, payload=b"client-exe")
        out_archive = tmp_path / "out" / "OpenWhisper-Client-2.10.0-win64.tar.xz"
        out_archive.parent.mkdir()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pack_update_archive.py",
                "--dist",
                str(dist),
                "--output",
                str(out_archive),
                "--version",
                "2.10.0",
                "--edition",
                "client",
            ],
        )
        assert pack_update_archive.main() == 0
        assert out_archive.is_file()
        packed = apply_mod.read_manifest_from_tar_xz(str(out_archive))
        assert packed["edition"] == EDITION_CLIENT
        assert "_internal/edition.json" in packed["files"]
        assert (dist / MANIFEST_NAME).is_file()

        # Wrong archive name for client edition must fail.
        wrong_name = tmp_path / "out" / "OpenWhisper-2.10.0-win64.tar.xz"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pack_update_archive.py",
                "--dist",
                str(dist),
                "--output",
                str(wrong_name),
                "--version",
                "2.10.0",
                "--edition",
                "client",
            ],
        )
        with pytest.raises(SystemExit, match="OpenWhisper-Client-2.10.0-win64.tar.xz"):
            pack_update_archive.main()

    def test_windows_signing_metadata_verifies_client_setup_exe(self, tmp_path):
        release = tmp_path / "release"
        release.mkdir()
        full_setup = release / "OpenWhisper-Setup-2.10.0.exe"
        client_setup = release / "OpenWhisper-Client-Setup-2.10.0.exe"
        full_setup.write_bytes(b"full-setup")
        client_setup.write_bytes(b"client-setup")

        report = {
            "schema_version": 1,
            "signed": True,
            "method": "trusted-signing",
            "signer_subject": "CN=Fiori Labs",
            "signer_thumbprint": "AB" * 20,
            "files": [
                {
                    "name": full_setup.name,
                    "sha256": hashlib.sha256(b"full-setup").hexdigest(),
                    "status": "Valid",
                    "signer_subject": "CN=Fiori Labs",
                    "signer_thumbprint": "AB" * 20,
                    "timestamped": True,
                }
            ],
        }
        with pytest.raises(ws.SigningError, match=client_setup.name):
            ws.release_metadata(report, release)

        report["files"].append(
            {
                "name": client_setup.name,
                "sha256": hashlib.sha256(b"client-setup").hexdigest(),
                "status": "Valid",
                "signer_subject": "CN=Fiori Labs",
                "signer_thumbprint": "AB" * 20,
                "timestamped": True,
            }
        )
        meta = ws.release_metadata(report, release)
        assert meta["windows_signed"] is True

        report["files"][1]["sha256"] = hashlib.sha256(b"tampered").hexdigest()
        with pytest.raises(ws.SigningError, match=client_setup.name):
            ws.release_metadata(report, release)

    def test_apply_native_update_passes_current_edition_to_prepare_candidate(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_CLIENT)
        release = parse_release_payload(_dual_edition_release())
        result = app_update.UpdateCheckResult(
            status=UpdateStatus.UPDATE_AVAILABLE,
            current_version="2.9.0",
            channel=InstallChannel.INSTALLER,
            release=release,
            can_apply=True,
            apply_mode=ApplyMode.NATIVE,
        )
        captured: dict = {}

        def fake_prepare(*args, **kwargs):
            captured.update(kwargs)
            return type("J", (), {"transaction_id": "a" * 32})()

        with (
            patch.object(app_update, "download_release_asset", return_value="client.tar.xz"),
            patch.object(app_update, "discover_install_registration", return_value=object()),
            patch.object(app_update, "running_app_dir", return_value="/app"),
            patch.object(app_update, "prepare_candidate", side_effect=fake_prepare),
        ):
            handoff = app_update.apply_update(result)
        assert handoff == "native:" + ("a" * 32)
        assert captured["expected_edition"] == EDITION_CLIENT
