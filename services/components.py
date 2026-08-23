"""Secure installation of optional downloadable components.

The bundled catalog pins immutable PyPI URLs and SHA-256 digests. ``urllib`` is
used because it honors Windows proxy settings and enterprise trust roots.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, Final, Optional, Set, Tuple

from _version import __version__
from config import bundle_root, components_root, is_frozen, local_app_dir
from services.format_utils import format_size_bytes

logger = logging.getLogger(__name__)

# Bumped when the shell changes in a way that invalidates existing component
# payloads (a Python minor upgrade, a numpy major upgrade, a new MSVC runtime).
COMPONENT_API: Final[int] = 1

# Must match ``meeting.agent.base.SIDECAR_BUNDLE_NAME`` — kept as a local
# constant so this module never imports the meeting package.
_SIDECAR_BUNDLE_NAME: Final[str] = "bundle.cjs"

# The component payload, pinned at release time. Entries point straight at PyPI,
# which needs no hosting of our own and cannot rot: a published wheel's URL and
# contents are immutable, and the SHA-256 below is verified before anything is
# extracted. Regenerate with ``python scripts/build_component.py gpu-accel``.
_BUILTIN_GPU_ARCHIVES: Final[Tuple[dict, ...]] = (
    {
        "name": "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        "url": (
            "https://files.pythonhosted.org/packages/20/e2/"
            "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
            "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"
        ),
        "sha256": "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
        "size_bytes": 553_162_896,
        "extract": "nvidia-wheel",
    },
    {
        "name": "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        "url": (
            "https://files.pythonhosted.org/packages/52/de/"
            "823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/"
            "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl"
        ),
        "sha256": "72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        "size_bytes": 76_408_187,
        "extract": "nvidia-wheel",
    },
    {
        "name": "nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl",
        "url": (
            "https://files.pythonhosted.org/packages/59/df/"
            "e7c3a360be4f7b93cee39271b792669baeb3846c58a4df6dfcf187a7ffab/"
            "nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl"
        ),
        "sha256": "8e018af8fa02363876860388bd10ccb89eb9ab8fb0aa749aaf58430a9f7c4891",
        "size_bytes": 3_591_604,
        "extract": "nvidia-wheel",
    },
)

# TODO(meeting-mode): placeholder digest — replace with the real SHA-256 pinned
# at release time once the meeting-agent / speaker-id payloads are published.
_PLACEHOLDER_SHA256: Final[str] = "0" * 64

# Version of the meeting-agent payload (portable win-x64 Node LTS + the built
# Pi sidecar bundle.cjs). Versioned by its own contents, like the GPU payload.
MEETING_AGENT_COMPONENT_VERSION: Final[str] = "node22-pi1"

# Version of the speaker-id payload (WeSpeaker-family ONNX embedding model).
SPEAKER_ID_COMPONENT_VERSION: Final[str] = "wespeaker-v1"

# Official WeSpeaker ResNet34-LM ONNX (~26.5 MB). Input is Kaldi 80-dim
# fbank [1, T, 80] — the same tensor ``meeting.diarize.embedder`` builds.
# SHA-256 pins the Hub file so a silent upstream replace cannot load.
SPEAKER_MODEL_REPO: Final[str] = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
SPEAKER_MODEL_FILENAME: Final[str] = "voxceleb_resnet34_LM.onnx"
SPEAKER_MODEL_REVISION: Final[str] = "main"
SPEAKER_MODEL_SHA256: Final[str] = (
    "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
)
_SPEAKER_MODEL_ENV: Final[str] = "OPENWHISPER_SPEAKER_MODEL"

# TODO(meeting-mode): these archives are not published yet. URLs follow the
# project's release-asset naming convention
# (<component-id>-<platform>-<version>.zip under the project releases);
# replace url/sha256/size_bytes with the real pinned values produced by
# ``python scripts/build_component.py <component-id>`` before shipping.
_BUILTIN_MEETING_AGENT_ARCHIVES: Final[Tuple[dict, ...]] = (
    {
        "name": f"meeting-agent-win_amd64-{MEETING_AGENT_COMPONENT_VERSION}.zip",
        "url": (
            "https://openwhisper.fiorilabs.tech/components/"
            f"meeting-agent-win_amd64-{MEETING_AGENT_COMPONENT_VERSION}.zip"
        ),
        "sha256": _PLACEHOLDER_SHA256,
        "size_bytes": 30_000_000,
        "extract": "zip",
    },
)

_BUILTIN_SPEAKER_ID_ARCHIVES: Final[Tuple[dict, ...]] = (
    {
        "name": f"speaker-id-win_amd64-{SPEAKER_ID_COMPONENT_VERSION}.zip",
        "url": (
            "https://openwhisper.fiorilabs.tech/components/"
            f"speaker-id-win_amd64-{SPEAKER_ID_COMPONENT_VERSION}.zip"
        ),
        "sha256": _PLACEHOLDER_SHA256,
        "size_bytes": 28_000_000,
        "extract": "zip",
    },
)

# Version of the gpu-accel payload. Derived from the CUDA libraries it carries,
# NOT from the application version: the payload is unchanged by an app release,
# and an app-derived version would report "update available" after every release.
# scripts/build_component.py imports this rather than deriving its own, so the
# emitted catalog block and the constant below can never disagree — a mismatch
# would make an installed component look outdated for no reason.
GPU_COMPONENT_VERSION: Final[str] = "cuda12.9"

_BUILTIN_CATALOG: Final[dict] = {
    "schema": 1,
    "components": {
        "gpu-accel": {
            "version": GPU_COMPONENT_VERSION,
            "component_api": COMPONENT_API,
            "platform": "win_amd64",
            # Sum of the DLLs the three archives above extract (cuBLAS 12.9.2.10,
            # NVRTC 12.9.86, CUDA runtime 12.9.79). Measured, not estimated: it
            # drives the pre-install free-space check.
            "install_bytes": 959_060_480,
            "archives": _BUILTIN_GPU_ARCHIVES,
        },
        "meeting-agent": {
            "published": False,
            "version": MEETING_AGENT_COMPONENT_VERSION,
            "component_api": COMPONENT_API,
            "platform": "win_amd64",
            # TODO(meeting-mode): measure once the payload exists (portable
            # Node runtime dominates the extracted size).
            "install_bytes": 85_000_000,
            "archives": _BUILTIN_MEETING_AGENT_ARCHIVES,
        },
        "speaker-id": {
            "published": False,
            "version": SPEAKER_ID_COMPONENT_VERSION,
            "component_api": COMPONENT_API,
            "platform": "win_amd64",
            # TODO(meeting-mode): measure once the payload exists.
            "install_bytes": 30_000_000,
            "archives": _BUILTIN_SPEAKER_ID_ARCHIVES,
        },
    },
}

# CTranslate2 4.8 loads exactly one CUDA library by name (plus nvcuda.dll from
# the driver): cuBLAS. cuDNN is intentionally not listed — the ctranslate2 wheel
# bundles its own 266 KB cudnn64_9.dll stub in the package directory and calls
# os.add_dll_directory on that directory at import time, so probing for
# "cudnn64_9.dll" answers a different question depending on whether ctranslate2
# has been imported yet. It never gates real GPU capability.
_REQUIRED_GPU_DLLS: Final[Tuple[str, ...]] = (
    "cublas64_12.dll",
)

# Linux equivalents. Delivered by pip wheels (requirements-gpu.txt) rather than
# by a component, so they are only ever probed, never installed from here.
_REQUIRED_GPU_SHARED_OBJECTS: Final[Tuple[str, ...]] = (
    "libcublas.so.12",
)

_USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
_CHUNK_BYTES: Final[int] = 1 << 20
_NETWORK_TIMEOUT_S: Final[int] = 30
# Written last, so its presence means "this tree is complete".
_SENTINEL_NAME: Final[str] = ".installed"
_MANIFEST_NAME: Final[str] = "manifest.json"


class ComponentId:
    """Stable identifiers for downloadable components."""

    GPU_ACCEL: Final[str] = "gpu-accel"
    MEETING_AGENT: Final[str] = "meeting-agent"
    SPEAKER_ID: Final[str] = "speaker-id"


class ComponentState:
    """Lifecycle state of a component on this machine."""

    NOT_INSTALLED: Final[str] = "not_installed"
    EXTERNAL: Final[str] = "external"
    INSTALLED: Final[str] = "installed"
    UPDATE_AVAILABLE: Final[str] = "update_available"
    INCOMPATIBLE: Final[str] = "incompatible"
    BROKEN: Final[str] = "broken"


class InstallPhase:
    """Coarse phases reported while installing, for progress display."""

    RESOLVING: Final[str] = "resolving"
    DOWNLOADING: Final[str] = "downloading"
    VERIFYING: Final[str] = "verifying"
    EXTRACTING: Final[str] = "extracting"
    FINALIZING: Final[str] = "finalizing"


class ComponentError(Exception):
    """An install failed for a reason worth showing the user verbatim."""


class ComponentCanceled(Exception):
    """The user canceled an in-flight install."""


@dataclass(frozen=True)
class ComponentInfo:
    """Everything the UI needs to render one component row."""

    component_id: str
    display_name: str
    summary: str
    state: str
    installed_version: Optional[str]
    available_version: Optional[str]
    download_bytes: int
    install_bytes: int
    reason: str = ""

    @property
    def is_usable(self) -> bool:
        return self.state in (
            ComponentState.EXTERNAL,
            ComponentState.INSTALLED,
            ComponentState.UPDATE_AVAILABLE,
        )


# Static descriptions. The catalog supplies versions, sizes, and URLs; these
# never change between releases and should not require a network round trip.
COMPONENT_DESCRIPTIONS: Final[Dict[str, Dict[str, str]]] = {
    ComponentId.GPU_ACCEL: {
        "display_name": "GPU Acceleration",
        "summary": (
            "NVIDIA CUDA runtime (cuBLAS) for 2-4x faster local transcription. "
            "Requires an NVIDIA graphics card."
        ),
    },
    ComponentId.MEETING_AGENT: {
        "display_name": "Meeting Intelligence Agent",
        "summary": (
            "Node runtime plus the Pi agent that maintains live meeting "
            "insights (key points, decisions, action items) during Meeting "
            "Mode. Requires an OpenRouter API key."
        ),
    },
    ComponentId.SPEAKER_ID: {
        "display_name": "Speaker Identification",
        "summary": (
            "Speaker-embedding model (WeSpeaker ONNX) that separates remote "
            "voices into individual speakers during Meeting Mode."
        ),
    },
}


def available_component_ids() -> Tuple[str, ...]:
    """Components that can be installed on this platform.

    Component payloads are native Windows DLLs activated through
    ``os.add_dll_directory``, so there is nothing installable elsewhere. Linux
    GPU users get the same libraries from pip wheels instead
    (``requirements-gpu.txt``), and macOS has no CUDA backend at all. An empty
    tuple keeps the UI from offering a payload it could never activate.

    Returns:
        Installable component identifiers, in display order.
    """
    if sys.platform != "win32":
        return ()
    return tuple(
        component_id for component_id in (
            ComponentId.GPU_ACCEL,
            ComponentId.MEETING_AGENT,
            ComponentId.SPEAKER_ID,
        )
        if component_is_published(component_id)
    )


def component_is_published(component_id: str) -> bool:
    """Whether a catalog entry has production-ready immutable artifacts."""
    entry = (_BUILTIN_CATALOG.get("components") or {}).get(component_id)
    if not isinstance(entry, dict) or entry.get("published", True) is False:
        return False
    archives = entry.get("archives") or ()
    if not archives or int(entry.get("install_bytes") or 0) <= 0:
        return False
    for archive in archives:
        digest = str(archive.get("sha256") or "").lower()
        if (len(digest) != 64 or digest == _PLACEHOLDER_SHA256
                or not str(archive.get("url") or "").startswith("https://")
                or int(archive.get("size_bytes") or 0) <= 0):
            return False
    return True


def gpu_runtime_available() -> bool:
    """Return whether the CUDA libraries CTranslate2 needs can be loaded.

    This deliberately probes the native libraries rather than the managed
    component sentinel. Users may already have a working CUDA Toolkit, NVIDIA
    pip wheels, or DLLs retained from an older OpenWhisper installer.

    Returns:
        True when every library CTranslate2 loads for GPU inference resolves.
    """
    try:
        import ctypes

        if sys.platform == "win32":
            for dll_name in _REQUIRED_GPU_DLLS:
                ctypes.WinDLL(dll_name)
            return True

        if sys.platform == "linux":
            for so_name in _REQUIRED_GPU_SHARED_OBJECTS:
                ctypes.CDLL(so_name)
            return True
    except (AttributeError, OSError):
        return False

    # macOS: faster-whisper has no Metal/MPS backend.
    return False


def component_dir(component_id: str) -> str:
    return os.path.join(components_root(), component_id)


def staging_dir() -> str:
    """Scratch directory for extraction.

    Must sit on the same volume as the install directory: ``os.replace``
    raises across volumes on Windows, and ``shutil.move`` would silently fall
    back to copying gigabytes.
    """
    return os.path.join(components_root(), ".staging")


def cache_dir() -> str:
    return os.path.join(components_root(), ".cache")


def is_installed(component_id: str) -> bool:
    """True when a complete component tree is present.

    Checks the sentinel rather than the directory, so a tree left behind by an
    interrupted extract is correctly reported as missing.
    """
    return os.path.isfile(os.path.join(component_dir(component_id), _SENTINEL_NAME))


def _payload_has_sidecar_bundle(payload_dir: str) -> bool:
    return os.path.isfile(os.path.join(payload_dir, _SIDECAR_BUNDLE_NAME))


def _source_sidecar_payload_dir() -> Optional[str]:
    """Repo ``sidecar/dist`` when running from source and the bundle is built.

    Frozen installs never look here — they use the downloadable meeting-agent
    component (or nothing).
    """
    if is_frozen():
        return None
    candidate = os.path.join(bundle_root(), "sidecar", "dist")
    if _payload_has_sidecar_bundle(candidate):
        return candidate
    return None


def meeting_agent_payload_dir() -> Optional[str]:
    """Directory holding a runnable Pi sidecar payload, when available.

    Resolution order:
        1. Installed ``meeting-agent`` component tree with ``bundle.cjs``
           (usable even before the catalog marks the component published, so
           a locally staged install works in development).
        2. Source-tree ``sidecar/dist`` when ``bundle.cjs`` has been built.
        3. ``None`` — callers fall back to the direct OpenRouter agent.

    Returns:
        Absolute path to a payload directory containing ``bundle.cjs``, or
        None when no runnable sidecar is present.
    """
    if is_installed(ComponentId.MEETING_AGENT):
        installed = component_dir(ComponentId.MEETING_AGENT)
        if _payload_has_sidecar_bundle(installed):
            return installed
        logger.warning(
            "meeting-agent component is installed but missing %s",
            _SIDECAR_BUNDLE_NAME,
        )
    return _source_sidecar_payload_dir()


def _first_onnx_file(root: str) -> Optional[str]:
    if not root or not os.path.isdir(root):
        return None
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if name.lower().endswith(".onnx"):
                return os.path.join(dirpath, name)
    return None


def speaker_model_cache_dir() -> str:
    return os.path.join(local_app_dir(), "models", "speaker-id")


def _env_speaker_model_path() -> Optional[str]:
    raw = (os.environ.get(_SPEAKER_MODEL_ENV) or "").strip()
    if not raw:
        return None
    if os.path.isfile(raw):
        if not raw.lower().endswith(".onnx"):
            logger.warning(
                "%s must point at an .onnx file (got %s)", _SPEAKER_MODEL_ENV, raw
            )
            return None
        return os.path.abspath(raw)
    found = _first_onnx_file(raw)
    if found:
        return found
    logger.warning(
        "%s is set but no .onnx file was found at %s", _SPEAKER_MODEL_ENV, raw
    )
    return None


def _installed_speaker_model_path() -> Optional[str]:
    if not is_installed(ComponentId.SPEAKER_ID):
        return None
    found = _first_onnx_file(component_dir(ComponentId.SPEAKER_ID))
    if found:
        return found
    logger.warning("speaker-id component is installed but contains no .onnx model")
    return None


def _source_speaker_model_path() -> Optional[str]:
    if is_frozen():
        return None
    return _first_onnx_file(os.path.join(bundle_root(), "models", "speaker-id"))


def speaker_model_path() -> Optional[str]:
    """Path to a usable speaker-embedding ONNX model, if one is already local.

    Resolution order:
        1. ``OPENWHISPER_SPEAKER_MODEL`` (file or directory containing ``.onnx``)
        2. Installed ``speaker-id`` component (usable even while unpublished,
           matching :func:`meeting_agent_payload_dir`)
        3. Per-user cache written by :func:`ensure_speaker_model`
        4. Source-tree ``models/speaker-id`` when not frozen
        5. ``None`` — callers download via :func:`ensure_speaker_model` or
           fall back to channel-level Me/Others labels

    Returns:
        Absolute path to an ``.onnx`` file, or None when none is present.
    """
    return (
        _env_speaker_model_path()
        or _installed_speaker_model_path()
        or _first_onnx_file(speaker_model_cache_dir())
        or _source_speaker_model_path()
    )


def _speaker_model_download_allowed() -> bool:
    try:
        from services.settings import (
            HuggingFaceAccessPolicy,
            is_hf_hub_offline_env_set,
            settings_manager,
        )
    except Exception:
        return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() not in (
            "1", "true", "yes", "on",
        )
    if is_hf_hub_offline_env_set():
        return False
    return settings_manager.load_hf_access_policy() != HuggingFaceAccessPolicy.NEVER


def _verify_speaker_model(path: str) -> None:
    if not os.path.isfile(path):
        raise ComponentError("The speaker model download produced no file.")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != SPEAKER_MODEL_SHA256:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise ComponentError(
            "The speaker model failed its integrity check and was discarded."
        )


def _download_speaker_model() -> str:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ComponentError(
            "huggingface_hub is required to download the speaker model."
        ) from exc

    cache_dir = speaker_model_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    logger.info(
        "Downloading speaker embedding model %s/%s",
        SPEAKER_MODEL_REPO, SPEAKER_MODEL_FILENAME,
    )
    try:
        path = hf_hub_download(
            repo_id=SPEAKER_MODEL_REPO,
            filename=SPEAKER_MODEL_FILENAME,
            revision=SPEAKER_MODEL_REVISION,
            local_dir=cache_dir,
        )
    except Exception as exc:
        raise ComponentError(
            f"Could not download the speaker embedding model: {exc}"
        ) from exc

    resolved = os.path.abspath(path)
    _verify_speaker_model(resolved)
    logger.info("Speaker embedding model ready: %s", resolved)
    return resolved


def ensure_speaker_model() -> Optional[str]:
    """Return a local speaker-embedding model, downloading it when allowed.

    Safe to call from a worker thread (meeting start). Never raises: a
    failed or blocked download returns None so the meeting continues with
    Me/Others channel labels.

    Returns:
        Absolute path to an ONNX model, or None when none is available.
    """
    existing = speaker_model_path()
    if existing:
        return existing
    if not _speaker_model_download_allowed():
        logger.info(
            "Speaker embedding model is not installed and downloads are "
            "disabled; Meeting Mode will use Me/Others channel labels"
        )
        return None
    try:
        return _download_speaker_model()
    except Exception:
        logger.exception("Failed to download the speaker embedding model")
        return None


def read_manifest(component_id: str) -> Optional[dict]:
    path = os.path.join(component_dir(component_id), _MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def installed_size_bytes(component_id: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(component_dir(component_id)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def prune_orphans() -> None:
    """Delete staging and rollback directories left by an interrupted install.

    Safe to call at startup: neither location holds anything a completed
    install depends on.
    """
    _rmtree(staging_dir())
    root = components_root()
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        if name.endswith(".old"):
            _rmtree(os.path.join(root, name))


def _current_abi() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def check_compatibility(manifest: dict) -> Optional[str]:
    """Return why a manifest is incompatible, or None."""
    api = manifest.get("component_api")
    if api is not None and api != COMPONENT_API:
        return (
            f"Built for a different version of OpenWhisper "
            f"(component API {api}, this app uses {COMPONENT_API})"
        )

    platform_tag = manifest.get("platform")
    if platform_tag and platform_tag != "win_amd64":
        return f"Built for {platform_tag}, which this app cannot use"

    # Only components that put Python packages on sys.path carry an ABI tag.
    # A DLL-only payload such as gpu-accel omits it.
    abi = manifest.get("python_abi")
    if abi and abi != _current_abi():
        return (
            f"Built for Python {abi.replace('cp', '')[:1]}."
            f"{abi.replace('cp', '')[1:]}, this app uses "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )

    return None


# Called as (phase, done bytes, total bytes), always off the Qt thread.
ProgressCallback = Callable[[str, int, int], None]


def _open(url: str, extra_headers: Optional[Dict[str, str]] = None):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for key, value in (extra_headers or {}).items():
        request.add_header(key, value)
    return urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_S)


def _rmtree(path: str) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _describe_network_error(exc: Exception) -> str:
    """Translate a urllib failure into something a user can act on."""
    import ssl

    if isinstance(exc, ssl.SSLCertVerificationError):
        # Name the host the app actually contacts. Component payloads are
        # fetched from PyPI and nowhere else, so listing our own domains here
        # would send a blocked user to allowlist hosts that are never used.
        return (
            "The download server's certificate could not be verified. This is "
            "usually caused by network security software that inspects HTTPS "
            "traffic. Ask your IT team to allow files.pythonhosted.org."
        )
    if isinstance(exc, urllib.error.HTTPError):
        return f"The download server returned an error ({exc.code} {exc.reason})."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the download server ({exc.reason})."
    return str(exc)


def _download_verified(
    url: str,
    sha256_hex: str,
    size_bytes: int,
    destination: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    offset_base: int = 0,
    grand_total: int = 0,
) -> None:
    """Fetch ``url`` to ``destination``, resuming and verifying its hash.

    Args:
        url: Archive URL.
        sha256_hex: Expected SHA-256, lowercase hex.
        size_bytes: Expected size, used as a cheap pre-hash check.
        destination: Final path for the verified archive.
        progress: Progress sink.
        cancel: Set to abort; checked once per chunk.
        offset_base: Bytes already accounted for by earlier archives.
        grand_total: Total bytes across all archives, for overall progress.

    Raises:
        ComponentCanceled: The cancel event was set.
        ComponentError: The download was incomplete or failed verification.
    """
    part_path = destination + ".part"
    digest = hashlib.sha256()
    resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    if resume_from:
        # Re-hash what is already on disk. Without this the running digest
        # would only cover the newly fetched bytes and the final comparison
        # would be meaningless.
        with open(part_path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(block)

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
    try:
        with _open(url, headers) as response:
            if resume_from and response.status != 206:
                # The server ignored Range and is sending the whole file.
                # Restart rather than appending a duplicate prefix.
                logger.info("Server ignored Range header; restarting download")
                resume_from, digest = 0, hashlib.sha256()
                if os.path.exists(part_path):
                    os.unlink(part_path)

            mode = "ab" if resume_from else "wb"
            with open(part_path, mode) as out:
                while True:
                    if cancel.is_set():
                        raise ComponentCanceled()
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    progress(
                        InstallPhase.DOWNLOADING,
                        offset_base + out.tell(),
                        grand_total,
                    )
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, ComponentCanceled):
            raise
        raise ComponentError(_describe_network_error(exc)) from exc

    actual_size = os.path.getsize(part_path)
    if size_bytes and actual_size != size_bytes:
        raise ComponentError(
            "The download did not complete "
            f"({format_size_bytes(actual_size)} of {format_size_bytes(size_bytes)})."
        )

    progress(InstallPhase.VERIFYING, offset_base + actual_size, grand_total)
    if digest.hexdigest() != sha256_hex.lower():
        # Discard rather than keep: otherwise every retry resumes from the
        # same corrupt bytes and fails identically forever.
        os.unlink(part_path)
        raise ComponentError(
            "The download failed its integrity check and was discarded. "
            "Please try again."
        )

    os.replace(part_path, destination)


def _safe_extract(
    archive_path: str,
    target_dir: str,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> None:
    """Extract a zip, rejecting entries that escape ``target_dir``.

    Every shipped catalog entry uses the ``nvidia-wheel`` extractor, so this is
    reached only by tests today. It stays because validating costs three lines
    and removes an entire class of failure for any future plain-zip payload.
    """
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        for index, member in enumerate(members):
            if cancel.is_set():
                raise ComponentCanceled()

            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ComponentError(f"Archive contains an unsafe path: {name}")

            archive.extract(member, target_dir)
            progress(InstallPhase.EXTRACTING, index + 1, len(members))


def _safe_extract_nvidia_wheel(
    archive_path: str,
    target_dir: str,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> None:
    """Extract only native NVIDIA DLLs from an official PyPI wheel.

    The managed component uses a flat ``bin`` directory so it can be added to
    the native loader search path with one registration. The wheel's Python
    package metadata and import shims are not needed by CTranslate2.

    Args:
        archive_path: Verified NVIDIA wheel downloaded from PyPI.
        target_dir: Component staging directory.
        progress: Progress sink.
        cancel: Set to abort; checked before every member.

    Raises:
        ComponentCanceled: The cancel event was set.
        ComponentError: The wheel contains unsafe paths or no NVIDIA DLLs.
    """
    with zipfile.ZipFile(archive_path) as archive:
        dll_members = []
        for member in archive.infolist():
            name = member.filename.replace(chr(92), "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ComponentError(f"Archive contains an unsafe path: {name}")
            parts = name.split("/")
            if (
                len(parts) >= 4
                and parts[0].lower() == "nvidia"
                and parts[-2].lower() == "bin"
                and parts[-1].lower().endswith(".dll")
            ):
                dll_members.append(member)

        if not dll_members:
            raise ComponentError(
                "The NVIDIA package did not contain the expected CUDA libraries."
            )

        bin_dir = os.path.join(target_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for index, member in enumerate(dll_members):
            if cancel.is_set():
                raise ComponentCanceled()
            destination = os.path.join(
                bin_dir, member.filename.replace(chr(92), "/").split("/")[-1]
            )
            with archive.open(member) as source, open(destination, "wb") as out:
                shutil.copyfileobj(source, out)
            progress(InstallPhase.EXTRACTING, index + 1, len(dll_members))


def _validate_component_payload(component_id: str, target_dir: str) -> None:
    if component_id != ComponentId.GPU_ACCEL:
        return

    bin_dir = os.path.join(target_dir, "bin")
    try:
        names = {name.casefold() for name in os.listdir(bin_dir)}
    except OSError as exc:
        raise ComponentError("The GPU component has no library folder.") from exc

    missing = [name for name in _REQUIRED_GPU_DLLS if name.casefold() not in names]
    if missing:
        raise ComponentError(
            "The GPU component is missing required libraries: "
            + ", ".join(missing)
        )


def install_component(
    component_id: str,
    entry: dict,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> None:
    """Download, verify, and atomically install a component.

    The tree is assembled in staging and swapped in with two atomic renames,
    so an interruption leaves either the previous install or the new one
    intact — never a half-written mixture.

    """
    archives = entry.get("archives") or []
    if not archives:
        raise ComponentError("The catalog entry lists no files to download.")

    os.makedirs(cache_dir(), exist_ok=True)
    os.makedirs(staging_dir(), exist_ok=True)

    progress(InstallPhase.RESOLVING, 0, 0)

    download_total = sum(int(a.get("size_bytes", 0)) for a in archives)
    install_bytes = int(entry.get("install_bytes", 0))
    _check_free_space(download_total + install_bytes)

    archive_paths = []
    consumed = 0
    for archive in archives:
        target = os.path.join(cache_dir(), archive["name"])
        if not os.path.exists(target):
            _download_verified(
                archive["url"],
                archive["sha256"],
                int(archive.get("size_bytes", 0)),
                target,
                progress,
                cancel,
                offset_base=consumed,
                grand_total=download_total,
            )
        consumed += int(archive.get("size_bytes", 0))
        archive_paths.append(target)

    staging = os.path.join(staging_dir(), f"{component_id}.{os.getpid()}")
    _rmtree(staging)
    os.makedirs(staging, exist_ok=True)

    destination = component_dir(component_id)
    rollback = destination + ".old"
    try:
        for archive, archive_path in zip(archives, archive_paths):
            if archive.get("extract") == "nvidia-wheel":
                _safe_extract_nvidia_wheel(
                    archive_path, staging, progress, cancel
                )
            else:
                _safe_extract(archive_path, staging, progress, cancel)

        _validate_component_payload(component_id, staging)

        progress(InstallPhase.FINALIZING, 0, 0)
        with open(os.path.join(staging, _MANIFEST_NAME), "w", encoding="utf-8") as out:
            json.dump(entry, out, indent=2)
        # Sentinel last: its presence is what makes the tree count as complete.
        with open(os.path.join(staging, _SENTINEL_NAME), "w", encoding="utf-8") as out:
            out.write(str(entry.get("version", "")))

        _rmtree(rollback)
        if os.path.isdir(destination):
            os.replace(destination, rollback)
        os.replace(staging, destination)
        _rmtree(rollback)

        # Only drop the cached archives once the install is committed, so a
        # failure part-way through does not force a multi-gigabyte re-download.
        for archive_path in archive_paths:
            try:
                os.unlink(archive_path)
            except OSError:
                pass

        logger.info(f"Installed component '{component_id}' version {entry.get('version')}")
    except OSError as exc:
        raise ComponentError(_describe_disk_error(exc)) from exc
    finally:
        _rmtree(staging)


def uninstall_component(component_id: str) -> None:
    """Remove an installed component from disk."""
    target = component_dir(component_id)
    if not os.path.isdir(target):
        return
    # Rename first so the tree stops counting as installed even if deleting
    # the (very large) contents is slow or partially blocked by antivirus.
    rollback = target + ".old"
    _rmtree(rollback)
    try:
        os.replace(target, rollback)
    except OSError:
        _rmtree(target)
        return
    _rmtree(rollback)
    logger.info(f"Removed component '{component_id}'")


def _check_free_space(required_bytes: int) -> None:
    if required_bytes <= 0:
        return
    root = components_root()
    os.makedirs(root, exist_ok=True)
    free = shutil.disk_usage(root).free
    needed = int(required_bytes * 1.15)
    if free < needed:
        raise ComponentError(
            f"Not enough disk space: {format_size_bytes(needed)} needed, "
            f"{format_size_bytes(free)} free on this drive."
        )


def _describe_disk_error(exc: OSError) -> str:
    """Translate a filesystem error into something a user can act on."""
    import errno

    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
        return "The drive ran out of space while installing."
    if exc.errno == errno.EACCES or getattr(exc, "winerror", None) == 5:
        return (
            "A file could not be written, which usually means antivirus "
            "software blocked it. Try adding an exclusion for "
            f"{components_root()}."
        )
    return f"Installation failed: {exc}"


class ComponentCoordinator:
    """Owns the catalog and serializes installs.

    Thread-safe: installs run on a worker thread while dialogs run on the Qt
    main thread. ``begin_install``/``end_install`` are claim tokens matching
    :class:`services.hf_access.HuggingFaceAccessCoordinator`'s model, so a
    second request for a component already installing is refused rather than
    starting a duplicate download.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Set[str] = set()
        self._cancel_flags: Dict[str, threading.Event] = {}

    def begin_install(self, component_id: str) -> Optional[threading.Event]:
        """Claim an install and return its cancel event, or None if busy."""
        with self._lock:
            if component_id in self._active:
                logger.debug(f"Install for '{component_id}' already in flight")
                return None
            self._active.add(component_id)
            event = threading.Event()
            self._cancel_flags[component_id] = event
            return event

    def end_install(self, component_id: str) -> None:
        with self._lock:
            self._active.discard(component_id)
            self._cancel_flags.pop(component_id, None)

    def is_installing(self, component_id: str) -> bool:
        with self._lock:
            return component_id in self._active

    def cancel_install(self, component_id: str) -> None:
        with self._lock:
            event = self._cancel_flags.get(component_id)
        if event is not None:
            logger.info(f"Cancelling install of '{component_id}'")
            event.set()

    def cancel_all(self) -> None:
        """Cancel every in-flight install.

        Called before the executor is shut down: a running multi-gigabyte
        download would otherwise hold shutdown open for many minutes, because
        ``cancel_futures`` only cancels futures that have not started.
        """
        with self._lock:
            events = list(self._cancel_flags.values())
        for event in events:
            event.set()

    def fetch_catalog(self, force: bool = False) -> Optional[dict]:
        """Return the component catalog.

        The catalog ships in the application (:data:`_BUILTIN_CATALOG`) and
        needs no network access: its entries point at immutable PyPI wheel URLs
        with pinned SHA-256 digests, so there is nothing to resolve at runtime.

        An earlier design fetched a catalog from the project website and treated
        the built-in copy as a fallback. That inverted reality — the website
        serves its SPA shell for unknown paths, so the remote branch never once
        succeeded, while costing a wasted request and a warning per session. It
        also made every install look outdated, because the permanently-failed
        remote flag suppressed update detection. Regenerate the pinned entries
        with ``python scripts/build_component.py gpu-accel``.

        ``force`` remains accepted for call compatibility.
        """
        return _BUILTIN_CATALOG

    def catalog_entry(self, component_id: str) -> Optional[dict]:
        catalog = self.fetch_catalog()
        if not catalog:
            return None
        return (catalog.get("components") or {}).get(component_id)

    def describe(self, component_id: str) -> ComponentInfo:
        """Summarize a component's state for the UI.

        What is on disk decides whether a component counts as installed; the
        catalog only supplies the version and sizes to compare against. A
        component with no catalog entry is therefore still reported as installed
        rather than missing.
        """
        meta = COMPONENT_DESCRIPTIONS.get(component_id, {})
        entry = self.catalog_entry(component_id)
        available_version = entry.get("version") if entry else None
        download_bytes = (
            sum(int(a.get("size_bytes", 0)) for a in entry.get("archives", []))
            if entry else 0
        )

        installed_manifest = read_manifest(component_id)
        present = is_installed(component_id)

        if not present:
            if component_id == ComponentId.GPU_ACCEL and gpu_runtime_available():
                return ComponentInfo(
                    component_id=component_id,
                    display_name=meta.get("display_name", component_id),
                    summary=meta.get("summary", ""),
                    state=ComponentState.EXTERNAL,
                    installed_version=None,
                    available_version=available_version,
                    download_bytes=download_bytes,
                    install_bytes=0,
                    reason=(
                        "CUDA libraries are already available from your "
                        "existing setup."
                    ),
                )

            # A directory without the sentinel is a partial install.
            state = (
                ComponentState.BROKEN
                if os.path.isdir(component_dir(component_id))
                else ComponentState.NOT_INSTALLED
            )
            reason = "The previous installation did not finish." if state == ComponentState.BROKEN else ""
            return ComponentInfo(
                component_id=component_id,
                display_name=meta.get("display_name", component_id),
                summary=meta.get("summary", ""),
                state=state,
                installed_version=None,
                available_version=available_version,
                download_bytes=download_bytes,
                install_bytes=int(entry.get("install_bytes", 0)) if entry else 0,
                reason=reason,
            )

        installed_version = (installed_manifest or {}).get("version")
        incompatible = check_compatibility(installed_manifest or {})
        # The catalog is a release pin, so a version difference always means the
        # shipped payload really changed. (A previous guard suppressed this when
        # a remote catalog fetch had failed — which was always, so no install was
        # ever offered an update.)
        outdated = (
            available_version
            and installed_version
            and available_version != installed_version
        )

        if incompatible:
            state, reason = ComponentState.INCOMPATIBLE, incompatible
        elif outdated:
            # Say what the user gets, since an update is their choice: a slimmer
            # payload reclaims disk, and the row already shows the download size.
            reclaimed = installed_size_bytes(component_id) - int(
                (entry or {}).get("install_bytes", 0) or 0
            )
            state = ComponentState.UPDATE_AVAILABLE
            reason = (
                f"Update frees {format_size_bytes(reclaimed)} of disk space."
                if reclaimed > 0
                else ""
            )
        else:
            state, reason = ComponentState.INSTALLED, ""

        return ComponentInfo(
            component_id=component_id,
            display_name=meta.get("display_name", component_id),
            summary=meta.get("summary", ""),
            state=state,
            installed_version=installed_version,
            available_version=available_version,
            download_bytes=download_bytes,
            install_bytes=installed_size_bytes(component_id),
            reason=reason,
        )

    def list_components(self) -> Tuple[ComponentInfo, ...]:
        """Describe every component installable on this platform.

        Empty on platforms with no component payload, so callers can hide the
        whole section rather than render an unusable row.
        """
        return tuple(self.describe(cid) for cid in available_component_ids())


component_coordinator = ComponentCoordinator()
