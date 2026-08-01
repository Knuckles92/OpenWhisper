"""Downloadable components: catalog, install, and removal.

The installer ships CPU transcription only. Optional payloads that would
otherwise dominate the download — currently the ~2 GB NVIDIA CUDA runtime —
are fetched on demand and unpacked under ``%LOCALAPPDATA%\\OpenWhisper\\components``.

This module deliberately mirrors :mod:`services.hf_access`: string constants
for decisions and actions live here rather than in the Qt layer, so business
logic can interpret results without importing UI modules, and a coordinator
singleton owns claim tokens so at most one install per component is in flight.

Networking uses :mod:`urllib.request` rather than ``httpx`` (which is present
transitively via ``openai``) for two Windows-specific reasons: ``urllib``
reads WinINET/registry proxy settings, and :func:`ssl.create_default_context`
loads the Windows system trust store, including the enterprise roots that
TLS-inspecting proxies depend on. ``httpx`` defaults to ``certifi`` and fails
certificate validation in exactly those environments.
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
from config import components_root
from services.format_utils import format_size_bytes

logger = logging.getLogger(__name__)

# Where the catalog lives. Kept off the archive host so a bad release can be
# corrected by editing one small JSON file. The path is versioned so a future
# application generation can publish an incompatible catalog shape.
CATALOG_URL: Final[str] = (
    "https://openwhisper.fiorilabs.tech/components/v1/index.json"
)

# Bumped when the shell changes in a way that invalidates existing component
# payloads (a Python minor upgrade, a numpy major upgrade, a new MSVC runtime).
COMPONENT_API: Final[int] = 1

_USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
_CHUNK_BYTES: Final[int] = 1 << 20  # 1 MiB: IO and hashing dominate at this size
_NETWORK_TIMEOUT_S: Final[int] = 30
# Written last, so its presence means "this tree is complete".
_SENTINEL_NAME: Final[str] = ".installed"
_MANIFEST_NAME: Final[str] = "manifest.json"


class ComponentId:
    """Stable identifiers for downloadable components."""

    GPU_ACCEL: Final[str] = "gpu-accel"


class ComponentState:
    """Lifecycle state of a component on this machine."""

    NOT_INSTALLED: Final[str] = "not_installed"
    INSTALLED: Final[str] = "installed"
    UPDATE_AVAILABLE: Final[str] = "update_available"
    INCOMPATIBLE: Final[str] = "incompatible"  # built for a different runtime
    BROKEN: Final[str] = "broken"  # partial or damaged install


class InstallPhase:
    """Coarse phases reported while installing, for progress display."""

    RESOLVING: Final[str] = "resolving"
    DOWNLOADING: Final[str] = "downloading"
    VERIFYING: Final[str] = "verifying"
    EXTRACTING: Final[str] = "extracting"
    FINALIZING: Final[str] = "finalizing"


class ComponentAction:
    """User choices returned by the component install prompt.

    Defined here rather than on the Qt dialog so ``services`` code can
    interpret a result without importing UI modules.
    """

    CANCEL: Final[str] = "cancel"
    INSTALL: Final[str] = "install"
    OPEN_COMPONENTS: Final[str] = "open_components"


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
    reason: str = ""  # why INCOMPATIBLE / BROKEN; empty otherwise

    @property
    def is_usable(self) -> bool:
        """True when the component is installed and safe to activate."""
        return self.state in (ComponentState.INSTALLED, ComponentState.UPDATE_AVAILABLE)


# Static descriptions. The catalog supplies versions, sizes, and URLs; these
# never change between releases and should not require a network round trip.
COMPONENT_DESCRIPTIONS: Final[Dict[str, Dict[str, str]]] = {
    ComponentId.GPU_ACCEL: {
        "display_name": "GPU Acceleration",
        "summary": (
            "NVIDIA CUDA runtime (cuBLAS and cuDNN) for 2-4x faster local "
            "transcription. Requires an NVIDIA graphics card."
        ),
    },
}


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def component_dir(component_id: str) -> str:
    """Installed location of ``component_id``."""
    return os.path.join(components_root(), component_id)


def staging_dir() -> str:
    """Scratch directory for extraction.

    Must sit on the same volume as the install directory: ``os.replace``
    raises across volumes on Windows, and ``shutil.move`` would silently fall
    back to copying gigabytes.
    """
    return os.path.join(components_root(), ".staging")


def cache_dir() -> str:
    """Directory holding downloaded archives and partial ``.part`` files."""
    return os.path.join(components_root(), ".cache")


def is_installed(component_id: str) -> bool:
    """True when a complete component tree is present.

    Checks the sentinel rather than the directory, so a tree left behind by an
    interrupted extract is correctly reported as missing.
    """
    return os.path.isfile(os.path.join(component_dir(component_id), _SENTINEL_NAME))


def read_manifest(component_id: str) -> Optional[dict]:
    """Read an installed component's manifest, or None when absent/unreadable."""
    path = os.path.join(component_dir(component_id), _MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def installed_size_bytes(component_id: str) -> int:
    """Total on-disk size of an installed component."""
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


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------

def _current_abi() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def check_compatibility(manifest: dict) -> Optional[str]:
    """Validate a manifest against this runtime.

    Args:
        manifest: Parsed component manifest.

    Returns:
        A human-readable reason the component cannot be used, or None when it
        is compatible.
    """
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


# --------------------------------------------------------------------------
# Download and install
# --------------------------------------------------------------------------

ProgressCallback = Callable[[str, int, int], None]
"""Called as ``(phase, done_bytes, total_bytes)``. Never from the Qt thread."""


def _open(url: str, extra_headers: Optional[Dict[str, str]] = None):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for key, value in (extra_headers or {}).items():
        request.add_header(key, value)
    return urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_S)


def _rmtree(path: str) -> None:
    """Delete a tree if present, ignoring failures."""
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _describe_network_error(exc: Exception) -> str:
    """Translate a urllib failure into something a user can act on."""
    import ssl

    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            "The download server's certificate could not be verified. This is "
            "usually caused by network security software that inspects HTTPS "
            "traffic. Ask your IT team to allow "
            "openwhisper.fiorilabs.tech and github.com."
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

    We build these archives ourselves, but validating costs three lines and
    removes an entire class of failure if a mirror is ever compromised.
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

    Args:
        component_id: Component to install.
        entry: Catalog entry describing archives, sizes, and hashes.
        progress: Progress sink.
        cancel: Set to abort.

    Raises:
        ComponentCanceled: The install was canceled.
        ComponentError: The install could not be completed.
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
        for archive_path in archive_paths:
            _safe_extract(archive_path, staging, progress, cancel)

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
    """Raise when the components volume cannot hold archive plus extracted tree."""
    if required_bytes <= 0:
        return
    root = components_root()
    os.makedirs(root, exist_ok=True)
    free = shutil.disk_usage(root).free
    needed = int(required_bytes * 1.15)  # headroom for filesystem overhead
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


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------

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
        self._catalog: Optional[dict] = None
        self._catalog_failed = False

    # -- install claims ----------------------------------------------------

    def begin_install(self, component_id: str) -> Optional[threading.Event]:
        """Claim the install slot for ``component_id``.

        Returns:
            A cancel event to pass to :func:`install_component`, or None when
            an install is already in flight.
        """
        with self._lock:
            if component_id in self._active:
                logger.debug(f"Install for '{component_id}' already in flight")
                return None
            self._active.add(component_id)
            event = threading.Event()
            self._cancel_flags[component_id] = event
            return event

    def end_install(self, component_id: str) -> None:
        """Release the install slot for ``component_id``."""
        with self._lock:
            self._active.discard(component_id)
            self._cancel_flags.pop(component_id, None)

    def is_installing(self, component_id: str) -> bool:
        with self._lock:
            return component_id in self._active

    def cancel_install(self, component_id: str) -> None:
        """Request cancellation of an in-flight install."""
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

    # -- catalog -----------------------------------------------------------

    def fetch_catalog(self, force: bool = False) -> Optional[dict]:
        """Return the component catalog, fetching it at most once per session.

        Returns None when the catalog is unreachable. Callers must treat that
        as "no update information available", never as "nothing is installed".
        """
        with self._lock:
            if self._catalog is not None and not force:
                return self._catalog
            if self._catalog_failed and not force:
                return None

        try:
            with _open(CATALOG_URL) as response:
                catalog = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            logger.warning(f"Could not fetch the component catalog: {exc}")
            with self._lock:
                self._catalog_failed = True
            return None

        with self._lock:
            self._catalog = catalog
            self._catalog_failed = False
        return catalog

    def catalog_entry(self, component_id: str) -> Optional[dict]:
        """Catalog entry for ``component_id``, or None when unavailable."""
        catalog = self.fetch_catalog()
        if not catalog:
            return None
        return (catalog.get("components") or {}).get(component_id)

    # -- state -------------------------------------------------------------

    def describe(self, component_id: str) -> ComponentInfo:
        """Summarize a component's state for the UI.

        The installed manifest is consulted first, so a failed catalog fetch
        never makes an installed component look missing.
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
        if incompatible:
            state, reason = ComponentState.INCOMPATIBLE, incompatible
        elif available_version and installed_version and available_version != installed_version:
            state, reason = ComponentState.UPDATE_AVAILABLE, ""
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
        """Describe every known component."""
        return tuple(self.describe(cid) for cid in COMPONENT_DESCRIPTIONS)


component_coordinator = ComponentCoordinator()
