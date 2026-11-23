from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from config import bundle_root, local_app_dir
from services.local_asr.catalog import BUILTIN_MODELS, MODELS, artifacts, uses_transcribe_cpp

BUNDLED_MODELS_DIRNAME = "bundled_models"
#: A custom model's files are the person's own, wherever they keep them.
_CUSTOM_FILES_KEPT = "Remove this custom model from the list instead; its files are kept."

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def model_dir(key: str) -> Path:
    """Where a built-in model downloads to."""
    if key not in BUILTIN_MODELS:
        raise ValueError("Unknown speech model")
    return Path(local_app_dir()) / "speech-models" / key


def bundled_model_dir(key: str) -> Path:
    """Return the read-only path of a model packaged inside the app bundle."""
    if key not in BUILTIN_MODELS:
        raise ValueError("Unknown speech model")
    return Path(bundle_root()) / BUNDLED_MODELS_DIRNAME / key


def _custom(key: str):
    """The custom model ``key`` names, or None for a built-in one."""
    model = MODELS.get(key)
    return model if model is not None and model.custom else None


def _dir_is_valid(directory: Path, spec: dict) -> bool:
    try:
        if json.loads((directory / "installed.json").read_text(encoding="utf-8")) != spec:
            return False
        return all((directory / f["name"]).stat().st_size == f["size_bytes"] for f in spec["files"])
    except (OSError, ValueError):
        return False


def is_bundled(key: str) -> bool:
    """True when ``key`` is shipped intact inside the application bundle."""
    if key not in BUILTIN_MODELS:
        return False
    return _dir_is_valid(bundled_model_dir(key), artifacts(key))


def active_model_dir(key: str) -> Path:
    """Return the user-downloaded directory when valid, else the bundled one."""
    user_dir = model_dir(key)
    spec = artifacts(key)
    if _dir_is_valid(user_dir, spec):
        return user_dir
    bundled = bundled_model_dir(key)
    if _dir_is_valid(bundled, spec):
        return bundled
    return user_dir


def is_cached(key: str) -> bool:
    custom = _custom(key)
    if custom is not None:
        from services.local_asr.custom import is_present
        return is_present(custom)
    spec = artifacts(key)
    return _dir_is_valid(model_dir(key), spec) or _dir_is_valid(bundled_model_dir(key), spec)


def needs_qwen_redownload(key: str) -> bool:
    model = BUILTIN_MODELS.get(key)
    if model is None or model.backend != "qwen_asr" or not uses_transcribe_cpp(model.backend):
        return False
    # The old model lived here as one or more Safetensors files. The cache
    # replaces that directory only after the GGUF download succeeds.
    return any(model_dir(key).glob("*.safetensors")) and not is_cached(key)


def load_path(key: str) -> str:
    custom = _custom(key)
    if custom is not None:
        if not is_cached(key):
            raise RuntimeError(f"{custom.label}'s files are no longer at {custom.path}.")
        return custom.path
    if not is_cached(key):
        raise RuntimeError(f"{MODELS[key].label} is not downloaded. Open Downloads.")
    directory = active_model_dir(key)
    spec = artifacts(key)
    gguf = next((f["name"] for f in spec["files"] if f["name"].endswith(".gguf")), None)
    return str(directory / gguf if gguf else directory)


def download(key: str, progress_callback=None, cancel: threading.Event | None = None) -> str:
    # Consent belongs to the existing coordinator; direct callers must pass its
    # policy gate before entering this function. The hard offline override also
    # applies to Moonshine's non-Hub file host.
    from services.settings import is_hf_hub_offline_env_set
    from services.components import _download_verified, ComponentCanceled
    if is_hf_hub_offline_env_set():
        raise RuntimeError("Model downloads are disabled by HF_HUB_OFFLINE.")
    custom = _custom(key)
    if custom is not None:
        if is_cached(key):
            return custom.path
        raise RuntimeError(f"{custom.label} isn't downloaded: it is your own file, and it is "
                           f"no longer at {custom.path}. Add it again from where it is now.")
    cancel = cancel or threading.Event()
    spec = artifacts(key)
    target = model_dir(key)
    from filelock import FileLock
    target.parent.mkdir(parents=True, exist_ok=True)
    total = sum(f["size_bytes"] for f in spec["files"])
    # Downloads run in killable workers. Protect the staging/backup swap
    # across processes as well as concurrent requests in this interpreter.
    with _lock(key), FileLock(str(target) + ".lock"):
        backup = target.with_name(target.name + ".previous")
        if not target.exists() and backup.exists():
            os.replace(backup, target)
        if is_cached(key):
            return str(active_model_dir(key))
        staging = target.with_name(target.name + ".partial")
        staging.mkdir(parents=True, exist_ok=True)

        def progress(_phase, value, count):
            if progress_callback:
                progress_callback(value, count)

        done = 0
        for file in spec["files"]:
            if cancel.is_set():
                raise ComponentCanceled()
            _download_verified(file["url"], file["sha256"], file["size_bytes"],
                               str(staging / file["name"]), progress, cancel,
                               offset_base=done, grand_total=total)
            done += file["size_bytes"]
        (staging / "installed.json").write_text(json.dumps(spec), encoding="utf-8")
        _swap_into_place(staging, target)
    return str(target)


def _swap_into_place(staging: Path, target: Path) -> None:
    """Replace ``target`` with ``staging``, restoring the old copy if the rename fails."""
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


def delete(key: str) -> None:
    from filelock import FileLock
    if _custom(key) is not None:
        raise ValueError(_CUSTOM_FILES_KEPT)
    target = model_dir(key)
    user_dirs = (
        target,
        target.with_name(target.name + ".partial"),
        # An interrupted swap leaves this; download() would move it back.
        target.with_name(target.name + ".previous"),
    )
    if not any(d.exists() for d in user_dirs) and is_bundled(key):
        raise ValueError(f"{MODELS[key].label} is included with OpenWhisper and cannot be deleted.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock(key), FileLock(str(target) + ".lock"):
        for directory in user_dirs:
            if directory.exists():
                shutil.rmtree(directory)


def inventory() -> dict:
    from services.hf_access import CachedModelInfo
    result = {}
    for key in BUILTIN_MODELS:
        spec = artifacts(key)
        user_valid = _dir_is_valid(model_dir(key), spec)
        bundled_valid = _dir_is_valid(bundled_model_dir(key), spec)
        if not (user_valid or bundled_valid):
            continue
        directory = model_dir(key) if user_valid else bundled_model_dir(key)
        result[spec["repo"]] = CachedModelInfo(
            spec["repo"],
            sum(f["size_bytes"] for f in spec["files"]),
            str(directory),
            (spec["revision"],),
            bundled=not user_valid and bundled_valid,
        )
    result.update(_custom_inventory())
    return result


def _custom_inventory() -> dict:
    """Custom models whose files are in place, keyed by model key.

    ``hf_access.resolve_model_repo`` gives a key it has no repository for
    back unchanged, so Downloads finds these entries by the key.
    """
    from services.hf_access import CachedModelInfo
    from services.local_asr.custom import is_present, registered, size_bytes
    result = {}
    for key, model in registered().items():
        try:
            if is_present(model):
                result[key] = CachedModelInfo(key, size_bytes(model), model.path, ())
        except OSError:
            continue
    return result
