"""Discover existing compatible speech models on disk and import them in place.

Scans default model caches (Hugging Face Hub, Handy/transcribe.cpp, Moonshine
Voice, NeMo-Speech) or a user-chosen folder without copying or modifying any
files. Built-in speech models that match by size and SHA-256 are linked directly
into their built-in catalog slot via ``cache.link_external``; other compatible
speech GGUFs, Moonshine/MLX/Qwen folders, and CTranslate2 Whisper folders are
registered as custom models in place.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from config import local_app_dir
from services.hf_access import get_hf_cache_dir, invalidate_cached_models_snapshot
from services.local_asr import cache, custom
from services.local_asr.catalog import BUILTIN_MODELS, DEFAULT_MODELS, artifacts
from services.local_asr.gguf_meta import (
    inspect_gguf,
    match_builtin_gguf,
    match_builtin_gguf_by_size,
    suggest_backend_from_filename,
)
from services.model_catalog import MODEL_REPOSITORIES
from services.settings import SettingsKey, settings_manager
from services.whisper_sources import (
    MODEL_FILES as WHISPER_MODEL_FILES,
    custom_model_label,
    custom_models,
    discover_local_models,
    hub_source,
    validate_model_folder,
)

logger = logging.getLogger(__name__)

_OPTIONAL_NOTICE_FILES = frozenset({"LICENSE", "NOTICE"})
_MAX_SCAN_DEPTH = 4


@dataclass(frozen=True)
class DiscoveredModel:
    """One compatible speech model found on disk that OpenWhisper can reuse."""

    #: Stable dedup and dismissal identifier (e.g. ``builtin:parakeet-v3``).
    id: str
    #: ``"builtin_speech"``, ``"custom_whisper"``, or ``"custom_speech"``.
    kind: str
    #: Target backend id (``"local_whisper"`` or a key in ``catalog.BACKENDS``).
    backend: str
    label: str
    #: Resolved filesystem path, or HF ``owner/repo[/subfolder]`` for custom Whisper.
    path: str
    #: Human-friendly origin shown in the import screen (e.g. ``"Hugging Face cache"``).
    source_label: str
    size_bytes: int
    builtin_key: str = ""
    arch: str = ""
    streaming: bool | None = None
    meeting: bool | None = None
    multilingual: bool | None = None
    gguf_architecture: str = ""
    #: True when the engine was inferred weakly and the user may want to pick it.
    needs_engine_choice: bool = False


@dataclass(frozen=True)
class ImportResult:
    """Outcome of importing a batch of discovered models."""

    imported: tuple[DiscoveredModel, ...]
    failed: tuple[tuple[DiscoveredModel, str], ...]
    reused_bytes: int


def default_search_locations() -> list[tuple[Path, str]]:
    """Default local cache directories that may already hold speech models."""
    home = Path.home()
    locations: list[tuple[Path, str]] = [
        (Path(get_hf_cache_dir()), "Hugging Face cache"),
    ]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if appdata:
            locations.append((Path(appdata) / "com.pais.handy" / "models", "Handy models"))
        if local:
            locations.append((Path(local) / "com.pais.handy" / "models", "Handy models"))
            locations.append((Path(local) / "transcribe.cpp" / "models", "transcribe.cpp cache"))
            locations.append((Path(local) / "moonshine_voice", "Moonshine cache"))
    elif sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        locations.append((app_support / "com.pais.handy" / "models", "Handy models"))
    else:
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share")))
        locations.append((xdg_data / "handy" / "models", "Handy models"))
        locations.append((xdg_data / "com.pais.handy" / "models", "Handy models"))

    locations.extend([
        (home / ".cache" / "transcribe.cpp", "transcribe.cpp cache"),
        (home / ".cache" / "moonshine_voice", "Moonshine cache"),
        (home / ".cache" / "nemo-speech", "NeMo-Speech cache"),
        (home / ".cache" / "nemo", "NeMo cache"),
    ])

    deduped: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for folder, label in locations:
        try:
            key = str(folder.expanduser().resolve()).casefold()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            deduped.append((folder, label))
    return deduped


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _folder_size(folder: Path) -> int:
    try:
        return sum(item.stat().st_size for item in folder.iterdir() if item.is_file())
    except OSError:
        return 0


def _own_speech_models_dir() -> Path | None:
    try:
        return (Path(local_app_dir()) / "speech-models").resolve()
    except OSError:
        return None


def _is_under(path: Path, parent: Path | None) -> bool:
    if parent is None:
        return False
    try:
        path.resolve().relative_to(parent)
        return True
    except (ValueError, OSError):
        return False


def _folder_matches_builtin(folder: Path, addable: set[str], cached_builtins: set[str]) -> str | None:
    """Return an uncached built-in folder model key if ``folder`` has all its files at exact size."""
    for key, model in BUILTIN_MODELS.items():
        if key in cached_builtins or model.backend not in addable:
            continue
        spec = artifacts(key)
        if any(f["name"].endswith(".gguf") for f in spec["files"]):
            continue
        required = [f for f in spec["files"] if f["name"] not in _OPTIONAL_NOTICE_FILES]
        if not required:
            continue
        try:
            if all((folder / f["name"]).is_file() and (folder / f["name"]).stat().st_size == f["size_bytes"]
                   for f in required):
                return key
        except OSError:
            continue
    return None


def _guess_moonshine_arch(folder: Path) -> str:
    lowered = folder.name.lower()
    if "medium" in lowered:
        return "medium"
    if "small" in lowered:
        return "small"
    # Medium streaming is ~269 MB; small streaming is ~142 MB.
    return "medium" if _folder_size(folder) >= 200_000_000 else "small"


def _custom_gguf_candidate(
    file_path: Path,
    source_label: str,
    addable_gguf: list[str],
    *,
    allow_filename_hint: bool,
) -> DiscoveredModel | None:
    """Build a ``DiscoveredModel`` for a non-builtin speech GGUF, or None."""
    if not addable_gguf:
        return None
    info = inspect_gguf(file_path)
    if not info.valid_magic or info.kind == "non_speech":
        return None
    if info.kind != "speech":
        if not allow_filename_hint:
            return None
        hint = suggest_backend_from_filename(file_path.name)
        if not hint or hint not in addable_gguf:
            return None
        backend = hint
        needs_choice = True
    else:
        backend = info.suggested_backend if info.suggested_backend in addable_gguf else ""
        if not backend:
            return None
        needs_choice = not bool(info.runtime)

    try:
        resolved = file_path.resolve()
        size = resolved.stat().st_size
    except OSError:
        return None

    base = BUILTIN_MODELS[DEFAULT_MODELS[backend]]
    streaming = info.streaming if info.streaming is not None else base.streaming
    meeting = base.meeting
    multilingual = info.multilingual
    label = info.name or resolved.stem
    return DiscoveredModel(
        id=f"path:{str(resolved).casefold()}",
        kind="custom_speech",
        backend=backend,
        label=label,
        path=str(resolved),
        source_label=source_label,
        size_bytes=size,
        streaming=streaming,
        meeting=meeting,
        multilingual=multilingual,
        gguf_architecture=info.architecture,
        needs_engine_choice=needs_choice,
    )


def _matches_cached_builtin_gguf(file_path: Path, cached_builtins: set[str], *, verify_hash: bool) -> bool:
    """True when ``file_path`` is identical to a built-in model that is already installed."""
    size_matches = [key for key in match_builtin_gguf_by_size(file_path) if key in cached_builtins]
    if not size_matches:
        return False
    matched = match_builtin_gguf(file_path, verify_hash=verify_hash)
    return bool(matched and matched in cached_builtins)


def _builtin_gguf_candidate(
    file_path: Path,
    source_label: str,
    addable_gguf: set[str],
    cached_builtins: set[str],
    *,
    verify_hash: bool,
) -> DiscoveredModel | None:
    """Return a built-in speech candidate if ``file_path`` matches an uncached built-in GGUF."""
    size_matches = [
        key for key in match_builtin_gguf_by_size(file_path)
        if key not in cached_builtins and BUILTIN_MODELS[key].backend in addable_gguf
    ]
    if not size_matches:
        return None
    matched = match_builtin_gguf(file_path, verify_hash=verify_hash)
    if not matched or matched in cached_builtins or BUILTIN_MODELS[matched].backend not in addable_gguf:
        return None
    try:
        resolved = file_path.resolve()
        size = resolved.stat().st_size
    except OSError:
        return None
    model = BUILTIN_MODELS[matched]
    return DiscoveredModel(
        id=f"builtin:{matched}",
        kind="builtin_speech",
        backend=model.backend,
        label=model.label,
        path=str(resolved),
        source_label=source_label,
        size_bytes=size,
        builtin_key=matched,
        streaming=model.streaming,
        meeting=model.meeting,
        multilingual=(model.languages != "English"),
    )


def _hf_snapshot_dirs(repo_dir: Path) -> list[Path]:
    """Return active snapshot directories for one ``models--*`` HF hub cache folder."""
    snapshots_root = repo_dir / "snapshots"
    if not _safe_is_dir(snapshots_root):
        return []
    ordered: list[Path] = []
    seen: set[str] = set()
    try:
        main_ref = (repo_dir / "refs" / "main").read_text(encoding="utf-8").strip()
        if main_ref:
            candidate = snapshots_root / main_ref
            if _safe_is_dir(candidate):
                ordered.append(candidate)
                seen.add(main_ref)
    except OSError:
        pass
    try:
        for child in sorted(snapshots_root.iterdir()):
            if child.name not in seen and _safe_is_dir(child):
                ordered.append(child)
    except OSError:
        pass
    return ordered


def _scan_hf_hub_cache(
    hub_root: Path,
    source_label: str,
    *,
    addable: set[str],
    addable_gguf: list[str],
    cached_builtins: set[str],
    registered_speech_paths: set[str],
    registered_whisper: set[str],
    known_whisper_repos: set[str],
    verify_builtin_hashes: bool,
) -> list[DiscoveredModel]:
    found: list[DiscoveredModel] = []
    if not _safe_is_dir(hub_root):
        return found
    addable_gguf_set = set(addable_gguf)
    try:
        entries = sorted(hub_root.iterdir())
    except OSError:
        return found

    for repo_dir in entries:
        if not repo_dir.name.startswith("models--") or not _safe_is_dir(repo_dir):
            continue
        raw = repo_dir.name.removeprefix("models--")
        if "--" not in raw:
            continue
        repo_id = raw.replace("--", "/", 1)
        snapshots = _hf_snapshot_dirs(repo_dir)
        if not snapshots:
            continue

        matched_repo = False
        # 1. Check for built-in speech models (GGUF or multi-file folder) in any snapshot.
        for snapshot in snapshots:
            folder_key = _folder_matches_builtin(snapshot, addable, cached_builtins)
            if folder_key:
                model = BUILTIN_MODELS[folder_key]
                spec = artifacts(folder_key)
                required = [f for f in spec["files"] if f["name"] not in _OPTIONAL_NOTICE_FILES]
                found.append(DiscoveredModel(
                    id=f"builtin:{folder_key}",
                    kind="builtin_speech",
                    backend=model.backend,
                    label=model.label,
                    path=str(snapshot.resolve()),
                    source_label=source_label,
                    size_bytes=sum(f["size_bytes"] for f in required),
                    builtin_key=folder_key,
                    streaming=model.streaming,
                    meeting=model.meeting,
                    multilingual=(model.languages != "English"),
                ))
                matched_repo = True
                break
            try:
                gguf_files = [item for item in snapshot.iterdir()
                              if item.is_file() and item.suffix.lower() == ".gguf"]
            except OSError:
                gguf_files = []
            for gguf_file in gguf_files:
                try:
                    resolved_key = str(gguf_file.resolve()).casefold()
                except OSError:
                    continue
                if resolved_key in registered_speech_paths:
                    continue
                builtin_item = _builtin_gguf_candidate(
                    gguf_file, source_label, addable_gguf_set, cached_builtins,
                    verify_hash=verify_builtin_hashes,
                )
                if builtin_item is not None:
                    found.append(builtin_item)
                    matched_repo = True
                    break
            if matched_repo:
                break
        if matched_repo:
            continue

        # 2. Check the primary snapshot for custom CTranslate2 Whisper or custom speech GGUFs.
        primary = snapshots[0]
        if repo_id not in known_whisper_repos:
            try:
                whisper_folders = discover_local_models(primary)
            except (OSError, ValueError):
                whisper_folders = []
            for folder in whisper_folders:
                try:
                    rel = Path(folder).resolve().relative_to(primary.resolve()).as_posix()
                    name = hub_source(repo_id, "" if rel == "." else rel).name
                except (ValueError, OSError):
                    continue
                if name in registered_whisper or name in known_whisper_repos:
                    continue
                found.append(DiscoveredModel(
                    id=f"whisper:{name}",
                    kind="custom_whisper",
                    backend="local_whisper",
                    label=custom_model_label(name),
                    path=name,
                    source_label=source_label,
                    size_bytes=_folder_size(Path(folder)),
                ))

        try:
            gguf_files = [item for item in primary.iterdir()
                          if item.is_file() and item.suffix.lower() == ".gguf"]
        except OSError:
            gguf_files = []
        for gguf_file in gguf_files:
            try:
                resolved_key = str(gguf_file.resolve()).casefold()
            except OSError:
                continue
            if resolved_key in registered_speech_paths:
                continue
            if _matches_cached_builtin_gguf(gguf_file, cached_builtins, verify_hash=verify_builtin_hashes):
                continue
            custom_item = _custom_gguf_candidate(
                gguf_file, f"{source_label} ({repo_id})", addable_gguf,
                allow_filename_hint=False,
            )
            if custom_item is not None:
                found.append(custom_item)

    return found


def _scan_directory(
    root: Path,
    source_label: str,
    *,
    addable: set[str],
    addable_gguf: list[str],
    cached_builtins: set[str],
    registered_speech_paths: set[str],
    registered_whisper: set[str],
    verify_builtin_hashes: bool,
    allow_filename_hint: bool,
) -> list[DiscoveredModel]:
    found: list[DiscoveredModel] = []
    if not _safe_is_dir(root):
        return found
    own_dir = _own_speech_models_dir()
    addable_gguf_set = set(addable_gguf)
    try:
        resolved_root = root.resolve()
    except OSError:
        return found

    moonshine_required = (
        set(custom.model_format("moonshine").required)
        if "moonshine" in addable else set()
    )

    for current, directories, files in os.walk(resolved_root, followlinks=False):
        current_path = Path(current)
        if _is_under(current_path, own_dir):
            directories[:] = []
            continue
        try:
            depth = len(current_path.relative_to(resolved_root).parts)
        except ValueError:
            depth = 0
        directories[:] = sorted(
            name for name in directories
            if not name.startswith(".") and depth < _MAX_SCAN_DEPTH
        )

        file_set = set(files)
        resolved_dir_key = str(current_path.resolve()).casefold()

        # 1. Built-in folder match (MLX / Moonshine / macOS Qwen).
        folder_key = _folder_matches_builtin(current_path, addable, cached_builtins)
        if folder_key:
            model = BUILTIN_MODELS[folder_key]
            spec = artifacts(folder_key)
            required = [f for f in spec["files"] if f["name"] not in _OPTIONAL_NOTICE_FILES]
            found.append(DiscoveredModel(
                id=f"builtin:{folder_key}",
                kind="builtin_speech",
                backend=model.backend,
                label=model.label,
                path=str(current_path.resolve()),
                source_label=source_label,
                size_bytes=sum(f["size_bytes"] for f in required),
                builtin_key=folder_key,
                streaming=model.streaming,
                meeting=model.meeting,
                multilingual=(model.languages != "English"),
            ))
            directories[:] = []
            continue

        # 2. Custom Moonshine folder.
        if moonshine_required and moonshine_required.issubset(file_set) and resolved_dir_key not in registered_speech_paths:
            arch = _guess_moonshine_arch(current_path)
            found.append(DiscoveredModel(
                id=f"path:{resolved_dir_key}",
                kind="custom_speech",
                backend="moonshine",
                label=current_path.name,
                path=str(current_path.resolve()),
                source_label=source_label,
                size_bytes=_folder_size(current_path),
                arch=arch,
                streaming=True,
                meeting=True,
                multilingual=False,
            ))
            directories[:] = []
            continue

        # 3. Local CTranslate2 Whisper folder.
        if set(WHISPER_MODEL_FILES).issubset(file_set):
            try:
                validated = validate_model_folder(current_path)
            except ValueError:
                validated = ""
            if validated and validated not in registered_whisper:
                found.append(DiscoveredModel(
                    id=f"whisper:{validated.casefold()}",
                    kind="custom_whisper",
                    backend="local_whisper",
                    label=custom_model_label(validated),
                    path=validated,
                    source_label=source_label,
                    size_bytes=_folder_size(current_path),
                ))
                directories[:] = []
                continue

        # 4. GGUF files inside this folder.
        for filename in sorted(files):
            if not filename.lower().endswith(".gguf"):
                continue
            file_path = current_path / filename
            try:
                resolved_key = str(file_path.resolve()).casefold()
            except OSError:
                continue
            if resolved_key in registered_speech_paths:
                continue
            builtin_item = _builtin_gguf_candidate(
                file_path, source_label, addable_gguf_set, cached_builtins,
                verify_hash=verify_builtin_hashes,
            )
            if builtin_item is not None:
                found.append(builtin_item)
                continue
            if _matches_cached_builtin_gguf(file_path, cached_builtins, verify_hash=verify_builtin_hashes):
                continue
            custom_item = _custom_gguf_candidate(
                file_path, source_label, addable_gguf,
                allow_filename_hint=allow_filename_hint,
            )
            if custom_item is not None:
                found.append(custom_item)

    return found


def _deduplicate(items: Iterable[DiscoveredModel]) -> list[DiscoveredModel]:
    """Keep one entry per built-in key, path, or discovery id (built-ins win)."""
    # Sort so built-in matches come first and win over generic path entries.
    prioritized = sorted(
        items,
        key=lambda item: (0 if item.kind == "builtin_speech" else 1, item.backend, item.label.casefold()),
    )
    result: list[DiscoveredModel] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_builtins: set[str] = set()
    for item in prioritized:
        path_key = item.path.casefold()
        if item.id in seen_ids or path_key in seen_paths:
            continue
        if item.builtin_key and item.builtin_key in seen_builtins:
            continue
        seen_ids.add(item.id)
        seen_paths.add(path_key)
        if item.builtin_key:
            seen_builtins.add(item.builtin_key)
        result.append(item)
    return result


def _scan_context(settings: dict | None = None):
    if settings is None:
        try:
            settings = settings_manager.load_all_settings()
        except Exception:
            settings = {}
    addable_list = custom.addable_backends()
    addable = set(addable_list)
    addable_gguf = custom.addable_gguf_backends()
    cached_builtins = {key for key in BUILTIN_MODELS if cache.is_cached(key)}
    registered_speech_paths = set()
    for model in custom.registered().values():
        try:
            registered_speech_paths.add(str(Path(model.path).resolve()).casefold())
        except OSError:
            registered_speech_paths.add(model.path.casefold())
    for info in cache.inventory().values():
        if getattr(info, "linked", False) and info.path:
            try:
                registered_speech_paths.add(str(Path(info.path).resolve()).casefold())
            except OSError:
                registered_speech_paths.add(info.path.casefold())
    registered_whisper = set(custom_models(settings))
    known_whisper_repos = set(MODEL_REPOSITORIES.values())
    return (
        addable, addable_gguf, cached_builtins,
        registered_speech_paths, registered_whisper, known_whisper_repos,
    )


def discover_existing_models(
    *,
    locations: Sequence[tuple[Path, str]] | None = None,
    settings: dict | None = None,
    verify_builtin_hashes: bool = True,
) -> list[DiscoveredModel]:
    """Scan default local model directories for reusable speech models. No network."""
    (
        addable, addable_gguf, cached_builtins,
        registered_speech_paths, registered_whisper, known_whisper_repos,
    ) = _scan_context(settings)
    search_dirs = list(locations) if locations is not None else default_search_locations()
    hf_root = Path(get_hf_cache_dir())
    try:
        hf_resolved = str(hf_root.resolve()).casefold()
    except OSError:
        hf_resolved = ""

    discovered: list[DiscoveredModel] = []
    for folder, label in search_dirs:
        if not _safe_is_dir(folder):
            continue
        try:
            is_hf = bool(hf_resolved) and str(folder.resolve()).casefold() == hf_resolved
        except OSError:
            is_hf = False
        if is_hf or any(child.name.startswith("models--") for child in _safe_iterdir(folder)):
            discovered.extend(_scan_hf_hub_cache(
                folder, label,
                addable=addable,
                addable_gguf=addable_gguf,
                cached_builtins=cached_builtins,
                registered_speech_paths=registered_speech_paths,
                registered_whisper=registered_whisper,
                known_whisper_repos=known_whisper_repos,
                verify_builtin_hashes=verify_builtin_hashes,
            ))
        else:
            discovered.extend(_scan_directory(
                folder, label,
                addable=addable,
                addable_gguf=addable_gguf,
                cached_builtins=cached_builtins,
                registered_speech_paths=registered_speech_paths,
                registered_whisper=registered_whisper,
                verify_builtin_hashes=verify_builtin_hashes,
                allow_filename_hint=False,
            ))
    return _deduplicate(discovered)


def discover_models_in_folder(
    folder: str | Path,
    *,
    settings: dict | None = None,
    verify_builtin_hashes: bool = True,
) -> list[DiscoveredModel]:
    """Scan a user-selected directory for reusable speech models."""
    target = Path(folder).expanduser()
    if not _safe_is_dir(target):
        raise ValueError("Choose an existing folder.")
    (
        addable, addable_gguf, cached_builtins,
        registered_speech_paths, registered_whisper, known_whisper_repos,
    ) = _scan_context(settings)
    label = target.name or str(target)
    if any(child.name.startswith("models--") for child in _safe_iterdir(target)):
        found = _scan_hf_hub_cache(
            target, label,
            addable=addable,
            addable_gguf=addable_gguf,
            cached_builtins=cached_builtins,
            registered_speech_paths=registered_speech_paths,
            registered_whisper=registered_whisper,
            known_whisper_repos=known_whisper_repos,
            verify_builtin_hashes=verify_builtin_hashes,
        )
    else:
        found = _scan_directory(
            target, label,
            addable=addable,
            addable_gguf=addable_gguf,
            cached_builtins=cached_builtins,
            registered_speech_paths=registered_speech_paths,
            registered_whisper=registered_whisper,
            verify_builtin_hashes=verify_builtin_hashes,
            allow_filename_hint=True,
        )
    return _deduplicate(found)


def _safe_iterdir(folder: Path) -> list[Path]:
    try:
        return list(folder.iterdir())
    except OSError:
        return []


def unseen_discoveries(
    items: Sequence[DiscoveredModel],
    settings: dict | None = None,
) -> list[DiscoveredModel]:
    """Filter out models whose discovery IDs the user already dismissed."""
    if settings is None:
        try:
            settings = settings_manager.load_all_settings()
        except Exception:
            settings = {}
    dismissed = {
        value for value in SettingsKey.DISMISSED_IMPORT_MODELS.read(settings)
        if isinstance(value, str)
    }
    return [item for item in items if item.id not in dismissed]


def dismiss_discoveries(ids: Iterable[str]) -> None:
    """Persist discovery IDs so the first-time alert does not show them again."""
    new_ids = [item_id for item_id in ids if isinstance(item_id, str) and item_id]
    if not new_ids:
        return

    def save(values):
        existing = [
            value for value in SettingsKey.DISMISSED_IMPORT_MODELS.read(values)
            if isinstance(value, str)
        ]
        values[SettingsKey.DISMISSED_IMPORT_MODELS] = list(dict.fromkeys([*existing, *new_ids]))

    settings_manager.mutate_settings(save)


def import_discovered_models(items: Sequence[DiscoveredModel]) -> ImportResult:
    """Link built-in matches and register custom models in place (no file copies)."""
    imported: list[DiscoveredModel] = []
    failed: list[tuple[DiscoveredModel, str]] = []
    whisper_to_add: list[DiscoveredModel] = []

    for item in items:
        try:
            if item.kind == "builtin_speech":
                cache.link_external(item.builtin_key, item.path, verify_hash=True)
                imported.append(item)
            elif item.kind == "custom_whisper":
                whisper_to_add.append(item)
            elif item.kind == "custom_speech":
                custom.register(
                    item.backend,
                    item.path,
                    item.label,
                    item.arch,
                    streaming=item.streaming,
                    meeting=item.meeting,
                    multilingual=item.multilingual,
                )
                imported.append(item)
            else:
                failed.append((item, f"Unsupported model kind: {item.kind}"))
        except Exception as exc:
            logger.warning("Failed to import discovered model %s: %s", item.label, exc)
            failed.append((item, str(exc)))

    if whisper_to_add:
        try:
            def add_whisper(values):
                existing = custom_models(values)
                values[SettingsKey.CUSTOM_WHISPER_MODELS] = list(dict.fromkeys([
                    *existing, *(item.path for item in whisper_to_add)
                ]))
            settings_manager.mutate_settings(add_whisper)
            imported.extend(whisper_to_add)
        except Exception as exc:
            logger.warning("Failed to save imported Whisper models: %s", exc)
            for item in whisper_to_add:
                failed.append((item, str(exc)))

    if imported:
        invalidate_cached_models_snapshot()

    reused_bytes = sum(item.size_bytes for item in imported)
    return ImportResult(
        imported=tuple(imported),
        failed=tuple(failed),
        reused_bytes=reused_bytes,
    )
