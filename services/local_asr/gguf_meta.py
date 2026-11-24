"""Lightweight GGUF header reader and speech-model compatibility detector.

Reads only the GGUF v2/v3 metadata table (never weights) so OpenWhisper can:
- reject non-speech LLM/embedding GGUF files before adding them,
- detect whether a GGUF targets NVIDIA NeMo-Speech.cpp or transcribe.cpp,
- auto-suggest the right local engine, label, and capabilities, and
- match an existing GGUF file against built-in catalog artifacts by size + SHA-256.
"""
from __future__ import annotations

import functools
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from services.local_asr.catalog import BACKENDS, BUILTIN_MODELS, _artifacts

_GGUF_MAGIC = b"GGUF"
_SUPPORTED_VERSIONS = (2, 3)

# GGUF scalar value type sizes in bytes (GGUF spec types 0..12).
_SCALAR_SIZES = {
    0: 1,   # UINT8
    1: 1,   # INT8
    2: 2,   # UINT16
    3: 2,   # INT16
    4: 4,   # UINT32
    5: 4,   # INT32
    6: 4,   # FLOAT32
    7: 1,   # BOOL
    10: 8,  # UINT64
    11: 8,  # INT64
    12: 8,  # FLOAT64
}
_SCALAR_FORMATS = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

# Hard bounds so a malformed or hostile file can never hang or allocate memory.
_MAX_KV_ENTRIES = 512
_MAX_KEY_BYTES = 1024
_MAX_STRING_BYTES = 4096
_MAX_SKIP_ARRAY_STRINGS = 100_000

# Well-known non-speech text / embedding / vision GGUF architectures.
_NON_SPEECH_ARCHITECTURES = frozenset({
    "llama", "mistral", "mixtral", "gemma", "gemma2", "gemma3", "phi2", "phi3",
    "deepseek", "deepseek2", "falcon", "mpt", "gpt2", "gptneox", "bloom",
    "starcoder", "starcoder2", "command-r", "cohere2", "internlm2", "olmo",
    "olmo2", "baichuan", "chatglm", "bert", "nomic-bert", "jina-bert", "clip",
    "qwen2", "qwen2moe", "qwen3moe", "smollm", "granite", "rwkv", "mamba",
    "jamba", "exaone", "minicpm", "llava", "deepseek3", "qwen2vl", "qwen25vl",
    "pixtral", "siglip", "t5", "t5encoder",
})

# Metadata key prefixes that indicate an audio / speech recognition GGUF.
_SPEECH_KEY_PREFIXES = (
    "asr.", "nemo.", "parakeet.", "nemotron.", "transcribe.", "audio.",
    "whisper.", "qwen3_asr.", "qwen_asr.", "mel.", "clip.audio.",
)

_NEMO_BACKENDS = frozenset({"parakeet", "nemotron"})
_TRANSCRIBE_BACKENDS = frozenset({"parakeet_unified", "qwen_asr"})


@dataclass(frozen=True)
class GgufInfo:
    """Summary of what a GGUF file's header says about the model inside."""

    valid_magic: bool
    version: int = 0
    architecture: str = ""
    name: str = ""
    prefixes: tuple[str, ...] = ()
    #: "speech", "non_speech", or "unknown" (e.g. truncated/minimal header).
    kind: str = "unknown"
    #: "nemo", "transcribe_cpp", or "" when unknown/ambiguous.
    runtime: str = ""
    #: Best-guess OpenWhisper backend ID ("parakeet", "nemotron",
    #: "parakeet_unified", "qwen_asr"), or "" when unknown.
    suggested_backend: str = ""
    streaming: bool | None = None
    multilingual: bool | None = None

    def rejection_reason(self, backend: str, filename: str = "This file") -> str:
        """Why ``backend`` cannot load this GGUF, or "" when compatible/unknown."""
        if not self.valid_magic:
            return f"{filename} is not a GGUF model file."
        if self.kind == "non_speech":
            arch = f" ({self.architecture})" if self.architecture else ""
            return f"{filename} is a text/LLM model{arch}, not a speech recognition model."
        if backend not in BACKENDS:
            return ""
        engine = BACKENDS[backend]
        if self.runtime == "nemo" and backend in _TRANSCRIBE_BACKENDS:
            target = BACKENDS.get(self.suggested_backend, "Parakeet")
            alt = f"{target} or Nemotron Streaming" if self.suggested_backend == "parakeet" else target
            return (
                f"{filename} is built for NVIDIA Speech (NeMo-Speech.cpp), not {engine}. "
                f"Add it under {alt} instead."
            )
        if self.runtime == "transcribe_cpp" and backend in _NEMO_BACKENDS:
            target = BACKENDS.get(self.suggested_backend, "Parakeet Unified")
            return (
                f"{filename} is built for transcribe.cpp, not {engine}. "
                f"Add it under {target} instead."
            )
        if self.runtime == "transcribe_cpp" and self.suggested_backend:
            if self.suggested_backend == "qwen_asr" and backend == "parakeet_unified":
                return (
                    f"{filename} is a Qwen3-ASR model and should be added under "
                    f"{BACKENDS['qwen_asr']} instead of {engine}."
                )
            if self.suggested_backend == "parakeet_unified" and backend == "qwen_asr":
                return (
                    f"{filename} is a Parakeet model and should be added under "
                    f"{BACKENDS['parakeet_unified']} instead of {engine}."
                )
        return ""

    def describe_detected(self) -> str:
        """Short user-facing summary of detected metadata, or ""."""
        if self.kind == "non_speech":
            return f"Not a speech model ({self.architecture or 'LLM'})"
        parts = []
        if self.suggested_backend in BACKENDS:
            parts.append(BACKENDS[self.suggested_backend])
        if self.runtime == "nemo":
            parts.append("NeMo-Speech.cpp")
        elif self.runtime == "transcribe_cpp":
            parts.append("transcribe.cpp")
        if self.architecture:
            parts.append(f"arch: {self.architecture}")
        return " · ".join(parts)


def _read_exact(stream: BinaryIO, count: int) -> bytes | None:
    data = stream.read(count)
    return data if len(data) == count else None


def _read_string(stream: BinaryIO, *, keep: bool = True) -> str | None:
    raw_len = _read_exact(stream, 8)
    if raw_len is None:
        return None
    (length,) = struct.unpack("<Q", raw_len)
    if length == 0:
        return ""
    if not keep or length > _MAX_STRING_BYTES:
        # Advance past oversized strings without allocating them.
        pos = stream.tell()
        stream.seek(0, 2)
        end = stream.tell()
        if pos + length > end:
            return None
        stream.seek(pos + length)
        return ""
    raw = _read_exact(stream, length)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _skip_or_read_value(stream: BinaryIO, value_type: int, *, keep: bool) -> tuple[bool, Any]:
    if value_type in _SCALAR_SIZES:
        size = _SCALAR_SIZES[value_type]
        raw = _read_exact(stream, size)
        if raw is None:
            return False, None
        if not keep:
            return True, None
        return True, struct.unpack(_SCALAR_FORMATS[value_type], raw)[0]
    if value_type == _TYPE_STRING:
        text = _read_string(stream, keep=keep)
        return (text is not None), text
    if value_type == _TYPE_ARRAY:
        header = _read_exact(stream, 12)
        if header is None:
            return False, None
        elem_type, count = struct.unpack("<IQ", header)
        if elem_type in _SCALAR_SIZES:
            byte_len = _SCALAR_SIZES[elem_type] * count
            pos = stream.tell()
            stream.seek(0, 2)
            end = stream.tell()
            if pos + byte_len > end:
                return False, None
            stream.seek(pos + byte_len)
            return True, None
        if elem_type == _TYPE_STRING:
            if count > _MAX_SKIP_ARRAY_STRINGS:
                return False, None
            for _ in range(count):
                if _read_string(stream, keep=False) is None:
                    return False, None
            return True, None
        return False, None
    return False, None


def read_gguf_metadata(path: Path) -> tuple[bool, int, dict[str, Any], set[str]]:
    """Return ``(valid_magic, version, selected_kv, prefixes)`` for ``path``."""
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
            if magic != _GGUF_MAGIC:
                return False, 0, {}, set()
            header = _read_exact(stream, 20)
            if header is None:
                return True, 0, {}, set()
            version, _tensor_count, kv_count = struct.unpack("<IQQ", header)
            if version not in _SUPPORTED_VERSIONS:
                return True, version, {}, set()
            kv: dict[str, Any] = {}
            prefixes: set[str] = set()
            for _ in range(min(kv_count, _MAX_KV_ENTRIES)):
                raw_klen = _read_exact(stream, 8)
                if raw_klen is None:
                    break
                (klen,) = struct.unpack("<Q", raw_klen)
                if klen == 0 or klen > _MAX_KEY_BYTES:
                    break
                raw_key = _read_exact(stream, klen)
                if raw_key is None:
                    break
                key = raw_key.decode("utf-8", errors="replace")
                raw_type = _read_exact(stream, 4)
                if raw_type is None:
                    break
                (vtype,) = struct.unpack("<I", raw_type)
                prefix = key.split(".", 1)[0] + "." if "." in key else ""
                if prefix:
                    prefixes.add(prefix)
                keep = key in (
                    "general.architecture",
                    "general.name",
                    "general.description",
                    "asr.head_type",
                    "asr.model_type",
                    "asr.streaming",
                    "asr.att_context_style",
                    "asr.language",
                    "asr.languages",
                    "parakeet.arch",
                    "parakeet.streaming",
                    "nemotron.att_context_style",
                    "transcribe.family",
                    "transcribe.arch",
                    "transcribe.model_type",
                    "transcribe.streaming",
                )
                ok, value = _skip_or_read_value(stream, vtype, keep=keep)
                if not ok:
                    break
                if keep and value is not None:
                    kv[key] = value
            return True, version, kv, prefixes
    except OSError:
        return False, 0, {}, set()


def _name_suggests_english_only(text: str) -> bool | None:
    lowered = text.lower()
    if any(token in lowered for token in ("multilingual", "v3", "orukeet", "qwen")):
        return False
    if any(token in lowered for token in ("-en-", "_en_", "-en.", "_en.", "english", "unified-en", "110m-en")):
        return True
    if lowered.endswith(("-en", "_en")):
        return True
    return None


def suggest_backend_from_filename(filename: str) -> str:
    """Gentle filename heuristic used only as a UI default when metadata is absent."""
    lowered = filename.lower()
    if "qwen" in lowered:
        return "qwen_asr"
    if "nemotron" in lowered:
        return "nemotron"
    if "unified" in lowered or "110m" in lowered or "tdt_ctc" in lowered or "tdt-ctc" in lowered:
        return "parakeet_unified"
    if "parakeet" in lowered or "orukeet" in lowered:
        return "parakeet"
    return ""


def inspect_gguf(path: str | Path) -> GgufInfo:
    """Inspect a GGUF file's header and classify its speech-engine compatibility."""
    file_path = Path(path)
    valid, version, kv, prefixes = read_gguf_metadata(file_path)
    if not valid:
        return GgufInfo(valid_magic=False)

    arch = str(kv.get("general.architecture") or "").strip().lower()
    name = str(kv.get("general.name") or "").strip()
    transcribe_family = str(kv.get("transcribe.family") or kv.get("transcribe.arch") or "").strip().lower()
    combined_name = f"{name} {file_path.name}".lower()
    has_speech_keys = any(p in prefixes for p in _SPEECH_KEY_PREFIXES)
    sorted_prefixes = tuple(sorted(prefixes))

    # 1. Explicit non-speech LLM / vision / embedding GGUF check.
    if arch in _NON_SPEECH_ARCHITECTURES and not has_speech_keys:
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="non_speech",
        )
    if arch == "qwen3" and not has_speech_keys and "qwen3." in prefixes:
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="non_speech",
        )

    # 2. Qwen3-ASR (transcribe.cpp)
    if (
        arch in ("qwen3-asr", "qwen3_asr", "qwen_asr", "qwen3asr")
        or "qwen3_asr." in prefixes
        or "qwen_asr." in prefixes
        or ("qwen" in transcribe_family)
        or (arch.startswith("qwen") and has_speech_keys)
    ):
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="speech", runtime="transcribe_cpp",
            suggested_backend="qwen_asr", streaming=False, multilingual=True,
        )

    # 3. Nemotron Streaming (NeMo-Speech.cpp)
    if (
        arch.startswith("nemotron")
        or "nemotron." in prefixes
        or (
            (arch == "asr" or "asr." in prefixes or "nemo." in prefixes)
            and ("nemotron" in combined_name or "asr.att_context_style" in kv)
        )
    ):
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="speech", runtime="nemo",
            suggested_backend="nemotron", streaming=True, multilingual=True,
        )

    # 4. NeMo-Speech.cpp Parakeet / generic NeMo ASR
    if arch == "asr" or "asr." in prefixes or "nemo." in prefixes:
        streaming = bool(kv.get("asr.streaming")) if "asr.streaming" in kv else (
            True if "streaming" in combined_name else False
        )
        en_only = _name_suggests_english_only(combined_name)
        multilingual = (not en_only) if en_only is not None else True
        backend = "nemotron" if streaming else "parakeet"
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="speech", runtime="nemo",
            suggested_backend=backend, streaming=streaming, multilingual=multilingual,
        )

    # 5. transcribe.cpp Parakeet / generic transcribe.cpp GGUF
    parakeet_arches = (
        "parakeet", "parakeet-unified", "parakeet_unified",
        "parakeet-tdt", "parakeet_tdt", "parakeet-ctc", "parakeet_ctc",
        "parakeet-tdt-ctc", "parakeet_tdt_ctc",
    )
    if "transcribe." in prefixes or arch in parakeet_arches or "parakeet." in prefixes:
        is_transcribe = (
            "transcribe." in prefixes
            or "parakeet.arch" in kv
            or "unified" in combined_name
            or "tdt_ctc" in combined_name
            or "tdt-ctc" in combined_name
            or "110m" in combined_name
        )
        streaming_val = kv.get("transcribe.streaming", kv.get("parakeet.streaming"))
        if isinstance(streaming_val, bool):
            streaming: bool | None = streaming_val
        elif "unified" in combined_name or "streaming" in combined_name:
            streaming = True
        elif "110m" in combined_name or "tdt_ctc" in combined_name or "tdt-ctc" in combined_name:
            streaming = False
        else:
            streaming = None
        en_only = _name_suggests_english_only(combined_name)
        multilingual = (not en_only) if en_only is not None else None
        if is_transcribe:
            return GgufInfo(
                valid_magic=True, version=version, architecture=arch, name=name,
                prefixes=sorted_prefixes, kind="speech", runtime="transcribe_cpp",
                suggested_backend="parakeet_unified", streaming=streaming,
                multilingual=multilingual,
            )
        # Bare `parakeet` architecture without runtime-specific markers: treat as
        # speech and suggest parakeet, but leave runtime="" so either engine works.
        return GgufInfo(
            valid_magic=True, version=version, architecture=arch, name=name,
            prefixes=sorted_prefixes, kind="speech", runtime="",
            suggested_backend="parakeet", streaming=streaming, multilingual=multilingual,
        )

    # 6. Unknown / minimal header (e.g. test stubs or novel speech model).
    hint = suggest_backend_from_filename(file_path.name)
    return GgufInfo(
        valid_magic=True, version=version, architecture=arch, name=name,
        prefixes=sorted_prefixes, kind="unknown", runtime="",
        suggested_backend=hint,
    )


@functools.cache
def _builtin_gguf_specs() -> dict[int, tuple[tuple[str, dict], ...]]:
    """Map exact file size -> ``((model_key, file_spec), ...)`` for all built-in GGUFs."""
    by_size: dict[int, list[tuple[str, dict]]] = {}
    for filename in ("models.json", "qwen_gguf_models.json"):
        data = _artifacts(filename)
        for key, spec in data.items():
            if key not in BUILTIN_MODELS:
                continue
            for file_spec in spec.get("files", ()):
                if file_spec["name"].endswith(".gguf"):
                    by_size.setdefault(int(file_spec["size_bytes"]), []).append((key, file_spec))
    return {size: tuple(entries) for size, entries in by_size.items()}


def match_builtin_gguf_by_size(path: str | Path) -> list[str]:
    """Fast O(1) candidate check: built-in model keys whose GGUF has this exact byte size."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return []
    return [key for key, _spec in _builtin_gguf_specs().get(size, ())]


def file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of ``path`` in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def match_builtin_gguf(path: str | Path, *, verify_hash: bool = True) -> str | None:
    """Return the built-in speech model key if ``path`` matches its pinned GGUF."""
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError:
        return None
    candidates = _builtin_gguf_specs().get(size)
    if not candidates:
        return None
    if not verify_hash:
        return candidates[0][0]
    try:
        digest = file_sha256(file_path)
    except OSError:
        return None
    for key, file_spec in candidates:
        if file_spec["sha256"].lower() == digest.lower():
            return key
    return None
