"""Host-owned runtime choices and validation for paired computers."""
from __future__ import annotations

from services.local_asr.catalog import (
    BACKENDS,
    WHISPER_BACKEND,
    runtime_id,
    selected_device,
)
from services.settings import SettingsKey, settings_manager


def runtime_state(engine: dict) -> dict:
    """Describe saved choices separately from the device actually running."""
    from services.components import gpu_runtime_available, is_installed
    from services.gpu_info import nvidia_gpu

    family = engine.get("family")
    if family not in (*BACKENDS, WHISPER_BACKEND):
        return {}
    settings = settings_manager.load_all_settings()
    gpu = nvidia_gpu()
    cuda = False
    compute_types = {}
    try:
        import ctranslate2

        cuda = bool(ctranslate2.get_cuda_device_count())
    except Exception:
        cuda = gpu is not None or engine.get("device") == "cuda"
    if family == WHISPER_BACKEND:
        cuda = cuda and gpu_runtime_available()
        for device in ("cpu", "cuda") if cuda else ("cpu",):
            try:
                compute_types[device] = ["auto", *sorted(ctranslate2.get_supported_compute_types(device))]
            except Exception:
                # Auto still lets the backend choose after a failed probe.
                compute_types[device] = ["auto"]
        devices = ["auto", "cpu"] + (["cuda"] if cuda else [])
        selected = {
            "device": settings.get(SettingsKey.WHISPER_DEVICE, "auto"),
            "compute_type": settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"),
        }
        compute_types["auto"] = compute_types.get("cuda" if cuda else "cpu", ["auto"])
    else:
        devices = [device for device in ("cpu", "cuda")
                   if (device != "cuda" or (family != "moonshine" and cuda))
                   and is_installed(runtime_id(family, device))]
        if family != "moonshine" and devices:
            devices.insert(0, "auto")
        selected = {"device": selected_device(family, settings),
                    "language": "en" if family == "moonshine" else settings.get(SettingsKey.LOCAL_ASR_LANGUAGE, "en")}
    return {
        "family": family,
        "model": engine.get("model", ""),
        "selected": selected,
        "devices": devices,
        "compute_types": compute_types,
        "languages": (["en"] if family == "moonshine" else ["en", "auto"]) if family in BACKENDS else [],
        "gpu": {"name": gpu.name, "total_mib": gpu.total_mib} if gpu else {},
    }


def validate_runtime(engine: dict, family: str, model: str, changes: dict) -> dict:
    """Reject stale, unsupported or arbitrary settings before persisting any."""
    if (engine.get("family"), engine.get("model")) != (family, model):
        raise ValueError("The host changed engines. Reconnect before changing its runtime.")
    if not isinstance(changes, dict) or not changes or set(changes) - {"device", "compute_type", "language"}:
        raise ValueError("Choose a device or precision supported by the host.")
    state = runtime_state(engine)
    if not state:
        raise ValueError("Select a local speech engine on the host first.")
    device = changes.get("device", state["selected"]["device"])
    if not isinstance(device, str) or device not in state["devices"]:
        raise ValueError("That device isn't ready on the host. Check its hardware and installed runtimes.")
    selected = {**state["selected"], **changes}
    if family == WHISPER_BACKEND:
        if "language" in changes:
            raise ValueError("This engine doesn't expose a host language setting.")
        # Moving between CPU and GPU should choose an appropriate precision.
        if "device" in changes and "compute_type" not in changes:
            selected["compute_type"] = "auto"
        compute = selected["compute_type"]
        if not isinstance(compute, str) or compute not in state["compute_types"].get(device, []):
            raise ValueError("That precision isn't supported by the selected device on the host.")
        return {SettingsKey.WHISPER_DEVICE: device,
                SettingsKey.WHISPER_COMPUTE_TYPE: compute}
    if "compute_type" in changes:
        raise ValueError("This engine manages its own precision on the host.")
    if selected["language"] not in state["languages"]:
        raise ValueError("Choose a language supported by this engine on the host.")
    devices = dict(settings_manager.get(SettingsKey.LOCAL_ASR_DEVICES, {}) or {})
    devices[family] = device
    return {SettingsKey.LOCAL_ASR_DEVICES: devices,
            SettingsKey.LOCAL_ASR_LANGUAGE: selected["language"]}
