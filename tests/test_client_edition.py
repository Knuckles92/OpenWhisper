"""Tests for Client-Only edition detection, build specs, installers, and CI contracts."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from services import edition as edition_mod
from services.edition import (
    EDITION_CLIENT,
    EDITION_ENV_VAR,
    EDITION_FULL,
    EDITION_MANIFEST_NAME,
    current_edition,
    edition_label,
    is_client_only,
    normalize_edition,
    read_edition_manifest,
    write_edition_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str | None:
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        candidates = (
            [Path(git).resolve().parent.parent / "bin" / "bash.exe"] if git else []
        )
        candidates += [
            Path(os.environ[key]) / "Git" / "bin" / "bash.exe"
            for key in ("ProgramFiles", "ProgramFiles(x86)")
            if os.environ.get(key)
        ]
        bash = next((str(path) for path in candidates if path.is_file()), bash)
    return bash


class TestEditionDetection:
    def test_constants(self):
        assert EDITION_FULL == "full"
        assert EDITION_CLIENT == "client"
        assert EDITION_MANIFEST_NAME == "edition.json"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("full", "full"),
            ("FULL", "full"),
            ("  Full  ", "full"),
            ("client", "client"),
            ("CLIENT", "client"),
            ("  Client\n", "client"),
            ("", None),
            ("   ", None),
            ("allinone", None),
            (None, None),
            (123, None),
        ],
    )
    def test_normalize_edition(self, raw, expected):
        assert normalize_edition(raw) == expected

    def test_defaults_to_full_when_unset_and_no_manifest(self, tmp_path, monkeypatch):
        monkeypatch.delenv(EDITION_ENV_VAR, raising=False)
        monkeypatch.setattr(edition_mod, "bundle_root", lambda: str(tmp_path))
        assert current_edition() == EDITION_FULL
        assert is_client_only() is False
        assert edition_label() == "OpenWhisper"

    def test_reads_manifest_from_bundle_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv(EDITION_ENV_VAR, raising=False)
        monkeypatch.setattr(edition_mod, "bundle_root", lambda: str(tmp_path))

        written = write_edition_manifest(tmp_path, EDITION_CLIENT)
        assert written == tmp_path / EDITION_MANIFEST_NAME
        assert not (tmp_path / f".{EDITION_MANIFEST_NAME}.tmp").exists()
        payload = json.loads(written.read_text(encoding="utf-8"))
        assert payload["edition"] == EDITION_CLIENT
        assert payload["schema_version"] == 1

        assert read_edition_manifest() == EDITION_CLIENT
        assert current_edition() == EDITION_CLIENT
        assert is_client_only() is True
        assert edition_label() == "OpenWhisper Client"

    def test_env_var_overrides_manifest_case_insensitively(self, tmp_path, monkeypatch):
        write_edition_manifest(tmp_path, EDITION_CLIENT)
        monkeypatch.setattr(edition_mod, "bundle_root", lambda: str(tmp_path))

        monkeypatch.setenv(EDITION_ENV_VAR, "FULL")
        assert current_edition() == EDITION_FULL
        assert is_client_only() is False
        assert edition_label() == "OpenWhisper"

        write_edition_manifest(tmp_path, EDITION_FULL)
        monkeypatch.setenv(EDITION_ENV_VAR, "  ClIeNt  ")
        assert current_edition() == EDITION_CLIENT
        assert is_client_only() is True
        assert edition_label() == "OpenWhisper Client"

    def test_invalid_env_var_falls_back_to_manifest_then_full(self, tmp_path, monkeypatch):
        monkeypatch.setattr(edition_mod, "bundle_root", lambda: str(tmp_path))
        monkeypatch.setenv(EDITION_ENV_VAR, "bogus")
        assert current_edition() == EDITION_FULL

        write_edition_manifest(tmp_path, EDITION_CLIENT)
        assert current_edition() == EDITION_CLIENT

    @pytest.mark.parametrize(
        "content",
        [
            "not valid json",
            '["client"]',
            '{"edition": "invalid"}',
            "{}",
        ],
    )
    def test_corrupt_or_invalid_manifest_defaults_to_full(
        self, tmp_path, monkeypatch, content
    ):
        monkeypatch.delenv(EDITION_ENV_VAR, raising=False)
        (tmp_path / EDITION_MANIFEST_NAME).write_text(content, encoding="utf-8")
        assert read_edition_manifest(tmp_path) is None
        assert current_edition(bundle_dir=tmp_path) == EDITION_FULL
        assert is_client_only(bundle_dir=tmp_path) is False

    def test_write_edition_manifest_creates_dirs_and_rejects_invalid(self, tmp_path):
        nested = tmp_path / "a" / "b"
        path = write_edition_manifest(nested, "CLIENT")
        assert path.is_file()
        assert read_edition_manifest(nested) == EDITION_CLIENT
        with pytest.raises(ValueError, match="Unsupported OpenWhisper edition"):
            write_edition_manifest(nested, "lite")

    def test_edition_label_explicit_override(self, monkeypatch):
        monkeypatch.setenv(EDITION_ENV_VAR, EDITION_FULL)
        assert edition_label(EDITION_CLIENT) == "OpenWhisper Client"
        assert edition_label(EDITION_FULL) == "OpenWhisper"


class TestBuildSpecContracts:
    def test_pyinstaller_spec_handles_client_edition_and_macos_url_scheme(self):
        spec = (ROOT / "OpenWhisper.spec").read_text(encoding="utf-8")
        assert 'os.environ.get("OPENWHISPER_EDITION", EDITION_FULL)' in spec
        assert "write_edition_manifest(" in spec
        assert "(str(_EDITION_MANIFEST_PATH), \".\")" in spec
        # Client-only exclusions must cover local ML runtimes and PyAV.
        for pkg in (
            "ctranslate2",
            "faster_whisper",
            "onnxruntime",
            "tokenizers",
            "huggingface_hub",
            "hf_xet",
            "av",
        ):
            assert f'"{pkg}"' in spec
        assert "CLIENT_ONLY_EXCLUDES" in spec
        assert "if IS_CLIENT_ONLY:" in spec
        assert "if not IS_CLIENT_ONLY:" in spec
        # Core client UI, audio capture, walkthrough, and networking stay in _COLLECT_PACKAGES.
        for kept in ("sounddevice", "lxml", "uvicorn", "mcp", "soundcard", "pywebrtc_audio", "keyring", "pynput"):
            assert f'"{kept}"' in spec
        # macOS custom URL scheme for openwhisper://pair?... deep links.
        assert '"CFBundleURLTypes"' in spec
        assert '"CFBundleURLSchemes": ["openwhisper"]' in spec
        assert '"CFBundleURLName": MACOS_BUNDLE_IDENTIFIER' in spec

    def test_inno_setup_keeps_app_id_and_registers_hkcu_uri_protocol(self):
        iss = (ROOT / "installer" / "OpenWhisper.iss").read_text(encoding="utf-8")
        # Same AppId preserves settings & history across Client <-> Full upgrades.
        assert "AppId={{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}" in iss
        assert "PrivilegesRequired=lowest" in iss
        assert "OutputBaseFilename={#OutputPrefix}-Setup-{#AppVersion}" in iss
        assert "[Registry]" in iss
        assert 'Root: HKCU; Subkey: "Software\\Classes\\openwhisper"' in iss
        assert 'ValueName: "URL Protocol"' in iss
        assert 'Subkey: "Software\\Classes\\openwhisper\\shell\\open\\command"' in iss
        assert '"""{app}\\{#AppExeName}"" ""%1"""' in iss
        assert "Flags: uninsdeletekey" in iss

    def test_windows_build_script_supports_client_edition_and_valid_ps1(self):
        ps1_path = ROOT / "scripts" / "build_installer.ps1"
        script = ps1_path.read_text(encoding="utf-8")
        assert "[ValidateSet('full', 'client')][string]$Edition = 'full'" in script
        assert "[switch]$IncludeClient" in script
        assert "OpenWhisper-Client-Setup-$Version.exe" in script
        assert "OpenWhisper-Client-$Version-win64.tar.xz" in script
        assert '"/DOutputPrefix=OpenWhisper-Client"' in script
        assert "'--edition', $CurrentEdition" in script
        assert "_internal\\edition.json" in script
        for absent in ("ctranslate2", "faster_whisper", "onnxruntime", "av.libs", "bundled_components\\asr-transcribe"):
            assert absent in script

        if sys.platform == "win32":
            pwsh = shutil.which("powershell") or shutil.which("pwsh")
            if pwsh:
                check_cmd = (
                    "$errors = $null; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$null, [ref]$errors); "
                    "if ($errors) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
                )
                completed = subprocess.run(
                    [pwsh, "-NoProfile", "-NonInteractive", "-Command", check_cmd],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                assert completed.returncode == 0, completed.stderr

    def test_linux_packaging_supports_client_edition_and_deep_links(self):
        script_path = ROOT / "scripts" / "build_installer.sh"
        script = script_path.read_text(encoding="utf-8")
        assert "--edition" in script
        assert "OpenWhisper-Client-$VERSION-linux-amd64.deb" in script
        assert "OpenWhisper-Client-$VERSION-linux-x86_64.pkg.tar.zst" in script
        assert 'PKG_NAME="openwhisper-client"' in script
        assert 'CONFLICT_PKG="openwhisper"' in script
        assert "Provides: openwhisper" in script
        assert "provides = openwhisper" in script
        assert "$INTERNAL_DIR/edition.json" in script
        assert "bundled_components/asr-transcribe" in script

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(ROOT / "installer" / "linux" / "openwhisper.desktop", encoding="utf-8")
        desktop = parser["Desktop Entry"]
        assert desktop["Exec"] == "openwhisper %u"
        assert desktop["MimeType"] == "x-scheme-handler/openwhisper;"

        bash = _bash()
        if bash:
            completed = subprocess.run(
                [bash, "-n"],
                input=script_path.read_bytes().replace(b"\r\n", b"\n"),
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr

    def test_macos_build_script_supports_client_edition_and_url_scheme_check(self):
        script_path = ROOT / "scripts" / "build_installer_macos.sh"
        script = script_path.read_text(encoding="utf-8")
        assert "--edition" in script
        assert 'DMG_ARTIFACT="$OUTPUT_DIR/${APP_NAME}-Client-${VERSION}-macos-arm64.dmg"' in script
        assert "CFBundleURLTypes:0:CFBundleURLSchemes:0" in script
        assert "client-excluded package leaked" in script
        assert "client-excluded native ML/FFmpeg library leaked" in script
        assert "Contents/Resources/edition.json" in script

        bash = _bash()
        if bash:
            completed = subprocess.run(
                [bash, "-n"],
                input=script_path.read_bytes().replace(b"\r\n", b"\n"),
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr

    def test_ci_workflow_builds_and_verifies_client_artifacts_for_all_platforms(self):
        workflow_path = ROOT / ".github" / "workflows" / "build-installers.yml"
        raw = workflow_path.read_text(encoding="utf-8")
        parsed = yaml.load(raw, Loader=yaml.BaseLoader)
        assert "jobs" in parsed

        assert r".\scripts\build_installer.ps1 -Clean -IncludeClient" in raw
        assert "installer/Output/OpenWhisper-Client-Setup-*.exe" in raw
        assert "./scripts/build_installer.sh --edition client" in raw
        assert "Smoke-test client Debian package in clean Ubuntu" in raw
        assert "./scripts/build_installer_macos.sh --edition client" in raw
        assert "expected_artifacts=13" in raw
        assert "expected_artifacts=11" in raw
        for expected_check in (
            "release/OpenWhisper-Client-Setup-$BUILD_VERSION.exe",
            "release/OpenWhisper-Client-$BUILD_VERSION-win64.tar.xz",
            "release/OpenWhisper-Client-$BUILD_VERSION-linux-amd64.deb",
            "release/OpenWhisper-Client-$BUILD_VERSION-linux-x86_64.pkg.tar.zst",
            "release/OpenWhisper-Client-$BUILD_VERSION-macos-arm64.dmg",
        ):
            assert expected_check in raw
