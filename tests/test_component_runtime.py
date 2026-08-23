"""Tests for in-process component activation.

Startup activation (``activate_components``) has always registered installed
payloads; ``activate_component`` is the mid-session variant used right after
an install so the component works without restarting the application.
"""
import json
import os
from pathlib import Path

import pytest

from services import component_runtime, components


@pytest.fixture
def component_root(tmp_path, monkeypatch):
    """Point the component system at a temporary directory."""
    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.setattr(components, "components_root", lambda: str(root))
    return root


@pytest.fixture
def recorded_registrations(monkeypatch):
    """Capture DLL-directory registrations instead of touching the loader."""
    registered = []

    def fake_register(path):
        registered.append(path)
        return True

    monkeypatch.setattr(component_runtime, "register_dll_directory", fake_register)
    return registered


def _write_manifest(target: Path, manifest: dict) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / ".installed").write_text(manifest.get("version", ""), encoding="utf-8")


def _make_installed(root: Path, component_id: str, manifest: dict) -> Path:
    """Create a GPU-shaped installed component tree (has ``bin/``)."""
    target = root / component_id
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "fake.dll").write_bytes(b"x" * 1024)
    _write_manifest(target, manifest)
    return target


def _make_meeting_agent_installed(root: Path, manifest: dict) -> Path:
    """Create the flat meeting-agent extract: node.exe + bundle.cjs, no bin/."""
    target = root / components.ComponentId.MEETING_AGENT
    _write_manifest(target, manifest)
    (target / "node.exe").write_bytes(b"MZ")
    (target / "bundle.cjs").write_text("module.exports = {}", encoding="utf-8")
    return target


def test_activation_registers_the_bin_directory(
    component_root, recorded_registrations
):
    _make_installed(component_root, "gpu-accel", {"version": "cuda12.9"})

    ok, reason = component_runtime.activate_component("gpu-accel")

    assert ok is True
    assert reason == ""
    expected = os.path.join(str(component_root / "gpu-accel"), "bin")
    assert recorded_registrations == [expected]


def test_activation_refuses_a_missing_install(
    component_root, recorded_registrations
):
    ok, reason = component_runtime.activate_component("gpu-accel")

    assert ok is False
    assert "not installed" in reason
    assert recorded_registrations == []


def test_activation_refuses_an_incompatible_payload(
    component_root, recorded_registrations
):
    """A payload built for another runtime must not reach the loader path."""
    _make_installed(
        component_root,
        "gpu-accel",
        {"version": "cuda12.9", "component_api": components.COMPONENT_API + 1},
    )

    ok, reason = component_runtime.activate_component("gpu-accel")

    assert ok is False
    assert "different version" in reason
    assert recorded_registrations == []


def test_activation_reports_a_failed_registration(component_root, monkeypatch):
    _make_installed(component_root, "gpu-accel", {"version": "cuda12.9"})
    monkeypatch.setattr(
        component_runtime, "register_dll_directory", lambda path: False
    )

    ok, reason = component_runtime.activate_component("gpu-accel")

    assert ok is False
    assert "library folder" in reason


def test_meeting_agent_activates_without_a_bin_directory(
    component_root, recorded_registrations
):
    _make_meeting_agent_installed(component_root, {"version": "node22-pi1"})

    ok, reason = component_runtime.activate_component(
        components.ComponentId.MEETING_AGENT
    )

    assert ok is True
    assert reason == ""
    assert recorded_registrations == []


def test_speaker_id_activates_without_a_bin_directory(
    component_root, recorded_registrations
):
    target = component_root / components.ComponentId.SPEAKER_ID
    _write_manifest(target, {"version": "wespeaker-1"})
    (target / "model.onnx").write_bytes(b"onnx")

    ok, reason = component_runtime.activate_component(
        components.ComponentId.SPEAKER_ID
    )

    assert ok is True
    assert reason == ""
    assert recorded_registrations == []


def test_gpu_without_bin_reports_missing_library_folder(
    component_root, recorded_registrations
):
    target = component_root / "gpu-accel"
    _write_manifest(target, {"version": "cuda12.9"})

    ok, reason = component_runtime.activate_component("gpu-accel")

    assert ok is False
    assert "library folder" in reason
    assert recorded_registrations == []
