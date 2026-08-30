"""Static contract tests for the native Linux release package."""

from __future__ import annotations

import configparser
import os
import struct
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINUX_INSTALLER = ROOT / "installer" / "linux"
BUILD_SCRIPT = ROOT / "scripts" / "build_installer.sh"


def test_build_script_is_executable_and_valid_bash():
    assert os.access(BUILD_SCRIPT, os.X_OK)
    completed = subprocess.run(
        ["bash", "-n", str(BUILD_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_control_template_has_native_runtime_dependencies():
    control = (LINUX_INSTALLER / "control.in").read_text(encoding="utf-8")
    assert "Package: openwhisper" in control
    assert "Version: @VERSION@" in control
    assert "Architecture: @ARCHITECTURE@" in control
    assert "Installed-Size: @INSTALLED_SIZE@" in control
    assert "libc6 (>= @GLIBC_MIN@)" in control
    for package in (
        "libdrm2",
        "libegl1",
        "libgl1",
        "libportaudio2",
        "libwayland-client0",
        "libwayland-cursor0",
        "libwayland-egl1",
        "libxcb-cursor0",
        "libxcb-icccm4",
        "libxcb-keysyms1",
        "libxcb-shape0",
        "libxcb-xkb1",
        "libxkbcommon-x11-0",
    ):
        assert package in control


def test_desktop_entry_and_launcher_match_installed_layout():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(LINUX_INSTALLER / "openwhisper.desktop", encoding="utf-8")
    desktop = parser["Desktop Entry"]
    assert desktop["Type"] == "Application"
    assert desktop["Exec"] == "openwhisper"
    assert desktop["Icon"] == "openwhisper"
    assert desktop["Terminal"] == "false"
    assert desktop["StartupWMClass"] == "OpenWhisper"

    launcher_path = LINUX_INSTALLER / "openwhisper"
    launcher = launcher_path.read_text(encoding="utf-8")
    assert os.access(launcher_path, os.X_OK)
    assert 'exec /usr/lib/openwhisper/OpenWhisper "$@"' in launcher


def test_linux_icon_is_committed_png_at_expected_size():
    icon = ROOT / "ui_qt" / "assets" / "openwhisper.png"
    payload = icon.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", payload[16:24])
    assert (width, height) == (256, 256)


def test_frozen_install_channel_uses_linux_copy_and_is_notify_only():
    from services.app_update import (
        ApplyMode,
        InstallChannel,
        ReleaseAsset,
        ReleaseInfo,
        channel_label,
        resolve_release_apply_mode,
    )

    assert channel_label(InstallChannel.INSTALLER, "linux") == "Linux package"
    assert channel_label(InstallChannel.INSTALLER, "linux2") == "Linux package"

    windows_asset = ReleaseAsset(
        url="https://example.invalid/OpenWhisper-Setup.exe",
        name="OpenWhisper-Setup.exe",
        size_bytes=123,
        sha256="a" * 64,
    )
    release = ReleaseInfo(
        version="9.9.9",
        tag_name="v9.9.9",
        html_url="https://example.invalid/release",
        notes="",
        setup_asset=windows_asset,
        native_asset=windows_asset,
    )
    assert resolve_release_apply_mode(
        InstallChannel.INSTALLER,
        release,
        platform_name="linux",
    ) == ApplyMode.NOTIFY_ONLY


def test_release_artifact_name_is_versioned_and_arch_specific():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "OpenWhisper-$VERSION-linux-amd64.deb" in script
    assert "dpkg-deb --root-owner-group" in script
    assert 'xvfb-run -a "$PYTHON" -m PyInstaller' in script
    assert "patchelf --remove-rpath" in script
    assert '"$EXE_PATH" --version' in script
    assert '"$EXE_PATH" --self-test' in script
    assert 'find -L "$DIST_DIR" -type l' in script
    assert "lintian --fail-on error" in script
    assert "-perm /0022" in script
