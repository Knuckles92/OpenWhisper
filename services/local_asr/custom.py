"""Speech models the person adds for the local engines, from files on this computer.

Each one is saved in ``SettingsKey.CUSTOM_SPEECH_MODELS`` as
``{"key", "backend", "label", "path"}``, plus ``"arch"`` for Moonshine.
A custom model loads like a built-in one of its engine and takes that
engine's abilities (live text, meetings) from the engine's default model.
Its languages are whatever it was trained on, so it claims none. Its files stay where they are: OpenWhisper never copies,
downloads or deletes them, and a host never offers them to paired computers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from services.local_asr.catalog import (
    BACKENDS,
    BUILTIN_MODELS,
    DEFAULT_MODELS,
    SpeechModel,
    artifacts,
    runtime_id,
)
from services.local_asr.languages import LANGUAGE_LABELS

#: Starts every custom model's key; built-in keys never do. The key has no
#: slash, so Whisper's custom sources never mistake it for a repository.
CUSTOM_PREFIX = "custom-"
MOONSHINE_ARCHES = ("small", "medium")
#: Files a model's publisher includes that its engine does not load.
_NOTICE_FILES = ("LICENSE", "NOTICE")
_WEIGHTS = "*.safetensors"


@dataclass(frozen=True)
class ModelFormat:
    """What an engine loads: one GGUF file, or a folder of files."""

    gguf: bool
    #: A folder model's files, by name.
    required: tuple[str, ...] = ()
    #: A folder model's weights, by pattern; "" when ``required`` names them.
    weights: str = ""

    def describe(self) -> str:
        return "a .gguf file" if self.gguf else "a model folder"


def model_format(backend: str) -> ModelFormat:
    """What ``backend`` loads on this computer, read from its default model's files.

    Qwen3-ASR loads a GGUF through transcribe.cpp on Windows and Linux and
    a Safetensors folder through PyTorch on a Mac, as its built-in models do.
    """
    names = [file["name"] for file in artifacts(DEFAULT_MODELS[backend])["files"]
             if file["name"] not in _NOTICE_FILES]
    if any(name.endswith(".gguf") for name in names):
        return ModelFormat(gguf=True)
    weights = _WEIGHTS if any(name.endswith(".safetensors") for name in names) else ""
    required = tuple(name for name in names if not (weights and name.endswith(".safetensors")))
    return ModelFormat(gguf=False, required=required, weights=weights)


def addable_backends() -> list[str]:
    """The engines with a runtime for this computer, which a custom model can use."""
    from services.components import catalog_entry_for_platform

    return [backend for backend in BACKENDS
            if catalog_entry_for_platform(runtime_id(backend, "cpu")) is not None]


def addable_gguf_backends() -> list[str]:
    """Addable engines on this computer that load ``.gguf`` files."""
    return [backend for backend in addable_backends() if model_format(backend).gguf]


@dataclass(frozen=True)
class SourceInspection:
    """Result of inspecting a candidate local speech file or folder."""

    resolved: Path
    gguf_info: object | None = None
    builtin_match: str = ""
    builtin_already_cached: bool = False
    already_registered_key: str = ""
    already_registered_backend: str = ""
    suggested_backend: str = ""
    suggested_label: str = ""
    suggested_streaming: bool | None = None
    suggested_meeting: bool | None = None
    suggested_multilingual: bool | None = None
    error: str = ""
    warning: str = ""


def _problem(backend: str, path: Path, *, read_header: bool) -> str:
    """Why ``path`` can't be ``backend``'s model, or "" when it can."""
    fmt = model_format(backend)
    engine = BACKENDS[backend]
    if fmt.gguf:
        if not path.is_file():
            return f"{engine} loads {fmt.describe()}. {path} is not one."
        if path.suffix.lower() != ".gguf":
            return f"{engine} loads {fmt.describe()}. Choose a file ending in .gguf."
        if read_header:
            from services.local_asr.gguf_meta import inspect_gguf
            info = inspect_gguf(path)
            rejection = info.rejection_reason(backend, path.name)
            if rejection:
                return rejection
        return ""
    if not path.is_dir():
        return f"{engine} loads {fmt.describe()}. {path} is not a folder."
    missing = [name for name in fmt.required if not (path / name).is_file()]
    if fmt.weights and not any(path.glob(fmt.weights)):
        missing.append(fmt.weights)
    if missing:
        return f"{path.name} is missing files {engine} needs: {', '.join(missing)}."
    return ""


def inspect_source(path: str | Path, backend: str = "", *, verify_builtin_hash: bool = False) -> SourceInspection:
    """Inspect ``path`` for GGUF metadata, built-in matches, duplicates, and capabilities.

    Never raises for invalid files; reports problems in ``error`` so dialogs can
    give live feedback before the user clicks Add.
    """
    from services.local_asr import cache
    from services.local_asr.gguf_meta import (
        inspect_gguf,
        match_builtin_gguf,
        match_builtin_gguf_by_size,
    )

    resolved = Path(path).expanduser().resolve()
    available_gguf = addable_gguf_backends()

    # Check if this exact path is already registered in custom speech models.
    existing = next((m for m in registered().values() if Path(m.path).resolve() == resolved), None)
    existing_key = existing.key if existing else ""
    existing_backend = existing.backend if existing else ""

    if backend and backend in BACKENDS and not model_format(backend).gguf:
        problem = _problem(backend, resolved, read_header=True)
        base = BUILTIN_MODELS[DEFAULT_MODELS[backend]]
        return SourceInspection(
            resolved=resolved,
            already_registered_key=existing_key,
            already_registered_backend=existing_backend,
            suggested_backend=backend,
            suggested_label=resolved.name,
            suggested_streaming=base.streaming,
            suggested_meeting=base.meeting,
            suggested_multilingual=base.languages != "English",
            error=problem,
        )

    if not resolved.is_file():
        return SourceInspection(
            resolved=resolved,
            error=f"{resolved} is not a file.",
        )
    if resolved.suffix.lower() != ".gguf":
        return SourceInspection(
            resolved=resolved,
            error="Choose a file ending in .gguf.",
        )

    info = inspect_gguf(resolved)
    if not info.valid_magic:
        return SourceInspection(
            resolved=resolved,
            gguf_info=info,
            error=f"{resolved.name} is not a GGUF model file.",
        )

    # Check if this file matches a built-in GGUF model.
    builtin_match = ""
    if match_builtin_gguf_by_size(resolved):
        matched = match_builtin_gguf(resolved, verify_hash=verify_builtin_hash)
        if matched and BUILTIN_MODELS[matched].backend in available_gguf:
            builtin_match = matched

    suggested_backend = ""
    if builtin_match:
        suggested_backend = BUILTIN_MODELS[builtin_match].backend
    elif info.suggested_backend in available_gguf:
        suggested_backend = info.suggested_backend
    elif backend in available_gguf:
        suggested_backend = backend
    elif available_gguf:
        suggested_backend = available_gguf[0]

    target_backend = backend if backend in BACKENDS else suggested_backend
    error = info.rejection_reason(target_backend, resolved.name) if target_backend else ""
    if not error and info.kind == "non_speech":
        error = info.rejection_reason("", resolved.name)

    builtin_cached = bool(builtin_match and cache.is_cached(builtin_match))
    warning = ""
    if builtin_match:
        label = BUILTIN_MODELS[builtin_match].label
        if builtin_cached:
            warning = (
                f"Matches built-in {label}, which is already installed — no need to add a duplicate."
            )
        else:
            warning = (
                f"Matches built-in {label}. Link it to reuse this file without downloading it again."
            )
    elif existing is not None:
        warning = (
            f"Already added as “{existing.label}” under {BACKENDS[existing.backend]}."
        )

    base = BUILTIN_MODELS[DEFAULT_MODELS[target_backend]] if target_backend in DEFAULT_MODELS else None
    if builtin_match:
        bm = BUILTIN_MODELS[builtin_match]
        sug_streaming: bool | None = bm.streaming
        sug_meeting: bool | None = bm.meeting
        sug_multilingual: bool | None = bm.languages != "English"
        sug_label = bm.label
    else:
        sug_streaming = info.streaming if info.streaming is not None else (base.streaming if base else False)
        sug_meeting = base.meeting if base else False
        sug_multilingual = (
            info.multilingual
            if info.multilingual is not None
            else (base.languages != "English" if base else False)
        )
        sug_label = info.name or resolved.stem

    return SourceInspection(
        resolved=resolved,
        gguf_info=info,
        builtin_match=builtin_match,
        builtin_already_cached=builtin_cached,
        already_registered_key=existing_key,
        already_registered_backend=existing_backend,
        suggested_backend=suggested_backend,
        suggested_label=sug_label,
        suggested_streaming=sug_streaming,
        suggested_meeting=sug_meeting,
        suggested_multilingual=sug_multilingual,
        error=error,
        warning=warning,
    )


def check_source(backend: str, path: str | Path) -> Path:
    """The model file or folder at ``path``, made absolute, if ``backend`` can load it.

    Raises ValueError saying what is wrong. For GGUF files, inspects the GGUF
    header metadata to reject non-speech LLM files and cross-runtime mismatches
    (e.g. NeMo-Speech.cpp vs transcribe.cpp).
    """
    if backend not in BACKENDS:
        raise ValueError("Choose a speech engine for this model.")
    resolved = Path(path).expanduser().resolve()
    problem = _problem(backend, resolved, read_header=True)
    if problem:
        raise ValueError(problem)
    return resolved


def is_present(model: SpeechModel) -> bool:
    """Whether a custom model's files are still where they were added from."""
    try:
        return not _problem(model.backend, Path(model.path), read_header=False)
    except OSError:
        return False


def size_bytes(model: SpeechModel) -> int:
    path = Path(model.path)
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.iterdir() if item.is_file())


@dataclass(frozen=True)
class RuntimeFormatOption:
    """One user-facing runtime/file format in the Add Custom Model dialog."""

    id: str
    label: str
    hint: str
    default_backend: str
    gguf: bool
    alt_backend: str = ""
    toggle_label: str = ""
    toggle_hint: str = ""
    toggle_default: bool = False
    #: True when the dialog's checkbox enables ``default_backend`` (otherwise ``alt_backend``).
    toggle_selects_default: bool = False


def runtime_format_options() -> list[RuntimeFormatOption]:
    """Deduplicated runtime formats available on this computer for custom models."""
    available = set(addable_backends())
    options: list[RuntimeFormatOption] = []
    if "parakeet_unified" in available or ("qwen_asr" in available and model_format("qwen_asr").gguf):
        has_both = "qwen_asr" in available and "parakeet_unified" in available
        options.append(RuntimeFormatOption(
            id="transcribe_gguf",
            label="transcribe.cpp GGUF",
            hint="Parakeet Unified, Qwen3-ASR, Fourier 2B (.gguf)",
            default_backend="qwen_asr" if "qwen_asr" in available else "parakeet_unified",
            alt_backend="parakeet_unified" if has_both else "",
            gguf=True,
            toggle_label="Speech-gated decoding (Silero VAD)" if has_both else "",
            toggle_hint=(
                "Recommended for Qwen3-ASR and Fourier GGUFs to prevent hallucinations during silence."
                if has_both else ""
            ),
            toggle_default=True,
            toggle_selects_default=True,
        ))
    if "parakeet_mlx" in available:
        options.append(RuntimeFormatOption(
            id="mlx_safetensors",
            label="Parakeet MLX (Safetensors)",
            hint="MLX Safetensors model folder for Apple GPU",
            default_backend="parakeet_mlx",
            gguf=False,
        ))
    if "parakeet" in available or "nemotron" in available:
        has_both = "parakeet" in available and "nemotron" in available
        options.append(RuntimeFormatOption(
            id="nemo_gguf",
            label="NeMo-Speech GGUF",
            hint="Parakeet v3, Orukeet, Nemotron (.gguf)",
            default_backend="parakeet" if "parakeet" in available else "nemotron",
            alt_backend="nemotron" if has_both else "",
            gguf=True,
            toggle_label="Cache-aware live streaming (Nemotron)" if has_both else "",
            toggle_hint=(
                "Enable for streaming Nemotron GGUFs; leave off for offline Parakeet / Orukeet models."
                if has_both else ""
            ),
            toggle_default=False,
            toggle_selects_default=False,
        ))
    if "qwen_asr" in available and not model_format("qwen_asr").gguf:
        options.append(RuntimeFormatOption(
            id="qwen_safetensors",
            label="Qwen3-ASR (Safetensors)",
            hint="PyTorch Safetensors folder (Apple GPU / CPU)",
            default_backend="qwen_asr",
            gguf=False,
        ))
    if "moonshine" in available:
        options.append(RuntimeFormatOption(
            id="moonshine_ort",
            label="Moonshine ORT",
            hint="Quantized .ort model folder (Small or Medium)",
            default_backend="moonshine",
            gguf=False,
        ))
    return options


def runtime_format_for_backend(backend: str) -> tuple[RuntimeFormatOption | None, bool]:
    """Return ``(option, toggle_checked)`` matching ``backend`` on this computer."""
    for option in runtime_format_options():
        if option.default_backend == backend:
            checked = option.toggle_selects_default if option.toggle_label else False
            return option, checked
        if option.alt_backend == backend:
            checked = (not option.toggle_selects_default) if option.toggle_label else False
            return option, checked
    return None, False


def _model(entry) -> SpeechModel:
    key, backend, path = entry["key"], entry["backend"], entry["path"]
    if not (isinstance(key, str) and key.startswith(CUSTOM_PREFIX) and backend in BACKENDS
            and isinstance(path, str) and path):
        raise ValueError("Not a custom speech model")
    arch = entry.get("arch", "") if backend == "moonshine" else ""
    if backend == "moonshine" and arch not in MOONSHINE_ARCHES:
        raise ValueError("A Moonshine model needs its size")
    label = entry.get("label")
    base = BUILTIN_MODELS[DEFAULT_MODELS[backend]]
    streaming = entry["streaming"] if isinstance(entry.get("streaming"), bool) else base.streaming
    meeting = entry["meeting"] if isinstance(entry.get("meeting"), bool) else base.meeting
    stream_final = base.stream_final and streaming
    vad_gate = entry["vad_gate"] if isinstance(entry.get("vad_gate"), bool) else base.vad_gate
    multilingual = entry["multilingual"] if isinstance(entry.get("multilingual"), bool) else None
    if multilingual is True:
        languages, language_codes = "Multilingual (custom model)", tuple(LANGUAGE_LABELS)
    elif multilingual is False:
        languages, language_codes = "English", ("en",)
    else:
        languages, language_codes = "Languages depend on the model", ()
    return replace(
        base, key=key, label=label if isinstance(label, str) and label.strip() else Path(path).stem,
        purpose="A model you added", license="See the model's card", arch=arch,
        languages=languages, language_codes=language_codes, platforms=(),
        streaming=streaming, meeting=meeting, stream_final=stream_final,
        vad_gate=vad_gate, multilingual=multilingual,
        custom=True, path=path,
    )


def parse(entries) -> dict[str, SpeechModel]:
    """The saved entries that describe a model, in order; others are skipped."""
    models: dict[str, SpeechModel] = {}
    for entry in entries if isinstance(entries, list) else ():
        try:
            model = _model(entry)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        models.setdefault(model.key, model)
    return models


_UNREAD = object()
_registered: tuple[object, dict[str, SpeechModel]] = (_UNREAD, {})


def registered() -> dict[str, SpeechModel]:
    """The custom models in the saved settings, re-read only after the file changes.

    ``catalog.MODELS`` asks for every lookup of a key it doesn't ship, so
    this is one stat of the settings file in the usual case.
    """
    global _registered
    from services.settings import SettingsKey, settings_manager

    signature = settings_manager.file_signature()
    seen, models = _registered
    if signature is None or signature != seen:
        values = settings_manager.load_settings((SettingsKey.CUSTOM_SPEECH_MODELS,))
        models = parse(SettingsKey.CUSTOM_SPEECH_MODELS.read(values))
        _registered = (signature, models)
    return models


def _new_key(label: str, taken) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")[:40].strip("-") or "model"
    key, number = CUSTOM_PREFIX + slug, 2
    while key in taken:
        key, number = f"{CUSTOM_PREFIX}{slug}-{number}", number + 1
    return key


def register(
    backend: str,
    path: str | Path,
    label: str = "",
    arch: str = "",
    *,
    streaming: bool | None = None,
    meeting: bool | None = None,
    multilingual: bool | None = None,
    vad_gate: bool | None = None,
) -> str:
    """Add a model for ``backend`` from ``path`` and return its key.

    The same file or folder added again for the same engine keeps its key,
    taking the new label, size, and capability settings.
    """
    from services.settings import SettingsKey, settings_manager

    source = str(check_source(backend, path))
    if backend == "moonshine" and arch not in MOONSHINE_ARCHES:
        raise ValueError("Choose whether this Moonshine model is Small or Medium.")
    label = label.strip() or Path(source).stem

    def add(values) -> str:
        entries = [entry for entry in SettingsKey.CUSTOM_SPEECH_MODELS.read(values)
                   if isinstance(entry, dict)]
        existing = next((entry for entry in entries if entry.get("backend") == backend
                         and entry.get("path") == source), None)
        key = existing["key"] if existing and isinstance(existing.get("key"), str) else _new_key(
            label, {entry.get("key") for entry in entries})
        entry = {"key": key, "backend": backend, "label": label, "path": source}
        if backend == "moonshine":
            entry["arch"] = arch
        if isinstance(streaming, bool):
            entry["streaming"] = streaming
        if isinstance(meeting, bool):
            entry["meeting"] = meeting
        if isinstance(multilingual, bool):
            entry["multilingual"] = multilingual
        if vad_gate is not None:
            entry["vad_gate"] = bool(vad_gate)
        values[SettingsKey.CUSTOM_SPEECH_MODELS] = (
            [entry if item is existing else item for item in entries] if existing
            else [*entries, entry])
        return key

    return settings_manager.mutate_settings(add)


def assigned(key: str, settings: dict) -> bool:
    """Whether dictation or meetings are set to load ``key``."""
    from services.settings import SettingsKey

    return (key in SettingsKey.LOCAL_ASR_MODELS.read(settings).values()
            or SettingsKey.MEETING_ASR_MODEL.read(settings) == key)


def unregister(key: str) -> None:
    """Forget a custom model. Its files are left where they are."""
    from services.settings import SettingsKey, settings_manager

    def remove(values):
        if assigned(key, values):
            raise ValueError("Choose another model before removing this one.")
        values[SettingsKey.CUSTOM_SPEECH_MODELS] = [
            entry for entry in SettingsKey.CUSTOM_SPEECH_MODELS.read(values)
            if not (isinstance(entry, dict) and entry.get("key") == key)]

    settings_manager.mutate_settings(remove)
