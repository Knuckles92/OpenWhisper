"""Custom CTranslate2 Whisper sources, validation, and folder discovery."""
from __future__ import annotations

import json
import ntpath
import os
from dataclasses import dataclass
from pathlib import Path

MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
DOWNLOAD_FILES = ("config.json", "preprocessor_config.json", "model.bin",
                  "tokenizer.json", "vocabulary.*")


@dataclass(frozen=True)
class WhisperSource:
    repo_id: str = ""
    subfolder: str = ""
    local_path: str = ""

    @property
    def name(self) -> str:
        return self.local_path or "/".join(
            part for part in (self.repo_id, self.subfolder) if part
        )

    @property
    def patterns(self) -> list[str]:
        return [f"{self.subfolder}/{name}" if self.subfolder else name
                for name in DOWNLOAD_FILES]


def hub_source(repo_id: str, subfolder: str = "") -> WhisperSource:
    """Validate separate repository and relative-folder fields without I/O."""
    from huggingface_hub.utils import validate_repo_id

    repo_id = repo_id.strip()
    validate_repo_id(repo_id)
    if len(repo_id.split("/")) != 2:
        raise ValueError("Enter a Hugging Face repository as owner/model.")
    subfolder = subfolder.strip().replace("\\", "/").rstrip("/")
    if subfolder and (subfolder.startswith("/") or any(
        part in ("", ".", "..") or any(char in part for char in ":*?[]")
        for part in subfolder.split("/")
    )):
        raise ValueError("Use a relative subfolder, such as ct2_int8_float16.")
    return WhisperSource(repo_id=repo_id, subfolder=subfolder)


def parse_source(name: str) -> WhisperSource:
    """Resolve aliases, owner/repo[/subfolder], or a local directory."""
    name = name.strip()
    expanded = os.path.expanduser(name)
    if os.path.isdir(expanded) or os.path.isabs(expanded) or ntpath.isabs(expanded):
        return WhisperSource(local_path=os.path.abspath(expanded))
    from services.model_catalog import MODEL_REPOSITORIES

    resolved = MODEL_REPOSITORIES.get(name, name)
    parts = resolved.split("/", 2)
    if len(parts) < 2:
        raise ValueError(f"Unknown Whisper model: {name}")
    return hub_source("/".join(parts[:2]), parts[2] if len(parts) == 3 else "")


def validate_model_folder(folder: str | Path) -> str:
    """Require ready-to-load files, including an offline tokenizer."""
    folder = Path(folder)
    missing = [name for name in MODEL_FILES
               if not (folder / name).is_file() or (folder / name).stat().st_size == 0]
    if missing:
        raise ValueError(f"{folder}: missing model files: {', '.join(missing)}")
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config.json must contain an object")
    except (OSError, ValueError) as exc:
        raise ValueError(f"{folder}: invalid config.json: {exc}") from exc
    return str(folder.resolve())


def discover_local_models(folder: str | Path) -> list[str]:
    """Find compatible model folders under a chosen parent without following links."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Choose an existing model folder.")
    found = []
    for current, directories, files in os.walk(root, followlinks=False):
        depth = len(Path(current).relative_to(root).parts)
        directories[:] = sorted(name for name in directories
                                if not name.startswith(".") and depth < 6)
        if set(MODEL_FILES).issubset(files):
            try:
                found.append(validate_model_folder(current))
            except ValueError:
                continue
    return sorted(set(found))


def discover_hub_models_in_process(repo_id: str, subfolder: str = "") -> list[str]:
    """Inspect repository filenames, without downloading weights."""
    from huggingface_hub import HfApi

    source = hub_source(repo_id, subfolder)
    groups: dict[str, set[str]] = {}
    for filename in HfApi().list_repo_files(source.repo_id):
        parent, _, basename = filename.rpartition("/")
        if source.subfolder and not (
            parent == source.subfolder or parent.startswith(source.subfolder + "/")
        ):
            continue
        groups.setdefault(parent, set()).add(basename)
    return [hub_source(source.repo_id, folder).name
            for folder, files in sorted(groups.items())
            if set(MODEL_FILES).issubset(files)]


def discover_hub_models(repo_id: str, subfolder: str = "") -> list[str]:
    """Bound metadata lookup to an isolated worker, away from Qt's UI thread."""
    import sys

    from services.local_asr.process import SpeechProcess
    from services.settings import is_hf_hub_offline_env_set

    source = hub_source(repo_id, subfolder)
    if is_hf_hub_offline_env_set():
        raise ValueError("Hugging Face is offline (HF_HUB_OFFLINE).")
    worker = SpeechProcess(sys.executable, isolated=True)
    try:
        return worker.request("discover_whisper_models", repo_id=source.repo_id,
                              subfolder=source.subfolder, timeout=60.)["models"]
    finally:
        worker.close()


def model_revision(name: str) -> str | None:
    """Return the bundled revision for a catalog alias; custom sources keep their own version."""
    from services.model_catalog import MODEL_REPOSITORIES, WHISPER_REVISIONS

    return WHISPER_REVISIONS.get(MODEL_REPOSITORIES.get(name, ""))


def cached_model_path(name: str) -> str:
    """Return a validated local directory, with no Hub metadata requests."""
    from huggingface_hub import snapshot_download

    source = parse_source(name)
    if source.local_path:
        return validate_model_folder(source.local_path)
    snapshot = snapshot_download(source.repo_id, local_files_only=True,
                                 revision=model_revision(name), allow_patterns=source.patterns)
    return validate_model_folder(Path(snapshot) / source.subfolder)


def is_custom_model(name: str) -> bool:
    from config import config
    from services.local_asr.catalog import MODELS
    from services.model_catalog import MODEL_REPOSITORIES

    if name in config.WHISPER_MODEL_CHOICES or name in MODELS or name in MODEL_REPOSITORIES:
        return False
    return "/" in name or "\\" in name or os.path.isdir(os.path.expanduser(name))


def custom_model_label(name: str) -> str:
    """Put the selected variant first so eliding long paths keeps it visible."""
    source = parse_source(name)
    if source.local_path:
        return f"{Path(source.local_path).name} · Local folder"
    if source.subfolder:
        return f"{source.subfolder.rsplit('/', 1)[-1]} · {source.repo_id}"
    return source.repo_id


def custom_models(settings: dict) -> list[str]:
    """Read registered sources; keep missing local folders available for removal."""
    from services.settings import SettingsKey

    values = settings.get(SettingsKey.CUSTOM_WHISPER_MODELS, [])
    found = []
    for name in values if isinstance(values, list) else []:
        if not isinstance(name, str) or not is_custom_model(name):
            continue
        try:
            canonical = parse_source(name).name
        except ValueError:
            continue
        if canonical not in found:
            found.append(canonical)
    return found


def discover_cached_models(cached: dict) -> list[str]:
    """Find root and nested models in cached repository snapshots, entirely offline."""
    found = []
    for repo_id, info in cached.items():
        # Sources use the default revision. An old detached snapshot must not
        # make a folder appear loadable when the current snapshot lacks it.
        try:
            revision = (Path(info.path) / "refs" / "main").read_text().strip()
        except OSError:
            continue
        if revision in info.revision_hashes:
            snapshot = Path(info.path) / "snapshots" / revision
            if not snapshot.is_dir():
                continue
            for folder in discover_local_models(snapshot):
                relative = Path(folder).relative_to(snapshot.resolve()).as_posix()
                try:
                    found.append(hub_source(repo_id, "" if relative == "." else relative).name)
                except ValueError:
                    continue
    return sorted(set(found))
