"""Tests for the downloadable component system."""
import hashlib
import io
import json
import os
import tarfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from _version import __version__
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
    speaker_cache = tmp_path / "speaker-models"
    speaker_cache.mkdir()
    monkeypatch.setattr(components, "components_root", lambda: str(root))
    monkeypatch.setattr(components, "speaker_model_cache_dir", lambda: str(speaker_cache))
    monkeypatch.delenv("OPENWHISPER_SPEAKER_MODEL", raising=False)
    return root


def _make_installed(root: Path, component_id: str, manifest: dict) -> Path:
    """Create a complete installed component tree."""
    target = root / component_id
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "fake.dll").write_bytes(b"x" * 1024)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / ".installed").write_text(manifest.get("version", ""), encoding="utf-8")
    return target


# Installation state

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


# Compatibility gates

def test_empty_manifest_is_compatible():
    assert check_compatibility({}) is None


def test_component_api_mismatch_is_rejected():
    reason = check_compatibility({"component_api": components.COMPONENT_API + 1})
    assert reason and "OpenWhisper" in reason


def test_foreign_platform_is_rejected():
    with patch.object(
        components, "current_platform_tag", return_value="win_amd64"
    ):
        reason = check_compatibility({"platform": "linux_x86_64"})
        assert reason and "linux_x86_64" in reason


def test_python_abi_mismatch_is_rejected():
    reason = check_compatibility({"python_abi": "cp999"})
    assert reason and "Python" in reason


def test_matching_abi_is_accepted():
    import sys
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    assert check_compatibility({"python_abi": tag}) is None


# Coordinator

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

    entry = catalog["components"][ComponentId.GPU_ACCEL]["platforms"][
        components.PLATFORM_WIN_AMD64
    ]
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


def test_available_component_ids_by_platform():
    """Windows keeps GPU+agent; Linux offers only the meeting agent."""
    with patch.object(components.sys, "platform", "linux"), patch.object(
        components.platform_module, "machine", return_value="x86_64"
    ):
        assert components.available_component_ids() == (
            ComponentId.MEETING_AGENT,
        )

    with patch.object(components.sys, "platform", "linux"), patch.object(
        components.platform_module, "machine", return_value="aarch64"
    ):
        assert components.available_component_ids() == (
            ComponentId.MEETING_AGENT,
        )

    with patch.object(components.sys, "platform", "darwin"), patch.object(
        components.platform_module, "machine", return_value="arm64"
    ):
        assert components.available_component_ids() == ()
        assert ComponentCoordinator().list_components() == ()

    with patch.object(components.sys, "platform", "win32"), patch.object(
        components.platform_module, "machine", return_value="AMD64"
    ):
        assert components.available_component_ids() == (
            ComponentId.GPU_ACCEL,
            ComponentId.MEETING_AGENT,
            *components.RUNTIME_IDS,
        )


def test_meeting_agent_catalog_is_published():
    """The self-contained sidecar is offered on every supported target."""
    expected = {
        components.PLATFORM_WIN_AMD64: ("node-exe", 101_654_617),
        components.PLATFORM_LINUX_X86_64: ("node-tar", 139_493_705),
        components.PLATFORM_LINUX_AARCH64: ("node-tar", 136_816_417),
    }
    for tag, (node_extract, install_bytes) in expected.items():
        assert components.component_is_published(
            ComponentId.MEETING_AGENT, platform_tag=tag
        ) is True
        entry = components.catalog_entry_for_platform(
            ComponentId.MEETING_AGENT, platform_tag=tag
        )
        assert entry is not None
        assert entry.get("published") is True
        assert entry.get("platform") == tag
        assert int(entry.get("install_bytes") or 0) == install_bytes
        extracts = [archive.get("extract") for archive in entry["archives"]]
        assert node_extract in extracts
        assert "zip" in extracts
        for archive in entry["archives"]:
            digest = str(archive["sha256"]).lower()
            assert len(digest) == 64
            assert digest != components._PLACEHOLDER_SHA256
            int(digest, 16)
            assert str(archive["url"]).startswith("https://")
            assert int(archive["size_bytes"]) > 0

    with patch.object(components.sys, "platform", "win32"), patch.object(
        components.platform_module, "machine", return_value="AMD64"
    ):
        assert ComponentId.MEETING_AGENT in components.available_component_ids()


def test_check_compatibility_rejects_cross_architecture():
    with patch.object(components, "current_platform_tag",
                      return_value=components.PLATFORM_LINUX_AARCH64):
        reason = components.check_compatibility(
            {"component_api": 1, "platform": components.PLATFORM_LINUX_X86_64}
        )
        assert reason and "linux_x86_64" in reason
    with patch.object(components, "current_platform_tag",
                      return_value=components.PLATFORM_LINUX_X86_64):
        assert components.check_compatibility(
            {"component_api": 1, "platform": components.PLATFORM_LINUX_X86_64}
        ) is None


def test_read_manifest_rejects_malformed_shapes(component_root):
    target = components.component_dir(ComponentId.MEETING_AGENT)
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('[1,2,3]')
    assert components.read_manifest(ComponentId.MEETING_AGENT) is None
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"platform": ["win_amd64"], "component_api": "x"}')
    assert components.read_manifest(ComponentId.MEETING_AGENT) is None
    # bool is a subclass of int; must still be rejected.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"component_api": true, "version": false}')
    assert components.read_manifest(ComponentId.MEETING_AGENT) is None
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"component_api": 1, "version": true}')
    assert components.read_manifest(ComponentId.MEETING_AGENT) is None


def test_boolean_manifest_cannot_activate_component(component_root):
    from services.component_runtime import activate_component

    target = Path(components.component_dir(ComponentId.MEETING_AGENT))
    target.mkdir(parents=True, exist_ok=True)
    (target / ".installed").write_text("x", encoding="utf-8")
    (target / "manifest.json").write_text(
        '{"component_api": true, "version": "1"}', encoding="utf-8"
    )
    ok, reason = activate_component(ComponentId.MEETING_AGENT)
    assert ok is False
    assert "manifest" in reason.lower() or "invalid" in reason.lower()


def test_prune_orphans_restores_complete_old_tree(component_root):
    dest = component_root / ComponentId.MEETING_AGENT
    old = component_root / f"{ComponentId.MEETING_AGENT}.old"
    old.mkdir()
    (old / ".installed").write_text("node22-pi1", encoding="utf-8")
    (old / "bundle.cjs").write_text("// ok", encoding="utf-8")
    assert not dest.exists()
    components.prune_orphans()
    assert dest.is_dir()
    assert (dest / ".installed").read_text(encoding="utf-8") == "node22-pi1"
    assert not old.exists()


def test_install_component_restores_previous_on_second_rename_failure(
    component_root, tmp_path, monkeypatch
):
    dest = component_root / ComponentId.MEETING_AGENT
    dest.mkdir()
    (dest / ".installed").write_text("old", encoding="utf-8")
    (dest / "keep.txt").write_text("previous", encoding="utf-8")

    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        # First replace moves destination -> rollback; second (staging -> dest) fails.
        if calls["n"] == 2:
            raise OSError("simulated second rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr(components.os, "replace", flaky_replace)
    monkeypatch.setattr(components, "_download_verified", lambda *a, **k: None)
    monkeypatch.setattr(components, "_validate_component_payload", lambda *a, **k: None)
    monkeypatch.setattr(components, "_check_free_space", lambda *a, **k: None)
    monkeypatch.setattr(
        components, "current_platform_tag", lambda **kwargs: "linux_x86_64"
    )
    monkeypatch.setattr(components.sys, "platform", "linux")

    # Pretend archives already exist in cache by writing empty files after resolve.
    entry = {
        "version": "new",
        "component_api": components.COMPONENT_API,
        "platform": "linux_x86_64",
        "install_bytes": 1,
        "archives": [
            {
                "name": "bundle.zip",
                "url": "https://example.invalid/bundle.zip",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "extract": "zip",
            }
        ],
    }

    def fake_download(url, sha, size, destination, *a, **k):
        Path(destination).write_bytes(b"PK\x03\x04")

    monkeypatch.setattr(components, "_download_verified", fake_download)
    monkeypatch.setattr(
        components, "_safe_extract", lambda *a, **k: None
    )

    with pytest.raises(ComponentError):
        components.install_component(
            ComponentId.MEETING_AGENT,
            entry,
            lambda *a: None,
            threading.Event(),
        )
    assert dest.is_dir()
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "previous"


def test_uninstall_component_raises_when_leftovers_remain(component_root, monkeypatch):
    target = component_root / ComponentId.MEETING_AGENT
    target.mkdir()
    (target / ".installed").write_text("x", encoding="utf-8")

    def fake_replace(src, dst):
        raise OSError("busy")

    monkeypatch.setattr(components.os, "replace", fake_replace)
    monkeypatch.setattr(components, "_rmtree", lambda path: None)
    with pytest.raises(ComponentError, match="fully remove"):
        components.uninstall_component(ComponentId.MEETING_AGENT)


def test_install_component_rejects_foreign_platform_before_download(
    component_root, monkeypatch
):
    called = []

    def boom(*args, **kwargs):
        called.append(True)
        raise AssertionError("_download_verified must not run")

    monkeypatch.setattr(components, "_download_verified", boom)
    monkeypatch.setattr(
        components, "current_platform_tag", lambda **kwargs: "linux_x86_64"
    )
    entry = {
        "version": "node22-pi1",
        "component_api": components.COMPONENT_API,
        "platform": "win_amd64",
        "install_bytes": 10,
        "archives": [
            {
                "name": "node.zip",
                "url": "https://example.invalid/node.zip",
                "sha256": "a" * 64,
                "size_bytes": 5,
                "extract": "node-exe",
            }
        ],
    }
    with pytest.raises(ComponentError, match="win_amd64"):
        components.install_component(
            ComponentId.MEETING_AGENT,
            entry,
            lambda *args: None,
            threading.Event(),
        )
    assert called == []


def test_unpublished_speaker_id_is_never_offered(component_root):
    """Placeholder speaker-id URLs/digests must stay unreachable."""
    with patch.object(components.sys, "platform", "win32"), patch.object(
        components, "_source_speaker_model_path", return_value=None
    ):
        assert components.component_is_published(ComponentId.SPEAKER_ID) is False
        assert ComponentId.SPEAKER_ID not in components.available_component_ids()
        assert components.speaker_model_path() is None


def test_meeting_agent_payload_dir_uses_installed_bundle(component_root):
    """A staged install with bundle.cjs is usable even when unpublished."""
    target = _make_installed(
        component_root,
        ComponentId.MEETING_AGENT,
        {"version": "node22-pi1", "component_api": 1, "platform": "win_amd64"},
    )
    (target / "bundle.cjs").write_text("// stub", encoding="utf-8")
    (target / "node.exe").write_bytes(b"node")
    with patch.object(components, "_source_sidecar_payload_dir", return_value=None), patch.object(
        components, "current_platform_tag", return_value="win_amd64"
    ), patch.object(components.sys, "platform", "win32"):
        assert components.meeting_agent_payload_dir() == str(target)


def test_meeting_agent_payload_dir_ignores_install_without_bundle(component_root):
    """Sentinel alone is not enough — the sidecar bundle must be present."""
    _make_installed(
        component_root,
        ComponentId.MEETING_AGENT,
        {"version": "node22-pi1", "component_api": 1, "platform": "win_amd64"},
    )
    with patch.object(components, "_source_sidecar_payload_dir", return_value=None):
        assert components.meeting_agent_payload_dir() is None


def test_meeting_agent_payload_dir_falls_back_to_source_dist(component_root, tmp_path):
    """From source, a built sidecar/dist/bundle.cjs is a valid payload."""
    dist = tmp_path / "sidecar" / "dist"
    dist.mkdir(parents=True)
    (dist / "bundle.cjs").write_text("// stub", encoding="utf-8")
    with patch.object(components, "is_frozen", return_value=False), patch.object(
        components, "bundle_root", return_value=str(tmp_path)
    ):
        assert components.meeting_agent_payload_dir() == str(dist)


def test_source_sidecar_payload_ignored_when_frozen(component_root, tmp_path):
    """Frozen builds must not pick up a sibling source-tree sidecar/dist."""
    dist = tmp_path / "sidecar" / "dist"
    dist.mkdir(parents=True)
    (dist / "bundle.cjs").write_text("// stub", encoding="utf-8")
    with patch.object(components, "is_frozen", return_value=True), patch.object(
        components, "bundle_root", return_value=str(tmp_path)
    ):
        assert components.meeting_agent_payload_dir() is None


def test_speaker_model_path_uses_unpublished_install(component_root):
    """A staged speaker-id tree is usable even while the catalog is unpublished."""
    target = _make_installed(
        component_root,
        ComponentId.SPEAKER_ID,
        {"version": "wespeaker-v1", "component_api": 1, "platform": "win_amd64"},
    )
    model = target / "voxceleb_resnet34_LM.onnx"
    model.write_bytes(b"onnx")
    with patch.object(components, "_source_speaker_model_path", return_value=None):
        assert components.speaker_model_path() == str(model)


def test_speaker_model_path_honors_env_file(component_root, tmp_path, monkeypatch):
    """OPENWHISPER_SPEAKER_MODEL pointing at a file wins over other sources."""
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"onnx")
    monkeypatch.setenv("OPENWHISPER_SPEAKER_MODEL", str(model))
    assert components.speaker_model_path() == str(model)


def test_speaker_model_path_honors_env_directory(component_root, tmp_path, monkeypatch):
    """OPENWHISPER_SPEAKER_MODEL may be a directory containing the ONNX file."""
    folder = tmp_path / "speaker"
    folder.mkdir()
    model = folder / "model.onnx"
    model.write_bytes(b"onnx")
    monkeypatch.setenv("OPENWHISPER_SPEAKER_MODEL", str(folder))
    assert components.speaker_model_path() == str(model)


def test_speaker_model_path_uses_cache_dir(component_root, tmp_path):
    """A previously downloaded cache file is enough to enable diarization."""
    cache = tmp_path / "speaker-models"
    model = cache / "voxceleb_resnet34_LM.onnx"
    model.write_bytes(b"onnx")
    with patch.object(components, "_source_speaker_model_path", return_value=None):
        assert components.speaker_model_path() == str(model)


def test_speaker_model_path_falls_back_to_source_tree(component_root, tmp_path):
    """From source, models/speaker-id/*.onnx is a valid development payload."""
    models = tmp_path / "models" / "speaker-id"
    models.mkdir(parents=True)
    model = models / "voxceleb_resnet34_LM.onnx"
    model.write_bytes(b"onnx")
    with patch.object(components, "is_frozen", return_value=False), patch.object(
        components, "bundle_root", return_value=str(tmp_path)
    ):
        assert components.speaker_model_path() == str(model)


def test_ensure_speaker_model_returns_existing_without_download(component_root, tmp_path):
    """ensure_speaker_model must not hit the network when a file is already local."""
    cache = tmp_path / "speaker-models"
    model = cache / "voxceleb_resnet34_LM.onnx"
    model.write_bytes(b"onnx")
    with patch.object(components, "_download_speaker_model") as download:
        assert components.ensure_speaker_model() == str(model)
        download.assert_not_called()


def test_ensure_speaker_model_downloads_when_missing(component_root, tmp_path):
    """First meeting start fetches the pinned WeSpeaker ONNX when allowed."""
    cache = tmp_path / "speaker-models"
    dest = cache / "voxceleb_resnet34_LM.onnx"

    def _fake_download():
        dest.write_bytes(b"onnx")
        return str(dest)

    with patch.object(components, "_source_speaker_model_path", return_value=None), \
            patch.object(components, "_speaker_model_download_allowed", return_value=True), \
            patch.object(components, "_download_speaker_model", side_effect=_fake_download):
        assert components.ensure_speaker_model() == str(dest)


def test_ensure_speaker_model_skips_download_when_blocked(component_root):
    """HF_HUB_OFFLINE / policy=never must not start a speaker-model download."""
    with patch.object(components, "_source_speaker_model_path", return_value=None), \
            patch.object(components, "_speaker_model_download_allowed", return_value=False), \
            patch.object(components, "_download_speaker_model") as download:
        assert components.ensure_speaker_model() is None
        download.assert_not_called()


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

    with patch.object(components.sys, "platform", "win32"), patch.object(
        components.platform_module, "machine", return_value="AMD64"
    ):
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


# Archive safety

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


def test_safe_extract_node_exe_flattens_nested_binary(tmp_path):
    archive = tmp_path / "node.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("node-v22.23.2-win-x64/node.exe", b"node-binary")
        zipped.writestr("node-v22.23.2-win-x64/README.md", "ignored")

    out = tmp_path / "out"
    out.mkdir()
    components._safe_extract_node_exe(
        str(archive), str(out), lambda *args: None, threading.Event()
    )

    assert (out / "node.exe").read_bytes() == b"node-binary"
    assert not (out / "node-v22.23.2-win-x64").exists()


def test_safe_extract_node_exe_rejects_unsafe_paths(tmp_path):
    archive = _zip_with_member(tmp_path / "evil.zip", "../node.exe")
    with pytest.raises(ComponentError, match="unsafe path"):
        components._safe_extract_node_exe(
            str(archive), str(tmp_path / "out"), lambda *a: None, threading.Event()
        )


def test_safe_extract_node_exe_errors_when_missing(tmp_path):
    archive = _zip_with_member(tmp_path / "empty.zip", "README.md")
    with pytest.raises(ComponentError, match="node.exe"):
        components._safe_extract_node_exe(
            str(archive), str(tmp_path / "out"), lambda *a: None, threading.Event()
        )


def test_safe_extract_node_tar_writes_executable(tmp_path):
    import tarfile

    archive = tmp_path / "node.tar"
    member = "node-v22.23.2-linux-x64/bin/node"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(name=member)
        data = b"#!/bin/node\n"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        extra = tarfile.TarInfo(name="node-v22.23.2-linux-x64/README.md")
        extra_data = b"ignore"
        extra.size = len(extra_data)
        tar.addfile(extra, io.BytesIO(extra_data))

    out = tmp_path / "out"
    out.mkdir()
    components._safe_extract_node_tar(
        str(archive),
        str(out),
        lambda *a: None,
        threading.Event(),
        member_name=member,
    )
    node_path = out / "node"
    assert node_path.read_bytes().startswith(b"#!")
    assert os.access(node_path, os.X_OK)

    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as tar:
        info = tarfile.TarInfo(name="../node")
        payload = b"bad"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(ComponentError, match="invalid|unsafe path"):
        components._safe_extract_node_tar(
            str(evil),
            str(tmp_path / "out2"),
            lambda *a: None,
            threading.Event(),
            member_name="../node",
        )


def test_validate_meeting_agent_payload_requires_node_and_bundle(tmp_path, monkeypatch):
    with patch.object(components.sys, "platform", "win32"), patch.object(
        components, "current_platform_tag", return_value="win_amd64"
    ):
        with pytest.raises(ComponentError, match="node.exe"):
            components._validate_component_payload(
                ComponentId.MEETING_AGENT, str(tmp_path)
            )

        (tmp_path / "node.exe").write_bytes(b"node")
        with pytest.raises(ComponentError, match="bundle.cjs"):
            components._validate_component_payload(
                ComponentId.MEETING_AGENT, str(tmp_path)
            )

        (tmp_path / "bundle.cjs").write_text("// stub", encoding="utf-8")

        class _Result:
            def __init__(self, code=0, stdout="v22.23.2\n"):
                self.returncode = code
                self.stdout = stdout
                self.stderr = ""

        monkeypatch.setattr(
            components.subprocess,
            "run",
            lambda *a, **k: _Result(),
        )
        components._validate_component_payload(
            ComponentId.MEETING_AGENT, str(tmp_path)
        )


def test_install_component_accepts_verified_meeting_agent_archives(
    component_root, tmp_path, monkeypatch
):
    node_zip = tmp_path / "node.zip"
    with zipfile.ZipFile(node_zip, "w") as archive:
        archive.writestr("node-v22.23.2-win-x64/node.exe", b"node-binary")
    bundle_zip = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle_zip, "w") as archive:
        archive.writestr("bundle.cjs", b"// sidecar")

    sources = {
        "node.zip": node_zip,
        "bundle.zip": bundle_zip,
    }

    def fake_download(_url, _sha, _size, destination, *_args, **_kwargs):
        source = sources[Path(destination).name]
        Path(destination).write_bytes(source.read_bytes())

    monkeypatch.setattr(components, "_download_verified", fake_download)

    class _Result:
        def __init__(self, code=0, stdout="v22.23.2\n"):
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(components.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(components.sys, "platform", "win32")
    monkeypatch.setattr(
        components, "current_platform_tag", lambda **kwargs: "win_amd64"
    )
    entry = {
        "version": "node22-pi1",
        "component_api": components.COMPONENT_API,
        "platform": "win_amd64",
        "install_bytes": 20,
        "archives": [
            {
                "name": "node.zip",
                "url": "https://example.invalid/node.zip",
                "sha256": "unused",
                "size_bytes": 10,
                "extract": "node-exe",
            },
            {
                "name": "bundle.zip",
                "url": "https://example.invalid/bundle.zip",
                "sha256": "unused",
                "size_bytes": 10,
                "extract": "zip",
            },
        ],
    }

    components.install_component(
        ComponentId.MEETING_AGENT,
        entry,
        lambda *args: None,
        threading.Event(),
    )

    installed = component_root / ComponentId.MEETING_AGENT
    assert (installed / ".installed").read_text() == "node22-pi1"
    assert (installed / "node.exe").read_bytes() == b"node-binary"
    assert (installed / "bundle.cjs").read_bytes() == b"// sidecar"


def test_install_component_rejects_meeting_agent_missing_bundle(
    component_root, tmp_path, monkeypatch
):
    node_zip = tmp_path / "node.zip"
    with zipfile.ZipFile(node_zip, "w") as archive:
        archive.writestr("node-v22.23.2-win-x64/node.exe", b"node-binary")
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as archive:
        archive.writestr("README.md", "no bundle")

    sources = {"node.zip": node_zip, "empty.zip": empty_zip}

    def fake_download(_url, _sha, _size, destination, *_args, **_kwargs):
        Path(destination).write_bytes(sources[Path(destination).name].read_bytes())

    monkeypatch.setattr(components, "_download_verified", fake_download)
    monkeypatch.setattr(components.sys, "platform", "win32")
    monkeypatch.setattr(
        components, "current_platform_tag", lambda **kwargs: "win_amd64"
    )
    entry = {
        "version": "node22-pi1",
        "install_bytes": 10,
        "archives": [
            {
                "name": "node.zip",
                "url": "https://example.invalid/node.zip",
                "sha256": "unused",
                "size_bytes": 5,
                "extract": "node-exe",
            },
            {
                "name": "empty.zip",
                "url": "https://example.invalid/empty.zip",
                "sha256": "unused",
                "size_bytes": 5,
                "extract": "zip",
            },
        ],
    }

    with pytest.raises(ComponentError, match="bundle.cjs"):
        components.install_component(
            ComponentId.MEETING_AGENT,
            entry,
            lambda *args: None,
            threading.Event(),
        )


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
    monkeypatch.setattr(components.sys, "platform", "win32")
    monkeypatch.setattr(
        components, "current_platform_tag", lambda **kwargs: "win_amd64"
    )
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


# Disk space

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


def test_prune_orphans_preserves_old_when_restore_fails(component_root, monkeypatch):
    dest = component_root / ComponentId.MEETING_AGENT
    old = component_root / f"{ComponentId.MEETING_AGENT}.old"
    old.mkdir()
    (old / ".installed").write_text("node22-pi1", encoding="utf-8")
    (old / "bundle.cjs").write_text("// ok", encoding="utf-8")

    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(components.os, "replace", boom)
    components.prune_orphans()
    assert old.is_dir()
    assert (old / ".installed").exists()
    assert not dest.exists()


def test_uninstall_removes_stale_old_without_target(component_root):
    old = component_root / f"{ComponentId.MEETING_AGENT}.old"
    old.mkdir()
    (old / "bundle.cjs").write_text("// leftover", encoding="utf-8")
    components.uninstall_component(ComponentId.MEETING_AGENT)
    assert not old.exists()


def test_meeting_agent_payload_rejects_malformed_manifest(component_root, tmp_path):
    target = components.component_dir(ComponentId.MEETING_AGENT)
    os.makedirs(target, exist_ok=True)
    (Path(target) / ".installed").write_text("x", encoding="utf-8")
    (Path(target) / "bundle.cjs").write_text("//", encoding="utf-8")
    node_name = "node.exe" if os.name == "nt" else "node"
    node_path = Path(target) / node_name
    node_path.write_bytes(b"#!/bin/node\n")
    if os.name != "nt":
        os.chmod(node_path, 0o755)
    (Path(target) / "manifest.json").write_text("{not-json", encoding="utf-8")
    with patch.object(components, "is_frozen", return_value=True):
        assert components.meeting_agent_payload_dir() is None


def test_catalog_entry_mutation_does_not_affect_builtin():
    entry = components.catalog_entry_for_platform(
        ComponentId.MEETING_AGENT, platform_tag=components.PLATFORM_WIN_AMD64
    )
    assert entry is not None
    entry["install_bytes"] = 1
    entry["archives"][0]["sha256"] = "0" * 64
    again = components.catalog_entry_for_platform(
        ComponentId.MEETING_AGENT, platform_tag=components.PLATFORM_WIN_AMD64
    )
    assert again["install_bytes"] != 1
    assert again["archives"][0]["sha256"] != "0" * 64


def test_node_tar_rejects_absolute_member_name(tmp_path):
    archive = tmp_path / "node.tar"
    member = "node-v22.23.2-linux-x64/bin/node"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(name=member)
        data = b"#!/bin/node\n"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ComponentError, match="invalid"):
        components._safe_extract_node_tar(
            str(archive),
            str(tmp_path / "out"),
            lambda *a: None,
            threading.Event(),
            member_name="/" + member,
        )


def test_validate_node_version_requires_exact_match(tmp_path, monkeypatch):
    (tmp_path / "node").write_bytes(b"#!/bin/node\n")
    os.chmod(tmp_path / "node", 0o755)
    (tmp_path / "bundle.cjs").write_text("//", encoding="utf-8")

    class _Result:
        def __init__(self, code=0, stdout="v22.23.20\n"):
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(components.sys, "platform", "linux")
    monkeypatch.setattr(components.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(ComponentError, match="unexpected version|reported"):
        components._validate_component_payload(
            ComponentId.MEETING_AGENT, str(tmp_path)
        )
