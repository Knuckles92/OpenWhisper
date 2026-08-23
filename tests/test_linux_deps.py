"""Tests for the Linux shared-library preflight."""

import io
import sys
import unittest
from unittest.mock import patch

from services.linux_deps import (
    check_linux_dependencies,
    detect_package_family,
    install_command,
    missing_libraries,
)


class TestLinuxDeps(unittest.TestCase):
    def test_detect_package_family(self):
        self.assertEqual(detect_package_family('ID=ubuntu\nID_LIKE=debian\n'), "apt")
        self.assertEqual(detect_package_family('ID=fedora\n'), "dnf")
        self.assertEqual(detect_package_family('ID=arch\n'), "pacman")

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
