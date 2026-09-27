""""Use this GPU": one action from a CUDA device without CUDA libraries to the GPU.

It used to take three: install GPU Acceleration, set the device back from
the "cpu" a fallback saved, and consent to the model download. A fresh
install stopped at "waiting for download consent" without saying the GPU
was blocked too.
"""
from types import SimpleNamespace

import pytest

from services import application_controller as module
from services import gpu_info, gpu_setup
from services.application_controller import ApplicationController
from services.components import ComponentId
from services.settings import SettingsKey
from transcriber.local_backend import GpuFallbackCause

PASCAL = {"float32", "int8", "int8_float32"}
GTX_1050_TI = gpu_info.NvidiaGpu("NVIDIA GeForce GTX 1050 Ti", 4096, (6, 1))


class Settings:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save_setting(self, key, value):
        self.values[key] = value

    def load_all_settings(self):
        return dict(self.values)


@pytest.fixture
def settings(monkeypatch):
    store = Settings(**{SettingsKey.WHISPER_DEVICE: "cpu", SettingsKey.WHISPER_MODEL: "auto"})
    monkeypatch.setattr(module, "settings_manager", store)
    monkeypatch.setattr("services.settings.settings_manager", store)
    return store


@pytest.fixture
def machine(monkeypatch):
    """A Linux laptop with a GTX 1050 Ti and no CUDA libraries or turbo yet."""
    monkeypatch.setattr(module, "available_component_ids", lambda: (ComponentId.GPU_ACCEL,))
    monkeypatch.setattr("services.components.available_component_ids", lambda: (ComponentId.GPU_ACCEL,))
    monkeypatch.setattr("services.components.gpu_runtime_available", lambda: False)
    monkeypatch.setattr("services.hf_access.is_model_cached", lambda name: False)
    monkeypatch.setattr(gpu_setup, "nvidia_gpu", lambda: GTX_1050_TI)
    monkeypatch.setattr(gpu_info.sys, "platform", "linux")
    monkeypatch.setattr(module, "is_hf_hub_offline_env_set", lambda: False)


def _backend(cause=GpuFallbackCause.MISSING_LIBRARIES):
    return SimpleNamespace(
        gpu_fallback_cause=cause,
        gpu_fallback_note="GPU unavailable, using CPU",
        _get_supported_compute_types=lambda device: PASCAL,
    )


def _controller(backend, answer):
    calls = []
    grants = []
    controller = SimpleNamespace(
        _gpu_offer_state=None,
        transcription_backends={"local_whisper": backend},
        is_meeting_active=lambda: False,
        recorder=SimpleNamespace(is_recording=False),
        is_transcribing=lambda: False,
        ui_controller=SimpleNamespace(
            show_use_gpu_dialog=lambda plan: (calls.append(("dialog", plan)), answer)[1],
            refresh_local_engine_controls=lambda: calls.append(("refresh",)),
        ),
        ensure_local_model_available=lambda: calls.append(("ensure",)),
        request_component_install=lambda component: calls.append(("install", component)),
        reload_whisper_model=lambda: calls.append(("reload",)),
        status_update=SimpleNamespace(emit=lambda text: calls.append(("status", text))),
        calls=calls,
        grants=grants,
    )
    for name in ("_gpu_offer_waiting", "_offer_gpu_setup", "_start_gpu_setup"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    return controller


@pytest.fixture
def grants(monkeypatch):
    granted = []
    monkeypatch.setattr(module.hf_access_coordinator, "grant_once", granted.append)
    return granted


def test_plan_for_the_laptop_in_the_report(settings, machine):
    plan = gpu_setup.plan_gpu_setup(settings.load_all_settings(), PASCAL)

    assert (plan.model, plan.compute_type, plan.model_setting) == ("turbo", "int8_float32", "auto")
    assert plan.install_component and plan.component_download_bytes == 674_301_418
    assert plan.model_download_bytes == 1_620_000_000
    assert not plan.supports_float16 and plan.estimate_mib == pytest.approx(1377, rel=0.05)


def test_plan_keeps_a_chosen_model_that_fits(settings, machine):
    settings.values[SettingsKey.WHISPER_MODEL] = "small"

    plan = gpu_setup.plan_gpu_setup(settings.load_all_settings(), PASCAL)

    assert plan.model == "small" and not plan.switches_model


def test_plan_replaces_a_model_the_card_cannot_hold(settings, machine):
    settings.values[SettingsKey.WHISPER_MODEL] = "large-v3"

    plan = gpu_setup.plan_gpu_setup(settings.load_all_settings(), PASCAL)

    assert plan.switches_model and plan.model_setting == "auto" and plan.model == "turbo"


def test_no_plan_where_gpu_acceleration_is_not_offered(settings, machine, monkeypatch):
    monkeypatch.setattr("services.components.available_component_ids", lambda: ())

    assert gpu_setup.plan_gpu_setup(settings.load_all_settings(), PASCAL) is None


def test_the_dialog_lists_all_three_steps(settings, machine):
    from ui_qt.dialogs.use_gpu_dialog import UseGpuDialog, plan_steps

    plan = gpu_setup.plan_gpu_setup(settings.load_all_settings(), PASCAL)
    steps = plan_steps(plan)

    assert "GPU Acceleration" in steps[0] and "674 MB" in steps[0]
    assert "Whisper turbo" in steps[1] and "1.62 GB" in steps[1]
    assert "int8_float32" in steps[-1] and "1.4 GB" in steps[-1] and "no float16" in steps[-1]
    dialog = UseGpuDialog(plan)
    assert "GTX 1050 Ti" in dialog.body.text()


def test_accepting_does_all_three(settings, machine, grants):
    controller = _controller(_backend(), answer=True)

    controller._offer_gpu_setup(controller.transcription_backends["local_whisper"])

    assert settings.values[SettingsKey.WHISPER_DEVICE] == "auto"
    assert grants == ["turbo"]
    assert ("install", ComponentId.GPU_ACCEL) in controller.calls
    assert ("ensure",) not in controller.calls
    assert controller._gpu_offer_state == "answered"


def test_accepting_with_cuda_present_just_reloads(settings, machine, grants, monkeypatch):
    monkeypatch.setattr("services.components.gpu_runtime_available", lambda: True)
    controller = _controller(_backend(), answer=True)

    controller._offer_gpu_setup(controller.transcription_backends["local_whisper"])

    assert ("reload",) in controller.calls
    assert not any(call[0] == "install" for call in controller.calls)


def test_declining_is_remembered_and_resumes_the_model_download(settings, machine, grants):
    controller = _controller(_backend(), answer=False)
    backend = controller.transcription_backends["local_whisper"]

    controller._offer_gpu_setup(backend)

    assert settings.values[SettingsKey.WHISPER_GPU_OFFER_DECLINED] is True
    assert settings.values[SettingsKey.WHISPER_DEVICE] == "cpu"
    assert controller.calls[-1] == ("ensure",)
    assert grants == []
    # Answered: neither this session nor the next one asks again.
    controller._gpu_offer_state = None
    assert not controller._gpu_offer_waiting(backend)


def test_offered_once(settings, machine, grants):
    controller = _controller(_backend(), answer=False)
    backend = controller.transcription_backends["local_whisper"]

    controller._offer_gpu_setup(backend)
    controller._offer_gpu_setup(backend)

    assert sum(1 for call in controller.calls if call[0] == "dialog") == 1


def test_out_of_memory_is_not_offered(settings, machine, grants):
    controller = _controller(_backend(GpuFallbackCause.OUT_OF_MEMORY), answer=True)

    controller._offer_gpu_setup(controller.transcription_backends["local_whisper"])

    assert controller.calls == []


def test_not_offered_during_a_recording(settings, machine, grants):
    controller = _controller(_backend(), answer=True)
    controller.recorder.is_recording = True

    controller._offer_gpu_setup(controller.transcription_backends["local_whisper"])

    assert controller.calls == [] and controller._gpu_offer_state is None


def test_the_model_consent_waits_for_the_offer(settings, machine):
    backend = _backend()
    controller = _controller(backend, answer=True)

    assert controller._gpu_offer_waiting(backend)
    controller._gpu_offer_state = "answered"
    assert not controller._gpu_offer_waiting(backend)
