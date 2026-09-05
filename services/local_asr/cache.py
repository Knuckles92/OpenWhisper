from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from config import local_app_dir
from services.local_asr.catalog import MODELS, artifacts

_locks = {key: threading.Lock() for key in MODELS}


def model_dir(key: str) -> Path:
    if key not in MODELS:
        raise ValueError("Unknown speech model")
    return Path(local_app_dir()) / "speech-models" / key


def is_cached(key: str) -> bool:
    directory = model_dir(key)
    spec = artifacts(key)
    try:
        if json.loads((directory / "installed.json").read_text()) != spec:
            return False
        return all((directory / f["name"]).stat().st_size == f["size_bytes"] for f in spec["files"])
    except (OSError, ValueError):
        return False


def load_path(key: str) -> str:
    if not is_cached(key):
        raise RuntimeError(f"{MODELS[key].label} is not downloaded. Open Downloads.")
    spec = artifacts(key)
    gguf = next((f["name"] for f in spec["files"] if f["name"].endswith(".gguf")), None)
    return str(model_dir(key) / gguf if gguf else model_dir(key))


def download(key: str, progress_callback=None, cancel: threading.Event | None = None) -> str:
    # Consent belongs to the existing coordinator; direct callers must pass its
    # policy gate before entering this function. The hard offline override also
    # applies to Moonshine's non-Hub file host.
    from services.settings import is_hf_hub_offline_env_set
    from services.components import _download_verified, ComponentCanceled
    if is_hf_hub_offline_env_set():
        raise RuntimeError("Model downloads are disabled by HF_HUB_OFFLINE.")
    cancel = cancel or threading.Event()
    spec = artifacts(key)
    target = model_dir(key)
    total = sum(f["size_bytes"] for f in spec["files"])
    with _locks[key]:
        if is_cached(key):
            return str(target)
        staging = target.with_name(target.name + ".partial")
        staging.mkdir(parents=True, exist_ok=True)
        done = 0
        for file in spec["files"]:
            if cancel.is_set():
                raise ComponentCanceled()
            def progress(_phase, value, count):
                if progress_callback:
                    progress_callback(value, count)
            _download_verified(file["url"], file["sha256"], file["size_bytes"],
                               str(staging / file["name"]), progress, cancel,
                               offset_base=done, grand_total=total)
            done += file["size_bytes"]
        (staging / "installed.json").write_text(json.dumps(spec), encoding="utf-8")
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except OSError:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return str(target)


def delete(key: str) -> None:
    with _locks[key]:
        for directory in (model_dir(key), model_dir(key).with_name(key + ".partial")):
            if directory.exists():
                shutil.rmtree(directory)


def inventory() -> dict:
    from services.hf_access import CachedModelInfo
    return {
        artifacts(key)["repo"]: CachedModelInfo(
            artifacts(key)["repo"],
            sum(f["size_bytes"] for f in artifacts(key)["files"]),
            str(model_dir(key)),
            (artifacts(key)["revision"],),
        )
        for key in MODELS if is_cached(key)
    }

