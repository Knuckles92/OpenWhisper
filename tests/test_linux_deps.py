"""Tests for the Linux shared-library preflight."""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from services.linux_deps import (
    MEETING_AUDIO_LIBRARIES,
    OPENWHISPER_RELEASES_URL,
    check_linux_dependencies,
    detect_package_family,
    install_command,
    meeting_audio_remediation,
    missing_libraries,
    reinstall_commands,
)


class TestLinuxDeps(unittest.TestCase):
    def test_detect_package_family(self):
        self.assertEqual(detect_package_family('ID=ubuntu\nID_LIKE=debian\n'), "apt")
        self.assertEqual(detect_package_family('ID=fedora\n'), "dnf")
        self.assertEqual(detect_package_family('ID=arch\n'), "pacman")
        self.assertEqual(
            detect_package_family('ID=somethingelse\n', fallback="unknown"),
            "unknown",
        )

    def test_meeting_audio_libpulse_mapping(self):
        soname, packages = MEETING_AUDIO_LIBRARIES[0]
        self.assertEqual(soname, "libpulse.so.0")
        self.assertEqual(packages["apt"], "libpulse0")
        self.assertEqual(packages["dnf"], "pulseaudio-libs")
        self.assertEqual(packages["pacman"], "libpulse")

    def test_meeting_audio_remediation_by_family(self):
        apt = meeting_audio_remediation("libpulse_missing", "apt")
        self.assertIn("libpulse0", " ".join(apt.commands))
        unknown = meeting_audio_remediation("audio_server_unavailable", "unknown")
        # Stack-neutral diagnostics remain available even without a package family.
        joined = " ".join(unknown.commands).lower()
        self.assertIn("systemctl", joined)
        self.assertIn("pactl", joined)
        self.assertNotIn("apt install", joined)

    def test_pactl_missing_remediation_by_family(self):
        expected = {
            "apt": "pulseaudio-utils",
            "dnf": "pulseaudio-utils",
            "pacman": "libpulse",
        }
        for family, package in expected.items():
            remediation = meeting_audio_remediation("pactl_missing", family)
            joined = " ".join(remediation.commands)
            self.assertIn(package, joined)
            self.assertIn("pactl info", remediation.verification)

    def test_remediation_never_pushes_pipewire_onto_pulse_for_transient_errors(self):
        for reason in (
            "default_sink_missing",
            "monitor_source_missing",
            "monitor_open_failed",
        ):
            pulse = meeting_audio_remediation(reason, "apt", server_kind="pulse")
            joined = " ".join(pulse.commands).lower()
            self.assertNotIn("pipewire-pulse", joined)
            self.assertNotIn("install -y pipewire", joined)
            self.assertIn("pactl", joined)

    def test_remediation_pipewire_install_only_for_missing_stack_cases(self):
        missing = meeting_audio_remediation(
            "pipewire_pulse_missing", "apt", server_kind="unavailable"
        )
        self.assertTrue(any("pipewire-pulse" in c for c in missing.commands))

    def test_audio_server_unavailable_is_stack_neutral(self):
        for family in ("apt", "dnf", "pacman", "unknown"):
            unavailable = meeting_audio_remediation(
                "audio_server_unavailable", family, server_kind="unavailable"
            )
            joined = " ".join(unavailable.commands).lower()
            self.assertNotIn("apt install", joined)
            self.assertNotIn("dnf install", joined)
            self.assertNotIn("pacman -s", joined)
            self.assertNotIn("install -y pipewire", joined)
            # No cross-stack auto-start via cmd_a || cmd_b.
            self.assertNotIn("||", joined)
            self.assertNotIn("systemctl --user start pipewire", joined)
            self.assertNotIn("systemctl --user restart pipewire", joined)
            self.assertIn("systemctl", joined)
            self.assertIn("pactl", joined)
            self.assertIn("pulseaudio", joined)
            # Mutually exclusive recovery guidance lives in the note, not as a
            # single pasteable fallback command.
            self.assertIn("||", unavailable.restart_note)

    def test_source_install_missing_soundcard_keeps_pip_advice(self):
        remediation = meeting_audio_remediation(
            "soundcard_missing", "apt", packaged=False
        )
        self.assertIn("python -m pip install 'soundcard>=0.4.3'", remediation.commands)
        self.assertIn("same environment", remediation.explanation)
        self.assertIn("python -m pip show soundcard", remediation.verification)

    def test_packaged_missing_soundcard_says_reinstall_not_pip(self):
        from _version import __version__

        expected = {
            "apt": f"sudo apt install --reinstall ./OpenWhisper-{__version__}-linux-amd64.deb",
            "pacman": f"sudo pacman -U ./OpenWhisper-{__version__}-linux-x86_64.pkg.tar.zst",
            "dnf": None,
            "unknown": None,
        }
        for family, command in expected.items():
            with self.subTest(family=family):
                remediation = meeting_audio_remediation(
                    "soundcard_missing", family, packaged=True
                )
                text = " ".join(
                    (remediation.title, remediation.explanation)
                    + remediation.commands
                    + remediation.verification
                ).lower()
                self.assertNotIn("pip", text)
                self.assertNotIn("python", text)
                self.assertNotIn("same environment", text)
                self.assertIn("reinstall", text)
                self.assertIn(OPENWHISPER_RELEASES_URL, remediation.explanation)
                self.assertEqual(
                    remediation.commands, (command,) if command else ()
                )

    def test_packaged_is_detected_from_frozen_build(self):
        with patch.object(sys, "frozen", True, create=True):
            frozen = meeting_audio_remediation("soundcard_missing", "apt")
        source = meeting_audio_remediation("soundcard_missing", "apt")
        self.assertIn("--reinstall", " ".join(frozen.commands))
        self.assertIn("pip install", " ".join(source.commands))

    def test_packaged_libpulse_check_needs_no_python(self):
        packaged = meeting_audio_remediation(
            "libpulse_missing", "apt", packaged=True
        )
        self.assertIn("libpulse0", " ".join(packaged.commands))
        self.assertNotIn("python", " ".join(packaged.verification))
        source = meeting_audio_remediation("libpulse_missing", "apt", packaged=False)
        self.assertIn("python -c", " ".join(source.verification))

    def test_reinstall_commands_match_release_asset_names(self):
        self.assertEqual(
            reinstall_commands("apt", "9.8.7"),
            ("sudo apt install --reinstall ./OpenWhisper-9.8.7-linux-amd64.deb",),
        )
        self.assertEqual(
            reinstall_commands("pacman", "9.8.7"),
            ("sudo pacman -U ./OpenWhisper-9.8.7-linux-x86_64.pkg.tar.zst",),
        )
        self.assertEqual(reinstall_commands("dnf", "9.8.7"), ())
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "build_installer.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('OpenWhisper-$VERSION-linux-amd64.deb"', script)
        self.assertIn('OpenWhisper-$VERSION-linux-x86_64.pkg.tar.zst"', script)

    def test_install_command_matches_family(self):
        missing = [("libEGL.so.1", {"apt": "libegl1", "dnf": "mesa-libEGL", "pacman": "libgl"})]
        self.assertEqual(install_command(missing, "apt"), "sudo apt install -y libegl1")
        self.assertEqual(install_command(missing, "dnf"), "sudo dnf install -y mesa-libEGL")
        self.assertEqual(
            install_command(missing, "pacman"),
            "sudo pacman -S --needed libgl",
        )

    def test_missing_libraries_reports_unloadable_sonames(self):
        required = (
            ("libEGL.so.1", {"apt": "libegl1"}),
            ("libportaudio.so.2", {"apt": "libportaudio2"}),
        )
        with patch(
            "services.linux_deps.probe_library",
            side_effect=lambda name: name != "libEGL.so.1",
        ):
            missing = missing_libraries(required)
        self.assertEqual([soname for soname, _ in missing], ["libEGL.so.1"])

    def test_check_skips_non_linux(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(check_linux_dependencies(), 0)

    def test_check_prints_install_line_on_linux(self):
        stream = io.StringIO()
        required = (("libEGL.so.1", {"apt": "libegl1", "dnf": "mesa-libEGL", "pacman": "libgl"}),)
        with patch.object(sys, "platform", "linux"), patch(
            "services.linux_deps.REQUIRED_LIBRARIES", required
        ), patch("services.linux_deps.probe_library", return_value=False), patch(
            "services.linux_deps.detect_package_family", return_value="apt"
        ):
            status = check_linux_dependencies(stream=stream)
        self.assertEqual(status, 1)
        output = stream.getvalue()
        self.assertIn("libEGL.so.1", output)
        self.assertIn("sudo apt install -y libegl1", output)
        self.assertNotIn("Traceback", output)
