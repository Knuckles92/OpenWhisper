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


def _make_installed(root: Path, component_id: str, manifest: dict) -> Path:
    """Create a complete installed component tree."""
    target = root / component_id
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "fake.dll").write_bytes(b"x" * 1024)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / ".installed").write_text(manifest.get("version", ""), encoding="utf-8")
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
