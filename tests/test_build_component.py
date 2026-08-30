"""Unit tests for scripts/build_component meeting-agent helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_component.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_component", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_node_archive_specs_cover_windows_and_linux_arches():
    module = _load_module()
    specs = module.node_archive_specs(version="22.23.2")
    platforms = {entry["platform"] for entry in specs}
    assert platforms == {
        module.PLATFORM_WIN_AMD64,
        module.PLATFORM_LINUX_X86_64,
        module.PLATFORM_LINUX_AARCH64,
    }
    by_platform = {entry["platform"]: entry for entry in specs}
    assert by_platform[module.PLATFORM_WIN_AMD64]["extract"] == "node-exe"
    assert by_platform[module.PLATFORM_WIN_AMD64]["name"].endswith("win-x64.zip")
    assert by_platform[module.PLATFORM_LINUX_X86_64]["extract"] == "node-tar"
    assert (
        by_platform[module.PLATFORM_LINUX_X86_64]["member"]
        == "node-v22.23.2-linux-x64/bin/node"
    )
    assert by_platform[module.PLATFORM_LINUX_AARCH64]["extract"] == "node-tar"
    assert (
        by_platform[module.PLATFORM_LINUX_AARCH64]["member"]
        == "node-v22.23.2-linux-arm64/bin/node"
    )


def test_node_archive_specs_apply_provided_shasums():
    module = _load_module()
    digest = "a" * 64
    specs = module.node_archive_specs(
        version="22.23.2",
        shasums={
            "node-v22.23.2-win-x64.zip": digest,
            "node-v22.23.2-linux-x64.tar.xz": digest,
            "node-v22.23.2-linux-arm64.tar.xz": digest,
        },
    )
    assert all(entry["sha256"] == digest for entry in specs)
