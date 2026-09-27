"""Verified host dependency setup over TLS, including the client recovery UI."""

# ruff: noqa: F811
import threading
from types import SimpleNamespace

import pytest

from services import component_runtime, components, gpu_info
from services.remote_asr.client import RemoteEngineError
from services.remote_asr.dependencies import HostRuntimeInstaller, dependency_options
from services.settings import SettingsKey, settings_manager
from tests.test_remote_engine import _connect, _host_controller, _wait_for
from tests.test_remote_model_management import (  # noqa: F401
    _pump_until,
    downloads,
    managed,
)


@pytest.fixture
def runtimes(monkeypatch):
    state = SimpleNamespace(
        installed=set(),
        calls=[],
        entered=threading.Event(),
        release=threading.Event(),
        activation=True,
        error=False,
        incomplete=False,
    )
    state.release.set()
    coordinator = components.ComponentCoordinator()
    monkeypatch.setattr(components, "component_coordinator", coordinator)
    monkeypatch.setattr(components, "current_platform_tag", lambda: "linux_x86_64")
    monkeypatch.setattr(components, "is_installed", lambda key: key in state.installed)
    monkeypatch.setattr(
        components,
        "gpu_runtime_available",
        lambda: "gpu-accel" in state.installed and state.activation,
    )
    monkeypatch.setattr(
        gpu_info,
        "nvidia_gpu",
        lambda: gpu_info.NvidiaGpu("NVIDIA RTX 2060", 6144, (7, 5)),
    )
    monkeypatch.setattr(
        component_runtime,
        "activate_component",
        lambda key: (state.activation, "private-host-path"),
    )

    def install(key, entry, progress, cancel):
        state.calls.append((key, entry))
        progress("Downloading", 25, 100)
        state.entered.set()
        assert state.release.wait(5)
        if state.error:
            raise OSError("secret-token in https://secret@example.com/private/path")
        if cancel.is_set():
            raise components.ComponentCanceled()
        if not state.incomplete:
            state.installed.add(key)
        progress("Installing", 100, 100)

    monkeypatch.setattr(components, "install_component", install)
    state.coordinator = coordinator
    yield state
    state.release.set()
    assert _wait_for(lambda: not coordinator.is_any_installing())


def test_linux_catalog_explains_cpu_gpu_and_unsupported_engines(managed, runtimes):
    managed.service.set_model_management(True)
    result = managed.service.remote_model_request("model_catalog")
    models = {item["model"]: item for item in result["models"]}
    assert result["can_install_runtime"] and result["installation"] == {}
    for model in ("nemotron-3.5", "parakeet-v3"):
        cpu, gpu = models[model]["dependencies"]
        assert cpu["installable"] and gpu["installable"]
        assert gpu["component"] == "asr-nvidia-cuda" and gpu["download_bytes"] > 0
        assert not gpu["ready"]
    assert "platform" in models["qwen-0.6b"]["dependencies"][0]["reason"]
    assert not models["qwen-0.6b"]["dependencies"][0]["installable"]
    assert models["tiny"]["dependencies"][1]["component"] == "gpu-accel"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"family": [], "model": "nemotron-3.5", "device": "cuda"},
        {"family": "nemotron", "model": "nemotron-3.5", "device": []},
        {
            "family": "nemotron",
            "model": "nemotron-3.5",
            "device": "cuda",
            "url": "https://untrusted.invalid",
        },
        {
            "family": "nemotron",
            "model": "nemotron-3.5",
            "device": "cuda",
            "component": "meeting-agent",
        },
        {"family": "nemotron", "model": "../../arbitrary", "device": "cuda"},
    ],
)
def test_install_accepts_only_bundled_model_device_requests(managed, runtimes, fields):
    managed.service.set_model_management(True)
    with pytest.raises(RuntimeError):
        managed.service.remote_model_request("install_runtime", **fields)
    assert not runtimes.calls


def test_install_survives_disconnect_reports_progress_and_uses_host_archive(
    managed, runtimes
):
    managed.service.set_model_management(True)
    runtimes.release.clear()
    request = managed.service.remote_model_request
    try:
        response = request(
            "install_runtime", family="nemotron", model="nemotron-3.5", device="cuda"
        )
        assert response["installation"]["state"] == "installing"
        assert runtimes.entered.wait(2)
        status = request("model_catalog")["installation"]
        assert (status["done"], status["total"]) == (25, 100)
        assert (
            request(
                "install_runtime", family="parakeet", model="parakeet-v3", device="cuda"
            )["installation"]
            == status
        )
        with pytest.raises(RuntimeError, match="in progress"):
            request(
                "install_runtime", family="nemotron", model="nemotron-3.5", device="cpu"
            )
        managed.service.set_model_management(False)
        runtimes.release.set()
        assert _wait_for(
            lambda: managed.service._runtime_installer.job().get("state") == "complete"
        )
        assert managed.engine.model == "parakeet-v3", (
            "Installing must not switch the engine"
        )
        managed.service.set_model_management(True)
        catalog = request("model_catalog")
        model = next(
            item for item in catalog["models"] if item["model"] == "nemotron-3.5"
        )
        assert model["dependencies"][1]["ready"]
        assert runtimes.calls[0][1]["platform"] == "linux_x86_64"
        assert runtimes.calls[0][1]["archives"][0]["sha256"]
        request(
            "install_runtime", family="nemotron", model="nemotron-3.5", device="cuda"
        )
        assert len(runtimes.calls) == 1
    finally:
        runtimes.release.set()


def test_install_rechecks_permission_and_revocation_on_existing_connection(
    managed, runtimes
):
    managed.service.set_model_management(True)
    connection = _connect(managed.host, managed.pairing)
    try:
        assert connection.ready["capabilities"]["runtime_installation"] is True
        managed.service.set_model_management(False)
        with pytest.raises(RuntimeError, match="disabled"):
            connection.request(
                "install_runtime",
                family="nemotron",
                model="nemotron-3.5",
                device="cuda",
            )
        managed.service.set_model_management(True)
        managed.host.registry.remove(managed.pairing.device_id)
        with pytest.raises(RuntimeError):
            connection.request(
                "install_runtime",
                family="nemotron",
                model="nemotron-3.5",
                device="cuda",
            )
        assert not runtimes.calls
    finally:
        connection.close()


def test_legacy_host_explains_runtime_upgrade(managed, runtimes, monkeypatch):
    managed.service.set_model_management(True)
    send = managed.host._send

    def legacy(ws, message):
        message.get("capabilities", {}).pop("runtime_installation", None)
        send(ws, message)

    monkeypatch.setattr(managed.host, "_send", legacy)
    with pytest.raises(RemoteEngineError, match="Update OpenWhisper"):
        managed.service.remote_model_request(
            "install_runtime", family="nemotron", model="nemotron-3.5", device="cuda"
        )
    assert not runtimes.calls


def test_install_rejects_missing_or_older_gpu_and_unsupported_platform(
    runtimes, monkeypatch
):
    installer = HostRuntimeInstaller()
    monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda: None)
    with pytest.raises(RuntimeError, match="driver"):
        installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
    monkeypatch.setattr(
        gpu_info, "nvidia_gpu", lambda: gpu_info.NvidiaGpu("GTX 1050 Ti", 4096, (6, 1))
    )
    # Without a Vulkan release for the host's platform, older GPUs stay on CPU.
    monkeypatch.setattr(components, "current_platform_tag", lambda: "win_amd64")
    with pytest.raises(RuntimeError, match="Turing"):
        installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
    monkeypatch.setattr(components, "current_platform_tag", lambda: "linux_x86_64")
    assert dependency_options("local_whisper")[1]["installable"]
    with pytest.raises(RuntimeError, match="platform"):
        installer.install("qwen_asr", "qwen-0.6b", "cpu", "laptop")
    assert not runtimes.calls


def test_older_gpu_linux_host_installs_the_vulkan_runtime(runtimes, monkeypatch):
    monkeypatch.setattr(
        gpu_info,
        "nvidia_gpu",
        lambda: gpu_info.NvidiaGpu("NVIDIA GeForce GTX 1050 Ti", 4096, (6, 1)),
    )
    for family in ("parakeet", "nemotron"):
        _cpu, gpu = dependency_options(family)
        assert (gpu["device"], gpu["component"], gpu["label"]) == (
            "cuda", "asr-nvidia-vulkan", "NVIDIA Speech GPU (Vulkan)"
        )
        assert gpu["installable"] and not gpu["reason"]
        assert gpu["download_bytes"] == 18_014_113
    finished = threading.Event()
    installer = HostRuntimeInstaller(finished.set)
    installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
    assert finished.wait(2) and installer.job()["state"] == "complete"
    assert [key for key, _entry in runtimes.calls] == ["asr-nvidia-vulkan"]
    _cpu, gpu = dependency_options("nemotron")
    assert gpu["ready"] and not gpu["installable"]


def test_local_install_claim_prevents_duplicate_remote_install(runtimes):
    installer = HostRuntimeInstaller()
    assert runtimes.coordinator.begin_install("asr-nvidia-cuda")
    try:
        with pytest.raises(RuntimeError, match="already"):
            installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
        assert not runtimes.calls
    finally:
        runtimes.coordinator.end_install("asr-nvidia-cuda")


@pytest.mark.parametrize("failure", ["error", "incomplete", "cancel", "activation"])
def test_install_failure_releases_claim_and_reports_recovery(runtimes, failure):
    finished = threading.Event()
    installer = HostRuntimeInstaller(finished.set)
    if failure in ("error", "incomplete"):
        setattr(runtimes, failure, True)
    elif failure == "activation":
        runtimes.activation = False
    else:
        runtimes.release.clear()
    installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
    if failure == "cancel":
        assert runtimes.entered.wait(2)
        runtimes.coordinator.cancel_install("asr-nvidia-cuda")
        runtimes.release.set()
    assert finished.wait(2)
    job = installer.job()
    assert job["state"] == ("restart_required" if failure == "activation" else "failed")
    assert "secret" not in job["error"] and "private-host" not in job["error"]
    assert not runtimes.coordinator.is_any_installing()
    if failure == "error":
        runtimes.error = False
        finished.clear()
        installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
        assert finished.wait(2) and installer.job()["state"] == "complete"


def test_thread_start_failure_is_recoverable(runtimes, monkeypatch):
    installer = HostRuntimeInstaller()
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _: (_ for _ in ()).throw(RuntimeError("private error")),
    )
    with pytest.raises(RuntimeError, match="Couldn't start"):
        installer.install("nemotron", "nemotron-3.5", "cuda", "laptop")
    assert installer.job()["state"] == "failed"
    assert not runtimes.coordinator.is_any_installing()


def test_select_device_requires_cached_model_ready_runtime_and_permission(
    managed, runtimes, downloads
):
    request = managed.service.remote_model_request
    managed.service.set_model_management(True)
    with pytest.raises(RuntimeError, match="Download"):
        request("select_model", family="nemotron", model="nemotron-3.5", device="cuda")
    downloads.cached.add("nemotron-3.5")
    with pytest.raises(RuntimeError, match="Install"):
        request("select_model", family="nemotron", model="nemotron-3.5", device="cuda")
    runtimes.installed.add("asr-nvidia-cuda")
    result = request(
        "select_model", family="nemotron", model="nemotron-3.5", device="cuda"
    )
    assert result["engine"]["model"] == "nemotron-3.5"
    managed.service.set_model_management(False)
    connection = _connect(managed.host, managed.pairing)
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            connection.request(
                "select_model", family="nemotron", model="nemotron-3.5", device="cuda"
            )
    finally:
        connection.close()


def test_controller_applies_target_device_and_refuses_activity(runtimes, downloads):
    runtimes.installed.add("asr-nvidia-cuda")
    downloads.cached.add("nemotron-3.5")
    controller = _host_controller("nemotron")
    controller._reload_in_flight = False
    assert (
        controller._switch_engine_to(
            "nemotron", "nemotron-3.5", "laptop", target_device="cuda"
        )
        is None
    )
    assert settings_manager.get(SettingsKey.LOCAL_ASR_DEVICES)["nemotron"] == "cuda"
    controller.reload_whisper_model.assert_called_once()
    controller.recorder.is_recording = True
    assert "transcribing" in controller._switch_engine_to(
        "nemotron", "nemotron-3.5", "laptop", target_device="cpu"
    )
    assert settings_manager.get(SettingsKey.LOCAL_ASR_DEVICES)["nemotron"] == "cuda"


def test_browser_installs_then_enables_selected_gpu_without_changing_local_settings(
    managed, runtimes, downloads, monkeypatch
):
    from PyQt6.QtWidgets import QMessageBox

    from ui_qt.dialogs.remote_models import RemoteModelsDialog

    managed.service.set_model_management(True)
    downloads.cached.add("nemotron-3.5")
    before = settings_manager.load_all_settings()
    runtimes.release.clear()
    dialog = RemoteModelsDialog(managed.service, "linux-host")
    dialog.show()
    try:
        assert _pump_until(lambda: dialog._valid and not dialog._busy)
        dialog.select_model("nemotron:nemotron-3.5")
        dialog.device_combo.setCurrentIndex(dialog.device_combo.findData("cuda"))
        assert (
            dialog.install_button.isEnabled() and not dialog.select_button.isEnabled()
        )
        assert "NVIDIA Speech GPU" in dialog.runtime_detail.text()
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No
        )
        dialog.install_button.click()
        assert not runtimes.calls
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes
        )
        dialog.install_button.click()
        assert _pump_until(lambda: dialog._poll.isActive())
        assert dialog.install_progress.value() == 25
        assert not dialog.install_button.isEnabled()
        runtimes.release.set()
        assert _wait_for(
            lambda: managed.service._runtime_installer.job().get("state") == "complete"
        )
        dialog._request("model_catalog")
        assert _pump_until(lambda: not dialog._busy)
        assert dialog.device_combo.currentData() == "cuda"
        assert dialog.select_button.isEnabled()
        assert not dialog._poll.isActive()
        # TLS authentication legitimately updates the host's last-seen device
        # timestamp; installing must leave every engine/client setting alone.
        after = settings_manager.load_all_settings()
        assert {k: v for k, v in after.items() if k != "remote_host_devices"} == {
            k: v for k, v in before.items() if k != "remote_host_devices"
        }
        dialog.search.setText("Nemotron")
        assert (
            sum(
                not dialog.model_list.item(i).isHidden()
                for i in range(dialog.model_list.count())
            )
            == 1
        )
    finally:
        runtimes.release.set()
        dialog.close()


def test_browser_legacy_host_keeps_use_action_and_explains_setup():
    from ui_qt.dialogs.remote_models import RemoteModelsDialog

    dialog = RemoteModelsDialog(None, "old-host")
    dialog._on_finished(
        "model_catalog",
        dict(
            models=[
                dict(
                    family="local_whisper",
                    model="tiny",
                    label="Whisper tiny",
                    cached=True,
                    runtime_ready=True,
                )
            ],
            can_select=True,
        ),
        "",
    )
    assert dialog.select_button.isEnabled()
    assert "Update OpenWhisper" in dialog.runtime_detail.text()
    assert not dialog.install_button.isEnabled()
    dialog.close()


def test_management_window_cannot_follow_a_changed_pairing(managed, runtimes):
    managed.service.set_model_management(True)
    with pytest.raises(RemoteEngineError, match="paired host changed"):
        managed.service.remote_model_request(
            "install_runtime",
            expected_pairing=object(),
            family="nemotron",
            model="nemotron-3.5",
            device="cuda",
        )
    assert not runtimes.calls


def test_host_setup_navigation_opens_remote_settings_when_unpaired():
    from unittest.mock import Mock

    from ui_qt.ui_controller import UIController

    controller = SimpleNamespace(
        remote_engine=SimpleNamespace(client_pairing=lambda: None),
        open_settings_destination=Mock(),
    )
    UIController.open_engine_help_destination(controller, "remote_models")
    controller.open_settings_destination.assert_called_once_with("remote_engine")
    controller.open_settings_destination.reset_mock()
    UIController.open_engine_help_destination(controller, "remote_engine")
    controller.open_settings_destination.assert_called_once_with("remote_engine")
