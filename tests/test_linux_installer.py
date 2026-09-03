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


def test_pkginfo_template_has_native_runtime_dependencies():
    pkginfo = (LINUX_INSTALLER / "PKGINFO.in").read_text(encoding="utf-8")
    assert "pkgname = openwhisper" in pkginfo
    assert "pkgbase = openwhisper" in pkginfo
    assert "pkgver = @VERSION@-1" in pkginfo
    assert "builddate = @BUILDDATE@" in pkginfo
    assert "packager = Fiori Labs <support@fiorilabs.tech>" in pkginfo
    assert "size = @INSTALLED_SIZE_BYTES@" in pkginfo
    assert "depend = glibc>=@GLIBC_MIN@" in pkginfo
    for package in (
        "libdrm",
        "libegl",
        "libgl",
        "portaudio",
        "wayland",
        "xcb-util-cursor",
        "xcb-util-wm",
        "xcb-util-keysyms",
        "libxcb",
        "libxkbcommon",
    ):
        assert f"depend = {package}" in pkginfo
    assert "optdepend = gnome-keyring:" in pkginfo
    assert "optdepend = libpulse:" in pkginfo
    assert "license = MIT" in pkginfo
    assert "license = GPL-3.0-only" in pkginfo
    assert "license = LGPL-3.0-only" in pkginfo


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
    assert "Recommends: gnome-keyring, pulseaudio-utils" in control


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
        can_apply,
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
    assert not can_apply(
        InstallChannel.INSTALLER,
        release,
        platform_name="linux",
    )


def test_release_bundle_and_package_retain_required_license_notices():
    spec = (ROOT / "OpenWhisper.spec").read_text(encoding="utf-8")
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert '_distribution_license("PyQt6",' in spec
    assert '_distribution_license("PyQt6-Qt6",' in spec
    assert "third_party_licenses/PyQt6/LICENSE" in script
    assert "third_party_licenses/Qt/LICENSE" in script
    assert "usr/share/licenses/openwhisper/LICENSE" in script
    assert "usr/share/licenses/openwhisper/PyQt6-GPL-3.0.txt" in script
    assert "usr/share/licenses/openwhisper/Qt-LGPL-3.0.txt" in script
    assert "PyQt6" in notices
    assert "Qt 6" in notices


def test_linux_audio_guide_is_bundled_and_installed_as_documentation():
    spec = (ROOT / "OpenWhisper.spec").read_text(encoding="utf-8")
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert '"docs" / "linux-system-audio.md"' in spec
    assert '"linux-system-audio.md"), "docs"' in spec
    assert "usr/share/doc/openwhisper/linux-system-audio.md" in script


def test_release_workflow_smokes_advertised_linux_baselines():
    workflow = (ROOT / ".github" / "workflows" / "build-installers.yml").read_text(
        encoding="utf-8"
    )
    assert "ubuntu:22.04" in workflow
    assert "debian:12" in workflow
    assert "archlinux:latest" in workflow
    assert "archlinux" in workflow
    # Combined release candidate ships five application artifacts (Win×2,
    # Linux×2, macOS DMG). The historical four-file inventory is retired.
    assert '-eq 5' in workflow
    assert "OpenWhisper-*-linux-x86_64.pkg.tar.zst" in workflow
    assert "OpenWhisper-*-macos-arm64.dmg" in workflow
    assert "pacman -Qkk openwhisper" in workflow
    assert "GPL-3.0-only" in workflow
    assert "LGPL-3.0-only" in workflow
    assert "/usr/share/licenses/openwhisper/PyQt6-GPL-3.0.txt" in workflow
    assert "/usr/share/licenses/openwhisper/Qt-LGPL-3.0.txt" in workflow


def test_release_artifact_name_is_versioned_and_arch_specific():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "OpenWhisper-$VERSION-linux-amd64.deb" in script
    assert "OpenWhisper-$VERSION-linux-x86_64.pkg.tar.zst" in script
    assert "dpkg-deb --root-owner-group" in script
    assert 'xvfb-run -a "$PYTHON" -m PyInstaller' in script
    assert "patchelf --remove-rpath" in script
    assert '"$EXE_PATH" --version' in script
    assert '"$EXE_PATH" --self-test' in script
    assert 'find -L "$DIST_DIR" -type l' in script
    assert "lintian --fail-on error" in script
    assert "-perm /0022" in script
    assert 'bsdtar --uid 0 --gid 0 --uname root --gname root --format=ustar' in script
    assert '--format=mtree' in script
    assert '-cf - .PKGINFO usr' in script
    assert "zstd -T0 -19" in script
    assert ".PKGINFO" in script
    assert 'gzip -n -9 >"$PACMAN_ROOT/.MTREE"' in script
    assert "libstdc++.so*" in script
    assert "libgcc_s.so*" in script


def test_spec_leaves_system_cxx_runtime_on_linux():
    spec = (ROOT / "OpenWhisper.spec").read_text(encoding="utf-8")
    assert "def _strip_system_cxx(" in spec
    assert "libstdc++.so" in spec
    assert "libgcc_s.so" in spec
    assert "a.binaries = _strip_system_cxx(_strip_qt(a.binaries))" in spec
