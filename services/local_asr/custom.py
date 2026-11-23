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
            with path.open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    return f"{path.name} is not a GGUF model file."
        return ""
    if not path.is_dir():
        return f"{engine} loads {fmt.describe()}. {path} is not a folder."
    missing = [name for name in fmt.required if not (path / name).is_file()]
    if fmt.weights and not any(path.glob(fmt.weights)):
        missing.append(fmt.weights)
    if missing:
        return f"{path.name} is missing files {engine} needs: {', '.join(missing)}."
    return ""


def check_source(backend: str, path: str | Path) -> Path:
    """The model file or folder at ``path``, made absolute, if ``backend`` can load it.

    Raises ValueError saying what is wrong. Only the format is checked: a
    file of the right kind for another model of the same format still
    fails when the engine loads it, with the engine's own message.
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
    vad_gate = entry["vad_gate"] if isinstance(entry.get("vad_gate"), bool) else base.vad_gate
    return replace(
        base, key=key, label=label if isinstance(label, str) and label.strip() else Path(path).stem,
        purpose="A model you added", license="See the model's card", arch=arch,
        languages="Languages depend on the model", language_codes=(),
        platforms=(), vad_gate=vad_gate,
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
    vad_gate: bool | None = None,
) -> str:
    """Add a model for ``backend`` from ``path`` and return its key.

    The same file or folder added again for the same engine keeps its key,
    taking the new label and size.
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
