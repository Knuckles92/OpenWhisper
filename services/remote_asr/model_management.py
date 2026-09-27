"""Host-owned model inventory and bounded, fetch-only remote downloads.

Only bundled speech models are accepted, never client-supplied repositories,
paths or runtime installers. Downloads share the local Downloads
page's policy and claim coordinator. An accepted download survives a client
closing its window; permission changes prevent new work, not work in progress.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


def model_choices() -> list[dict]:
    from config import config
    from services.local_asr.catalog import MODELS, WHISPER_BACKEND

    return [
        {"family": model.backend, "model": key, "label": model.label}
        for key, model in MODELS.items()
    ] + [
        {"family": WHISPER_BACKEND, "model": name, "label": f"Whisper {name}"}
        for name in config.WHISPER_MODEL_CHOICES if name != "auto"
    ]


class HostModelManager:
    """At most one remotely initiated download, with a bounded status record."""

    def __init__(self, on_changed: Callable[[], None] = lambda: None):
        self._lock = threading.Lock()
        self._job: dict = {}
        self._on_changed = on_changed

    def job(self) -> dict:
        with self._lock:
            return dict(self._job)

    def catalog(self) -> dict:
        from services.components import is_installed
        from services.hf_access import format_download_size, is_model_cached
        from services.local_asr.catalog import (
            WHISPER_BACKEND,
            resolve_runtime,
            selected_device,
        )
        from services.remote_asr.dependencies import dependency_options
        from services.settings import settings_manager

        settings = settings_manager.load_all_settings()
        runtimes = {WHISPER_BACKEND: True}
        models = model_choices()
        dependencies = {}
        for entry in models:
            family, model = entry["family"], entry["model"]
            if family not in dependencies:
                dependencies[family] = dependency_options(family)
            if family not in runtimes:
                component, _device = resolve_runtime(family, selected_device(family, settings))
                runtimes[family] = is_installed(component)
            entry.update(
                cached=is_model_cached(model),
                runtime_ready=runtimes[family],
                download_size=format_download_size(model) or "",
                dependencies=dependencies[family],
                selected_device=(settings.get("whisper_device", "auto") if family == WHISPER_BACKEND
                                 else selected_device(family, settings)),
            )
        return {"models": models, "download": self.job()}

    def download(self, family: str, model: str, device_name: str) -> dict:
        from services.hf_access import AccessDecision
        from services.hf_access import hf_access_coordinator as coordinator

        choice = next((entry for entry in model_choices()
                       if (entry["family"], entry["model"]) == (family, model)), None)
        if choice is None:
            raise ValueError("Choose a speech model from the host's catalog.")
        with self._lock:
            if self._job.get("state") == "downloading":
                if (self._job["family"], self._job["model"]) == (family, model):
                    return {"download": dict(self._job)}
                raise RuntimeError("Another remote model download is in progress. Wait for it to finish.")
            if not coordinator.begin_request(model):
                raise RuntimeError("This model is already being downloaded or managed on the host.")
            try:
                decision = coordinator.evaluate_access(model)
                if decision == AccessDecision.BLOCKED_BY_ENV:
                    raise RuntimeError("Model downloads are disabled by HF_HUB_OFFLINE on the host.")
                if decision not in (AccessDecision.LOAD_CACHED, AccessDecision.DOWNLOAD_ALLOWED):
                    raise RuntimeError(
                        "The host requires download consent. Choose ‘Always allow downloads’ in "
                        "Settings → Downloads on the host, or download this model there first."
                    )
                self._job = {**choice, "state": "complete" if decision == AccessDecision.LOAD_CACHED
                             else "downloading", "done": 0, "total": 0, "error": ""}
                if decision == AccessDecision.LOAD_CACHED:
                    coordinator.end_request(model)
                else:
                    threading.Thread(
                        target=self._download, args=(model, device_name),
                        name="remote-model-download", daemon=True,
                    ).start()
            except Exception:
                coordinator.end_request(model)
                # A worker could not be started; don't leave a permanent busy job.
                if self._job.get("model") == model and self._job.get("state") == "downloading":
                    self._job["state"] = "failed"
                    self._job["error"] = "Couldn't start the download on the host."
                raise
            return {"download": dict(self._job)}

    def _download(self, model: str, device_name: str) -> None:
        from services.hf_access import (
            download_model_files,
            hf_access_coordinator,
            is_model_cached,
        )

        def progress(done: int, total: int) -> None:
            with self._lock:
                self._job.update(done=max(0, int(done)), total=max(0, int(total)))

        error = ""
        try:
            logger.info("Downloading %s for paired computer %s", model, device_name)
            download_model_files(model, progress_callback=progress)
            if not is_model_cached(model):
                raise RuntimeError("Downloaded model is incomplete")
        except Exception:
            # Network exceptions may contain host paths or credential-bearing URLs.
            logger.exception("Remote model download failed for %s", model)
            error = "Download failed on the host. Check its connection, free disk space and logs, then retry."
        finally:
            hf_access_coordinator.end_request(model)
            with self._lock:
                self._job.update(state="failed" if error else "complete", error=error)
            self._on_changed()
