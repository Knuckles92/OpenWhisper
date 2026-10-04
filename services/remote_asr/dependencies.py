"""Host-selected speech dependencies; clients never supply installer sources."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def dependency_options(family: str) -> list[dict]:
    from services.component_catalog import get_component_details
    from services.components import (
        ComponentId,
        available_component_ids,
        catalog_entry_for_platform,
        gpu_runtime_available,
        is_installed,
    )
    from services.gpu_info import nvidia_gpu
    from services.local_asr.catalog import (
        BACKENDS,
        CUDA_MIN_COMPUTE_CAPABILITY,
        WHISPER_BACKEND,
        apple_silicon,
        runtime_id,
    )

    if family not in (*BACKENDS, WHISPER_BACKEND):
        return []
    gpu = nvidia_gpu()
    available = available_component_ids()
    options = []
    devices = (
        ("cpu",) if family == "moonshine"
        # Auto is the Apple GPU's runtime; a Mac has no CUDA.
        else ("auto", "cpu") if family == "parakeet_mlx" or (family != WHISPER_BACKEND and apple_silicon())
        else ("cpu", "cuda")
    )
    for device in devices:
        component = (
            (ComponentId.GPU_ACCEL if device == "cuda" else "")
            if family == WHISPER_BACKEND
            else runtime_id(family, device)
        )
        installed = not component or is_installed(component)
        ready = (
            gpu_runtime_available() if component == ComponentId.GPU_ACCEL else installed
        )
        reason = ""
        if component and component not in available:
            reason = "This runtime is not available for the host's platform."
        elif device == "cuda" and gpu is None:
            reason = "No NVIDIA GPU detected on the host. Check its NVIDIA driver, or use CPU."
        elif (
            # Older cards get the Vulkan runtime where the host has one.
            component == ComponentId.ASR_NVIDIA_CUDA
            and gpu.compute_capability is not None
            and gpu.compute_capability < CUDA_MIN_COMPUTE_CAPABILITY
        ):
            reason = "This speech GPU runtime requires an NVIDIA Turing or newer GPU. Use CPU on this host."
        elif installed and not ready:
            reason = "Runtime installed but unavailable. Restart OpenWhisper on the host and check its NVIDIA driver."
        entry = catalog_entry_for_platform(component) if component else None
        label = (
            get_component_details(component).display_name
            if component
            else "Built-in CPU runtime"
        )
        options.append(
            dict(
                device=device,
                component=component,
                label=label,
                ready=bool(ready and not reason),
                installed=installed,
                installable=bool(
                    component and not installed and not ready and not reason
                ),
                reason=reason,
                download_bytes=sum(
                    int(a.get("size_bytes", 0))
                    for a in (entry or {}).get("archives", [])
                ),
            )
        )
    return options


def validate_model_device(family: str, model: str, device: str) -> dict:
    from services.hf_access import is_model_cached
    from services.remote_asr.model_management import model_choices

    choice = next(
        (
            item
            for item in model_choices()
            if (item["family"], item["model"]) == (family, model)
        ),
        None,
    )
    if choice is None:
        raise ValueError("Choose a speech model from the host's catalog.")
    if not is_model_cached(model):
        raise RuntimeError("Download the model on the host first.")
    option = next(
        (item for item in dependency_options(family) if item["device"] == device), None
    )
    if option is None or not option["ready"]:
        raise RuntimeError(
            (option or {}).get("reason")
            or "Install the selected device's runtime on the host first."
        )
    return choice


class HostRuntimeInstaller:
    """One background installation, coordinated with the host's Downloads page."""

    def __init__(self, on_changed=lambda: None):
        self._lock = threading.Lock()
        self._job = {}
        self._on_changed = on_changed

    def job(self):
        with self._lock:
            return dict(self._job)

    def install(self, family: str, model: str, device: str, device_name: str) -> dict:
        from services.components import component_coordinator
        from services.remote_asr.model_management import model_choices

        if not any(
            (item["family"], item["model"]) == (family, model)
            for item in model_choices()
        ):
            raise ValueError("Choose a speech model from the host's catalog.")
        option = next(
            (item for item in dependency_options(family) if item["device"] == device),
            None,
        )
        if option is None:
            raise ValueError(
                "Choose CPU or NVIDIA GPU from the host's runtime choices."
            )
        with self._lock:
            if self._job.get("state") == "installing":
                if self._job["component"] == option["component"]:
                    return {"installation": dict(self._job)}
                raise RuntimeError(
                    "Another remote runtime installation is in progress. Wait for it to finish."
                )
            if option["ready"]:
                return {"installation": {**option, "state": "complete"}}
            if not option["installable"]:
                raise RuntimeError(
                    option["reason"]
                    or "Install this runtime in Settings → Downloads on the host."
                )
            component = option["component"]
            cancel = component_coordinator.begin_install(component)
            if cancel is None:
                raise RuntimeError(
                    "This runtime is already being installed on the host. Refresh when it finishes."
                )
            self._job = dict(
                component=component,
                label=option["label"],
                family=family,
                model=model,
                device=device,
                state="installing",
                phase="Starting",
                done=0,
                total=0,
                error="",
            )
            try:
                threading.Thread(
                    target=self._install,
                    args=(component, cancel, device_name),
                    name="remote-runtime-install",
                    daemon=True,
                ).start()
            except Exception:
                component_coordinator.end_install(component)
                self._job.update(
                    state="failed",
                    error="Couldn't start runtime installation on the host.",
                )
                raise RuntimeError(self._job["error"]) from None
            return {"installation": dict(self._job)}

    def _install(self, component, cancel, device_name):
        from services.component_runtime import activate_component
        from services.components import (
            ComponentCanceled,
            component_coordinator,
            install_component,
            is_installed,
        )

        def progress(phase, done, total):
            with self._lock:
                self._job.update(
                    phase=str(phase), done=max(0, int(done)), total=max(0, int(total))
                )

        error, state = "", "complete"
        try:
            logger.info("Installing %s for paired computer %s", component, device_name)
            # A local install may have completed between inventory and our claim.
            if not is_installed(component):
                entry = component_coordinator.catalog_entry(component)
                if entry is None:
                    raise RuntimeError("Runtime unavailable")
                install_component(component, entry, progress, cancel)
            if not is_installed(component):
                raise RuntimeError("Runtime installation is incomplete")
            activated, _reason = activate_component(component)
            if not activated:
                state = "restart_required"
                error = (
                    "Installed. Restart OpenWhisper on the host to enable this runtime."
                )
        except ComponentCanceled:
            state, error = (
                "failed",
                "Runtime installation was canceled on the host. You can retry.",
            )
        except Exception:
            logger.exception("Remote runtime installation failed for %s", component)
            state, error = (
                "failed",
                "Runtime installation failed on the host. Check its connection, free disk space and logs, then retry.",
            )
        finally:
            component_coordinator.end_install(component)
            with self._lock:
                self._job.update(state=state, error=error)
            self._on_changed()
