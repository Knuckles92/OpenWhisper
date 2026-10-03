"""Remote runtime controls: host validation, real TLS requests, and engine cards."""
# Pytest resolves the imported fixtures by name in test parameters.
# ruff: noqa: F811
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication

from services.remote_asr.runtime import runtime_state, validate_runtime
from services.remote_asr.service import RemoteEngineService
from services.settings import SettingsKey, settings_manager
from tests.test_remote_engine import (  # noqa: F401
    FakeEngine,
    _connect,
    _host_controller,
    _pair,
    _wait_for,
    host,
    identity,
    no_real_tailscale,
    paired_backend,
    store,
)
from transcriber.remote_backend import HostModel, RemoteLink, RemoteModels


class RuntimeEngine(FakeEngine):
    device = "cpu"
    compute_type = "int8"

    @property
    def identity(self):
        return (*super().identity, self.device, self.compute_type)

    def describe(self):
        return {**super().describe(), "device": self.device, "compute_type": self.compute_type,
                "label": f"Whisper {self.model}"}


@pytest.fixture
def engine():
    return RuntimeEngine("local_whisper", "turbo")


@pytest.fixture(autouse=True)
def hardware(monkeypatch):
    from services import components, gpu_info

    installed = {"asr-nvidia-cpu", "asr-nvidia-cuda", "asr-moonshine", "asr-qwen"}
    monkeypatch.setattr(components, "is_installed", lambda key: key in installed)
    monkeypatch.setattr(components, "gpu_runtime_available", lambda: True)
    monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda: gpu_info.NvidiaGpu("NVIDIA GTX 1050 Ti", 4096, (6, 1)))
    ct = SimpleNamespace(get_cuda_device_count=lambda: 1, get_supported_compute_types=lambda device:
                         {"int8", "float32", "int8_float32"} if device == "cuda" else {"int8", "float32"})
    monkeypatch.setitem(sys.modules, "ctranslate2", ct)
    settings_manager.update_settings({SettingsKey.WHISPER_DEVICE: "cpu",
                                      SettingsKey.WHISPER_COMPUTE_TYPE: "int8"})
    return installed, ct


@pytest.fixture
def controls(host, engine):
    def configure(family, model, changes, device_name):
        updates = validate_runtime(engine.describe(), family, model, changes)
        settings_manager.update_settings(updates)
        engine.device = changes.get("device", engine.device)
        if engine.device == "auto":
            engine.device = "cuda"
        engine.compute_type = "int8_float32" if engine.device == "cuda" else "int8"

    service = RemoteEngineService(lambda: None, configure_engine=configure)
    service._engine = lambda: engine
    host._runtime = service.runtime_state
    host._configure_runtime = service.configure_runtime
    return service


def test_host_reports_real_capabilities_and_saved_choices(engine, hardware):
    state = runtime_state(engine.describe())
    assert state["selected"] == {"device": "cpu", "compute_type": "int8"}
    assert state["devices"] == ["auto", "cpu", "cuda"]
    assert "float16" not in state["compute_types"]["cuda"]
    assert "int8_float32" in state["compute_types"]["cuda"]
    assert state["gpu"]["total_mib"] == 4096
    hardware[1].get_cuda_device_count = lambda: 0
    assert runtime_state(engine.describe())["devices"] == ["auto", "cpu"]


def test_runtime_changes_are_bounded_and_device_switch_resets_precision(engine):
    assert validate_runtime(engine.describe(), engine.family, engine.model, {"device": "cuda"}) == {
        SettingsKey.WHISPER_DEVICE: "cuda", SettingsKey.WHISPER_COMPUTE_TYPE: "auto"}
    before = settings_manager.load_all_settings()
    for changes in ({"device": "mps"}, {"compute_type": "float16"}, {"device": []},
                    {"language": "auto"}, {"command": "run"}, {}, {"compute_type": {}}):
        with pytest.raises(ValueError):
            validate_runtime(engine.describe(), engine.family, engine.model, changes)
    with pytest.raises(ValueError, match="changed engines"):
        validate_runtime(engine.describe(), "parakeet", "parakeet-v3", {"device": "cpu"})
    assert settings_manager.load_all_settings() == before


def test_optional_devices_require_installed_runtime_and_moonshine_is_cpu_only(hardware):
    hardware[0].remove("asr-nvidia-cuda")
    state = runtime_state({"family": "parakeet", "model": "parakeet-v3"})
    assert state["devices"] == ["auto", "cpu"]
    state = runtime_state({"family": "moonshine", "model": "moonshine-small"})
    assert state["devices"] == ["cpu"] and state["languages"] == ["en"]
    assert state["compute_types"] == {}
    with pytest.raises(ValueError):
        validate_runtime({"family": "moonshine", "model": "moonshine-small"},
                         "moonshine", "moonshine-small", {"device": "cuda"})


def test_device_change_reloads_same_model_and_adopts_host_precision(paired_backend, controls, engine):
    backend = paired_backend
    backend.reload_model()
    assert backend.runtime["can_configure"]
    backend.request_runtime(engine.family, engine.model, {"device": "cuda"})
    backend.reload_model()
    assert backend.is_available() and not backend.switch_error
    assert backend.device == engine.device == "cuda"
    assert "cuda (int8_float32)" in backend.device_info
    assert backend.link().gpu_name == "NVIDIA GTX 1050 Ti"
    assert backend.model_choices().runtime["selected"] == {"device": "cuda", "compute_type": "auto"}
    backend.reload_model()
    assert backend._requested_runtime is None
    backend.request_runtime(engine.family, engine.model, {"compute_type": "int8_float32"})
    backend.reload_model()
    assert backend.runtime["selected"]["compute_type"] == "int8_float32"
    from ui_qt.widgets.local_engine_controls import LocalEngineControls

    # The host's own fields must reflect the precision picked over the wire.
    assert LocalEngineControls().compute_combo.currentText() == "int8_float32"


def test_host_refusal_restores_authoritative_controls(paired_backend, controls, engine):
    backend = paired_backend
    backend.reload_model()
    controls._configure_engine = Mock(side_effect=RuntimeError("The host is running a meeting."))
    backend.request_runtime(engine.family, engine.model, {"device": "cuda"})
    backend.reload_model()
    assert backend.is_available() and backend.device == "cpu"
    assert backend.switch_error == "The host is running a meeting."
    assert backend.runtime["selected"]["device"] == "cpu"


def test_runtime_reply_waits_for_reload_and_reports_cpu_fallback(controls, engine):
    started, settled = threading.Event(), threading.Event()
    settled.set()
    controls._engine_settled = settled.is_set

    def configure(*_args):
        settled.clear()
        started.set()

    controls._configure_engine = configure
    results = []
    worker = threading.Thread(target=lambda: results.append(controls.configure_runtime(
        engine.family, engine.model, {"device": "cuda"}, "laptop")))
    worker.start()
    try:
        assert started.wait(2)
        assert not results
        with pytest.raises(RuntimeError, match="changing its runtime"):
            controls.configure_runtime(engine.family, engine.model, {"device": "cpu"}, "other")
    finally:
        settled.set()
        worker.join(3)
    assert results[0]["device"] == "cpu", "actual fallback must win over the requested GPU"


def test_optional_language_is_applied_to_remote_audio(paired_backend, controls, engine):
    import numpy as np

    engine.family, engine.model = "parakeet", "parakeet-v3"
    paired_backend.reload_model()
    paired_backend.request_runtime(engine.family, engine.model, {"language": "auto"})
    paired_backend.reload_model()
    paired_backend._request_audio("transcribe", np.ones(160, dtype=np.float32))
    assert engine.calls[-1][-1] == "auto"


def test_capability_disappearing_before_reload_does_not_send_runtime_changes(paired_backend, controls, engine, host):
    paired_backend.reload_model()
    paired_backend.request_runtime(engine.family, engine.model, {"device": "cuda"})
    host._configure_runtime = None
    paired_backend.reload_model()
    assert paired_backend.is_available()
    assert "Update OpenWhisper" in paired_backend.switch_error
    assert engine.device == "cpu"


def test_protocol_rejects_extra_fields_and_removed_clients(host, controls, engine):
    result = _pair(host)
    connection = _connect(host, result)
    try:
        with pytest.raises(RuntimeError, match="Invalid runtime request"):
            connection.request("configure_runtime", family=engine.family, model=engine.model,
                               settings={"device": "cuda"}, arbitrary="value")
        with pytest.raises(RuntimeError, match="isn't ready"):
            connection.request("configure_runtime", family=engine.family, model=engine.model,
                               settings={"device": "invalid"})
        assert engine.device == "cpu"
        host.remove_device(result.device_id)
        assert _wait_for(lambda: not connection.alive)
        with pytest.raises(RuntimeError):
            connection.request("configure_runtime", family=engine.family, model=engine.model,
                               settings={"device": "cuda"})
        assert engine.device == "cpu"
    finally:
        connection.close()


def test_idle_reconnect_updates_device_even_when_model_is_unchanged(paired_backend, controls, engine, host):
    backend = paired_backend
    backend.reload_model()
    engine.device, engine.compute_type = "cuda", "int8_float32"
    settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "cuda")
    host.engine_changed()
    assert _wait_for(lambda: not backend._process.alive)
    backend.check_link()
    assert backend.restore_link() == "connected"
    assert backend.device == backend.link().device == "cuda"
    assert backend.runtime["selected"]["device"] == "cuda"


def test_catalog_refresh_updates_choices_without_reconnecting_audio(paired_backend, controls, engine):
    backend = paired_backend
    backend.reload_model()
    process = backend._process
    state = runtime_state(engine.describe())
    state["dependencies"] = [{"device": "cuda", "label": "GPU runtime", "ready": True}]
    catalog = {"engine": engine.describe(), "runtime": state, "can_select": True,
               "models": [{"family": engine.family, "model": engine.model, "label": "Whisper turbo",
                           "cached": True, "runtime_ready": True}]}
    ready = {"capabilities": {"engine_controls": True}}
    assert backend.refresh_host_catalog(backend._pairing, ready, catalog)
    assert backend._process is process and process.alive
    assert backend.runtime["dependencies"][0]["ready"]
    assert not backend.refresh_host_catalog(object(), ready, catalog)
    assert not backend.refresh_host_catalog(backend._pairing, ready,
                                            {**catalog, "engine": {**engine.describe(), "model": "base"}})
    assert backend._process is process and backend.model_name == engine.model


def test_old_host_remains_readable_but_cannot_be_configured(paired_backend, engine):
    paired_backend.reload_model()
    assert paired_backend.runtime is None
    assert "cpu (int8)" in paired_backend.device_info
    with pytest.raises(ValueError, match="Update OpenWhisper"):
        paired_backend.request_runtime(engine.family, engine.model, {"device": "cuda"})


def test_host_controller_persists_runtime_and_reloads_even_for_same_model(monkeypatch, engine):
    from services.remote_asr import engines

    controller = _host_controller("local_whisper")
    controller._reload_in_flight = False
    controller.current_backend = object()
    monkeypatch.setattr(engines, "host_engine_for", lambda backend: engine)
    controller.transcription_backends["local_whisper"] = SimpleNamespace(
        is_available=lambda: True, last_loaded_model="turbo")
    assert controller._switch_engine_to("local_whisper", "turbo", "laptop", runtime={"device": "cuda"}) is None
    controller.reload_whisper_model.assert_called_once()
    assert settings_manager.get(SettingsKey.WHISPER_DEVICE) == "cuda"
    assert settings_manager.get(SettingsKey.WHISPER_COMPUTE_TYPE) == "auto"
    controller.recorder.is_recording = True
    assert "transcribing" in controller._switch_engine_to("local_whisper", "turbo", "laptop", runtime={"device": "cpu"})
    assert settings_manager.get(SettingsKey.WHISPER_DEVICE) == "cuda"


def _choices(engine, **runtime):
    current = HostModel(engine.family, engine.model, engine.describe()["label"])
    return RemoteModels("devbox", (current,), current,
                        {**runtime_state(engine.describe()), "can_configure": True, **runtime}, engine.describe())


def test_card_uses_host_choices_and_locks_during_activity(engine):
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    tab = UploadFileTab()
    tab.set_backend("Remote computer")
    tab.set_remote_models(_choices(engine))
    tab.set_remote_link(RemoteLink("connected", host="devbox", device="cuda", compute_type="int8_float32",
                                   gpu_name="GTX 1050 Ti", gpu_memory_mib=4096))
    tab.show()
    QApplication.processEvents()
    controls = tab.remote_engine
    assert controls.isVisible() and controls.device_combo.isEnabled()
    assert controls.compute_combo.isVisible() and not controls.language_combo.isVisible()
    assert "4 GB VRAM" in tab.remote_runtime_label.text()
    assert "NVIDIA GPU" in tab.remote_runtime_label.text()
    emitted = []
    tab.remote_runtime_selected.connect(lambda *args: emitted.append(args))
    controls.device_combo.activated.emit(controls.device_combo.findData("cuda"))
    assert emitted == [("local_whisper", "turbo", {"device": "cuda"})]
    assert not controls.device_combo.isEnabled()
    assert settings_manager.get(SettingsKey.WHISPER_DEVICE) == "cpu", "UI must not save host settings locally"
    tab.set_remote_models(_choices(engine))
    assert len(emitted) == 1
    for lock, unlock in ((lambda: tab.set_backend_enabled(False), lambda: tab.set_backend_enabled(True)),
                         (lambda: tab.set_engine_busy(True), lambda: tab.set_engine_busy(False))):
        lock()
        assert not controls.device_combo.isEnabled()
        unlock()
        assert controls.device_combo.isEnabled()
    tab.set_remote_link(RemoteLink("offline", host="devbox"))
    assert not controls.device_combo.isEnabled() and tab.remote_runtime_label.isHidden()
    tab.set_backend("Local Whisper")
    assert not controls.isVisible()


def test_legacy_runtime_fields_are_read_only(engine):
    from ui_qt.widgets.remote_engine_controls import RemoteEngineControls

    widget = RemoteEngineControls()
    widget.set_state(RemoteModels("old-host", engine=engine.describe()))
    assert widget.device_combo.currentData() == "cpu"
    assert widget.compute_combo.currentData() == "int8"
    assert not widget.device_combo.isEnabled()
    assert "Update OpenWhisper" in widget.device_combo.toolTip()


def test_missing_gpu_runtime_has_setup_route_in_main_card(engine, monkeypatch):
    from services import components
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    # The simulated NVIDIA host offers Windows runtimes even on a macOS client.
    monkeypatch.setattr(components, "current_platform_tag", lambda: "win_amd64")
    monkeypatch.setattr(components, "gpu_runtime_available", lambda: False)
    tab = UploadFileTab()
    tab.set_backend("Remote computer")
    tab.set_remote_models(_choices(engine))
    tab.set_remote_link(RemoteLink("connected", host="devbox", device="cpu"))
    tab.show()
    destinations = []
    changes = []
    tab.help_requested.connect(destinations.append)
    tab.remote_runtime_selected.connect(lambda *args: changes.append(args))
    assert tab.remote_manage_button.isVisible()
    assert tab.remote_manage_button.available == ["GPU Acceleration"]
    assert "GPU Acceleration" in tab.remote_manage_button.toolTip()
    tab.remote_manage_button.click()
    assert destinations == ["remote_models"]
    combo = tab.remote_engine.device_combo
    index = combo.findData("setup:cuda")
    assert index >= 0 and combo.isEnabled()
    combo.activated.emit(index)
    assert destinations == ["remote_models", "remote_models"]
    assert not changes
    tab.close()


def test_optional_card_shows_language_and_only_host_devices(engine):
    from ui_qt.widgets.remote_engine_controls import RemoteEngineControls

    engine.family, engine.model = "moonshine", "moonshine-small"
    widget = RemoteEngineControls()
    widget.set_state(_choices(engine))
    widget.show()
    assert not widget.compute_combo.isVisible()
    assert widget.language_combo.isVisible()
    assert widget.language_combo.currentText() == "English"
    assert not widget.language_combo.isEnabled()
    assert not widget.device_combo.isEnabled()


def test_saved_auto_change_invalidates_idle_clients_even_if_resolved_runtime_is_same():
    from services.remote_asr.engines import SpeechWorkerEngine, WhisperEngine

    backend = SimpleNamespace(last_loaded_model="turbo", device="cpu", compute_type="int8",
                              backend_id="parakeet", model_name="parakeet-v3")
    whisper = WhisperEngine(backend)
    before = whisper.identity
    settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "auto")
    assert whisper.identity != before
    optional = SpeechWorkerEngine(backend)
    before = optional.identity
    settings_manager.save_setting(SettingsKey.LOCAL_ASR_LANGUAGE, "auto")
    assert optional.identity != before
