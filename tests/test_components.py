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
    ComponentId,
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
    with patch.object(coordinator, "fetch_catalog", return_value=None), patch.object(
        components, "gpu_runtime_available", return_value=False
    ):
        info = coordinator.describe("gpu-accel")
        assert info.state == ComponentState.NOT_INSTALLED
        assert info.is_usable is False

        _make_installed(component_root, "gpu-accel", {"version": "1.0"})
        info = coordinator.describe("gpu-accel")
        assert info.state == ComponentState.INSTALLED
        assert info.is_usable is True
        assert info.installed_version == "1.0"


def test_describe_recognizes_an_existing_cuda_setup(component_root):
    coordinator = ComponentCoordinator()
    with patch.object(coordinator, "fetch_catalog", return_value=None), patch.object(
        components, "gpu_runtime_available", return_value=True
    ):
        info = coordinator.describe(ComponentId.GPU_ACCEL)

    assert info.state == ComponentState.EXTERNAL
    assert info.is_usable is True
    assert "existing setup" in info.reason


def test_catalog_needs_no_network(monkeypatch):
    """The catalog ships in the app; nothing is fetched to read it.

    Guards against reintroducing a hosted catalog: the previous design fetched
    one from the project website, which served its SPA shell for that path, so
    the request never succeeded and only cost a warning per session.
    """
    def _fail(*args, **kwargs):
        raise AssertionError("the catalog must not open a network connection")

    monkeypatch.setattr(components, "_open", _fail)
    catalog = ComponentCoordinator().fetch_catalog()

    entry = catalog["components"][ComponentId.GPU_ACCEL]
    assert entry["archives"]
    assert all(
        archive["url"].startswith("https://files.pythonhosted.org/")
        for archive in entry["archives"]
    )
    assert all(archive["sha256"] for archive in entry["archives"])


def test_built_in_catalog_ships_no_cudnn_wheel():
    """The payload must not regain the ~740 MB library CTranslate2 never loads."""
    names = [
        archive["name"].lower()
        for archive in components._BUILTIN_GPU_ARCHIVES
    ]
    assert names
    assert not any("cudnn" in name for name in names)
    assert any("cublas" in name for name in names)


def test_available_component_ids_is_empty_off_windows():
    """Linux and macOS have no installable component payload."""
    with patch.object(components.sys, "platform", "linux"):
        assert components.available_component_ids() == ()
        assert ComponentCoordinator().list_components() == ()

    with patch.object(components.sys, "platform", "win32"):
        assert components.available_component_ids() == (
            ComponentId.GPU_ACCEL,
            ComponentId.MEETING_AGENT,
            ComponentId.SPEAKER_ID,
        )


def test_gpu_runtime_probes_shared_objects_on_linux():
    """A Linux user with the pip wheels installed counts as GPU-capable."""
    loaded = []

    class _FakeCtypes:
        RTLD_GLOBAL = 0

        @staticmethod
        def CDLL(name, *args, **kwargs):
            loaded.append(name)
            return object()

    with patch.object(components.sys, "platform", "linux"), patch.dict(
        "sys.modules", {"ctypes": _FakeCtypes}
    ):
        assert components.gpu_runtime_available() is True

    assert loaded == list(components._REQUIRED_GPU_SHARED_OBJECTS)


def test_gpu_runtime_unavailable_on_macos():
    """faster-whisper has no Metal/MPS backend, so never claim GPU support."""
    with patch.object(components.sys, "platform", "darwin"):
        assert components.gpu_runtime_available() is False


def test_a_superseded_install_is_offered_the_update(component_root):
    """A shipped-version difference must reach the user as a choice.

    An earlier guard suppressed this whenever a remote catalog fetch had failed —
    which was always, since no catalog was ever served — so nobody was offered
    the slimmer payload and everyone kept ~1 GB of unused cuDNN on disk.
    """
    coordinator = ComponentCoordinator()
    _make_installed(
        component_root, "gpu-accel", {"version": "cuda12.9-cudnn9.24"}
    )

    info = coordinator.describe("gpu-accel")

    assert info.installed_version == "cuda12.9-cudnn9.24"
    assert info.available_version == components.GPU_COMPONENT_VERSION
    assert info.state == ComponentState.UPDATE_AVAILABLE


def test_update_available_explains_the_disk_it_frees(component_root):
    """The user chooses whether to update, so the row must state the payoff."""
    coordinator = ComponentCoordinator()
    _make_installed(component_root, "gpu-accel", {"version": "old"})
    entry = {"version": "new", "install_bytes": 0, "archives": []}

    with patch.object(coordinator, "fetch_catalog", return_value={
        "schema": 1, "components": {"gpu-accel": entry},
    }):
        info = coordinator.describe("gpu-accel")

    assert info.state == ComponentState.UPDATE_AVAILABLE
    assert "frees" in info.reason


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


def test_describe_survives_a_missing_catalog_entry(component_root):
    """What is on disk decides installed-ness, not what the catalog knows.

    A payload dropped from the catalog must not make an install vanish from the
    UI, leaving the user with files they cannot see or remove.
    """
    coordinator = ComponentCoordinator()
    _make_installed(component_root, "gpu-accel", {"version": "1.0"})
    with patch.object(coordinator, "catalog_entry", return_value=None):
        info = coordinator.describe("gpu-accel")

    assert info.state == ComponentState.INSTALLED
    assert info.installed_version == "1.0"


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


def test_safe_extract_nvidia_wheel_flattens_only_dlls(tmp_path):
    wheel = tmp_path / "nvidia.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("nvidia/cublas/bin/cublas64_12.dll", b"cublas")
        archive.writestr("nvidia/cublas/__init__.py", "ignored")

    out = tmp_path / "out"
    components._safe_extract_nvidia_wheel(
        str(wheel), str(out), lambda *args: None, threading.Event()
    )

    assert (out / "bin" / "cublas64_12.dll").read_bytes() == b"cublas"
    assert not (out / "nvidia").exists()


def test_validate_gpu_payload_rejects_missing_core_dlls(tmp_path):
    """cuBLAS is the one library CTranslate2 cannot run on the GPU without."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "cudart64_12.dll").write_bytes(b"runtime")

    with pytest.raises(ComponentError, match="cublas64_12.dll"):
        components._validate_component_payload(ComponentId.GPU_ACCEL, str(tmp_path))


def test_validate_gpu_payload_does_not_require_cudnn(tmp_path):
    """CTranslate2 4.8 never loads cuDNN, so a payload without it is valid.

    Guards the ~740 MB saving: reintroducing a cuDNN requirement here would
    silently make every published component fail verification.
    """
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "cublas64_12.dll").write_bytes(b"cublas")

    components._validate_component_payload(ComponentId.GPU_ACCEL, str(tmp_path))


def test_install_component_accepts_verified_nvidia_wheels(
    component_root, tmp_path, monkeypatch
):
    cublas_wheel = tmp_path / "cublas.whl"
    with zipfile.ZipFile(cublas_wheel, "w") as archive:
        archive.writestr("nvidia/cublas/bin/cublas64_12.dll", b"cublas")
    nvrtc_wheel = tmp_path / "nvrtc.whl"
    with zipfile.ZipFile(nvrtc_wheel, "w") as archive:
        archive.writestr("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll", b"nvrtc")

    sources = {
        "cublas.whl": cublas_wheel,
        "nvrtc.whl": nvrtc_wheel,
    }

    def fake_download(_url, _sha, _size, destination, *_args, **_kwargs):
        source = sources[Path(destination).name]
        Path(destination).write_bytes(source.read_bytes())

    monkeypatch.setattr(components, "_download_verified", fake_download)
    entry = {
        "version": "test",
        "component_api": components.COMPONENT_API,
        "platform": "win_amd64",
        "install_bytes": 12,
        "archives": [
            {
                "name": "cublas.whl",
                "url": "https://example.invalid/cublas.whl",
                "sha256": "unused",
                "size_bytes": 6,
                "extract": "nvidia-wheel",
            },
            {
                "name": "nvrtc.whl",
                "url": "https://example.invalid/nvrtc.whl",
                "sha256": "unused",
                "size_bytes": 6,
                "extract": "nvidia-wheel",
            },
        ],
    }

    components.install_component(
        ComponentId.GPU_ACCEL,
        entry,
        lambda *args: None,
        threading.Event(),
    )

    installed = component_root / ComponentId.GPU_ACCEL
    assert (installed / ".installed").read_text() == "test"
    assert (installed / "bin" / "cublas64_12.dll").read_bytes() == b"cublas"
    assert (installed / "bin" / "nvrtc64_120_0.dll").read_bytes() == b"nvrtc"


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
