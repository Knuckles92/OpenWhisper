"""Tests for the downloadable component system."""
import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from services import components
from services.components import (
    ComponentCanceled,
    ComponentCoordinator,
    ComponentError,
    ComponentState,
    check_compatibility,
)


@pytest.fixture
def component_root(tmp_path, monkeypatch):
    """Point the component system at a temporary directory."""
    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.setattr(components, "components_root", lambda: str(root))
    return root


def _make_installed(root: Path, component_id: str, manifest: dict) -> Path:
    """Create a complete installed component tree."""
    target = root / component_id
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "fake.dll").write_bytes(b"x" * 1024)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / ".installed").write_text(manifest.get("version", ""), encoding="utf-8")
    return target


# --- installation state ---------------------------------------------------

def test_not_installed_when_directory_absent(component_root):
    assert components.is_installed("gpu-accel") is False


def test_partial_install_without_sentinel_is_not_installed(component_root):
    """A tree left behind by an interrupted extract must not count as usable."""
    (component_root / "gpu-accel" / "bin").mkdir(parents=True)
    assert components.is_installed("gpu-accel") is False


def test_installed_requires_sentinel(component_root):
    _make_installed(component_root, "gpu-accel", {"version": "1.0"})
    assert components.is_installed("gpu-accel") is True


def test_prune_orphans_removes_staging_and_rollback(component_root):
    (component_root / ".staging" / "gpu-accel.123").mkdir(parents=True)
    (component_root / "gpu-accel.old").mkdir()
    keep = component_root / "gpu-accel"
    keep.mkdir()

    components.prune_orphans()

    assert not (component_root / ".staging").exists()
    assert not (component_root / "gpu-accel.old").exists()
    assert keep.exists()


def test_uninstall_removes_the_tree(component_root):
    _make_installed(component_root, "gpu-accel", {"version": "1.0"})
    components.uninstall_component("gpu-accel")
    assert components.is_installed("gpu-accel") is False


# --- compatibility gates --------------------------------------------------

def test_empty_manifest_is_compatible():
    assert check_compatibility({}) is None


def test_component_api_mismatch_is_rejected():
    reason = check_compatibility({"component_api": components.COMPONENT_API + 1})
    assert reason and "OpenWhisper" in reason


def test_foreign_platform_is_rejected():
    reason = check_compatibility({"platform": "linux_x86_64"})
    assert reason and "linux_x86_64" in reason


def test_python_abi_mismatch_is_rejected():
    reason = check_compatibility({"python_abi": "cp999"})
    assert reason and "Python" in reason


def test_matching_abi_is_accepted():
    import sys
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    assert check_compatibility({"python_abi": tag}) is None


# --- coordinator ----------------------------------------------------------

def test_begin_install_is_exclusive():
    coordinator = ComponentCoordinator()
    assert coordinator.begin_install("gpu-accel") is not None
    # A second request while one is in flight must be refused, not queued.
    assert coordinator.begin_install("gpu-accel") is None

    coordinator.end_install("gpu-accel")
    assert coordinator.begin_install("gpu-accel") is not None


def test_cancel_all_sets_every_flag():
    coordinator = ComponentCoordinator()
    event = coordinator.begin_install("gpu-accel")
    assert not event.is_set()

    coordinator.cancel_all()

    assert event.is_set()


def test_describe_reports_states(component_root):
    coordinator = ComponentCoordinator()
    with patch.object(coordinator, "fetch_catalog", return_value=None):
        info = coordinator.describe("gpu-accel")
        assert info.state == ComponentState.NOT_INSTALLED
        assert info.is_usable is False

        _make_installed(component_root, "gpu-accel", {"version": "1.0"})
        info = coordinator.describe("gpu-accel")
        assert info.state == ComponentState.INSTALLED
        assert info.is_usable is True
        assert info.installed_version == "1.0"


def test_describe_flags_incompatible_installs(component_root):
    coordinator = ComponentCoordinator()
    _make_installed(
        component_root, "gpu-accel",
        {"version": "1.0", "component_api": components.COMPONENT_API + 1},
    )
    with patch.object(coordinator, "fetch_catalog", return_value=None):
        info = coordinator.describe("gpu-accel")

    assert info.state == ComponentState.INCOMPATIBLE
    assert info.is_usable is False
    assert info.reason


def test_describe_survives_catalog_failure(component_root):
    """An unreachable catalog must never hide an installed component."""
    coordinator = ComponentCoordinator()
    _make_installed(component_root, "gpu-accel", {"version": "1.0"})
    with patch.object(coordinator, "fetch_catalog", side_effect=OSError("offline")):
        with patch.object(coordinator, "catalog_entry", return_value=None):
            info = coordinator.describe("gpu-accel")

    assert info.state == ComponentState.INSTALLED


# --- archive safety -------------------------------------------------------

def _zip_with_member(path: Path, member_name: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member_name, "payload")
    return path


def test_safe_extract_rejects_parent_traversal(tmp_path):
    archive = _zip_with_member(tmp_path / "evil.zip", "../escaped.txt")
    with pytest.raises(ComponentError, match="unsafe path"):
        components._safe_extract(
            str(archive), str(tmp_path / "out"), lambda *a: None, threading.Event()
        )


def test_safe_extract_rejects_absolute_paths(tmp_path):
    archive = _zip_with_member(tmp_path / "evil.zip", "/etc/passwd")
    with pytest.raises(ComponentError, match="unsafe path"):
        components._safe_extract(
            str(archive), str(tmp_path / "out"), lambda *a: None, threading.Event()
        )


def test_safe_extract_writes_normal_members(tmp_path):
    archive = _zip_with_member(tmp_path / "ok.zip", "bin/lib.dll")
    out = tmp_path / "out"
    components._safe_extract(str(archive), str(out), lambda *a: None, threading.Event())
    assert (out / "bin" / "lib.dll").read_text() == "payload"


def test_safe_extract_honors_cancellation(tmp_path):
    archive = _zip_with_member(tmp_path / "ok.zip", "bin/lib.dll")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ComponentCanceled):
        components._safe_extract(
            str(archive), str(tmp_path / "out"), lambda *a: None, cancel
        )


# --- disk space -----------------------------------------------------------

def test_free_space_check_raises_when_short(component_root):
    class _Usage:
        free = 1024

    with patch("services.components.shutil.disk_usage", return_value=_Usage()):
        with pytest.raises(ComponentError, match="disk space"):
            components._check_free_space(10 * 1024 * 1024 * 1024)


def test_free_space_check_passes_when_ample(component_root):
    class _Usage:
        free = 100 * 1024 * 1024 * 1024

    with patch("services.components.shutil.disk_usage", return_value=_Usage()):
        components._check_free_space(1024)  # must not raise
