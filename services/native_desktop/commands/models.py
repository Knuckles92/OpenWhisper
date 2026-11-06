"""Offline model catalog and real application download/selection commands."""
from __future__ import annotations

from .common import changed, identifier, settings


def list_models(context, _params):
    from services.model_catalog import MODEL_CATALOG, custom_model_details
    from services.local_asr.catalog import backend_of, selected_model
    from services.hf_access import is_model_cached
    saved = settings(context).load_all_settings()
    family = saved.get("selected_model", "local_whisper")
    from services.local_asr.catalog import BACKENDS
    selected = saved.get("whisper_model", "base") if family == "local_whisper" else selected_model(family, saved) if family in BACKENDS else ""
    state = getattr(context.ui, "state", {})
    progress = state.get("model_downloads", {})
    items = []
    from services.whisper_sources import custom_models
    catalog = {**MODEL_CATALOG, **{name: custom_model_details(name) for name in custom_models(saved)}}
    for model_id, details in catalog.items():
        backend = backend_of(model_id)
        installed = is_model_cached(model_id)
        download = progress.get(model_id, {})
        items.append({"id": model_id, "name": details.model_name,
                      "family": backend, "description": details.description,
                      "installed": installed, "selected": model_id == selected and backend == family,
                      "size": details.download_size, "size_mb": details.download_size_mb,
                      "languages": details.language_support, "license": details.license,
                      "best_for": details.best_for, "limitations": list(details.limitations),
                      "repository_url": details.repository_url,
                      "status": download.get("status", "installed" if installed else "available"),
                      "progress": download.get("progress", min(1, download.get("done", 0) / download["total"]) if download.get("total") else 0),
                      "downloaded_bytes": download.get("done", 0), "total_bytes": download.get("total", 0),
                      "can_download": not installed and download.get("status") != "downloading",
                      "can_remove": installed and download.get("status") != "downloading"})
    return {"items": items, "selection": {"family": family, "id": selected}}


def _model(context, params):
    from services.model_catalog import MODEL_CATALOG
    from services.whisper_sources import custom_models
    model_id = identifier(params)
    if model_id not in MODEL_CATALOG and model_id not in custom_models(settings(context).load_all_settings()):
        raise ValueError("Choose a model from the catalog.")
    return model_id


def download(context, params):
    context.controller.request_model_download(_model(context, params))
    return changed("models.list")


def download_batch(context, params):
    items = params.get("ids")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Choose between 1 and 100 models.")
    models = [_model(context, {"id": model}) for model in items]
    context.controller.request_model_batch_download(models)
    return changed("models.list")


def remove(context, params):
    context.controller.request_model_delete(_model(context, params))
    return changed("models.list")


def select(context, params):
    from services.local_asr.catalog import backend_of
    model_id = _model(context, params)
    refusal = context.controller.select_host_model(backend_of(model_id), model_id)
    if refusal:
        raise ValueError(refusal)
    return changed("models.list", "settings.schema", "settings.values")


def cancel(context, _params):
    context.controller.cancel_model_batch_download()
    return changed("models.list")


def devices(context, _params):
    from services.audio_devices import list_input_devices, load_priority, default_input
    available = list_input_devices()
    default = default_input(devices=available)
    priority = load_priority(manager=settings(context))
    items = [{"id": "default", "name": "System default", "input_channels": 0, "default": True}]
    items.extend({"id": str(device.index), "name": device.display,
                  "input_channels": device.channels, "default": device == default,
                  "key": device.key, "hostapi": device.hostapi} for device in available)
    selected = next((str(device.index) for device in available if priority and device.key == priority[0]), "default")
    return {"items": items, "selected": selected, "priority": priority}


def select_device(context, params):
    from services.audio_devices import list_input_devices
    controller = context.controller
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job or controller.is_meeting_active():
        raise ValueError("Finish the recording before changing microphones.")
    device_id = str(params.get("id", ""))
    device = next((d for d in list_input_devices() if str(d.index) == device_id), None)
    if device_id != "default" and device is None:
        raise ValueError("That microphone is no longer available. Refresh the list.")
    settings(context).update_settings({"audio_input_priority": [device.key] if device else []})
    controller.change_audio_device()
    return changed("devices.list")


def device_priority(context, params):
    from services.audio_devices import normalize_priority
    priority = params.get("priority")
    if not isinstance(priority, list) or len(priority) > 50 or normalize_priority(priority) != priority:
        raise ValueError("Supply an ordered list of microphone name and hostapi pairs.")
    controller = context.controller
    if controller.recorder.is_recording or controller.transcription_runtime.has_active_job or controller.is_meeting_active():
        raise ValueError("Finish the recording before changing microphones.")
    settings(context).update_settings({"audio_input_priority": priority})
    controller.change_audio_device()
    return changed("devices.list")


def discover(context, params):
    from services.whisper_sources import discover_local_models, discover_hub_models
    from .common import string
    if "path" in params:
        sources = discover_local_models(string(params["path"], "model folder", required=True, maximum=4096))
    else:
        sources = discover_hub_models(string(params.get("repo"), "Hugging Face repository", required=True, maximum=200),
                                     string(params.get("subfolder", ""), "model subfolder", maximum=500))
    return {"items": [{"id": source, "source": source} for source in sources]}
