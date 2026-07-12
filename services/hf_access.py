"""Hugging Face cache detection and download-consent coordination.

Implements the cache-first, consent-driven access flow: cached models always
load locally with no network activity; Hugging Face is contacted only when a
requested model is missing from the local cache AND the persisted
``HuggingFaceAccessPolicy`` (or a one-time grant) permits the download.
"""

import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, Final, Optional, Set, Tuple

from services.settings import (
    HuggingFaceAccessPolicy,
    is_hf_hub_offline_env_set,
    settings_manager,
)

logger = logging.getLogger(__name__)


# Approximate download sizes (MB) for the CTranslate2 model repositories,
# bundled so the consent dialog never contacts Hugging Face just to show an
# estimate. Keys are canonical faster-whisper model names.
MODEL_DOWNLOAD_SIZE_MB: Final[Dict[str, int]] = {
    "tiny": 76,
    "tiny.en": 76,
    "base": 145,
    "base.en": 145,
    "small": 484,
    "small.en": 484,
    "medium": 1530,
    "medium.en": 1530,
    "large-v1": 3090,
    "large-v2": 3090,
    "large-v3": 3090,
    "large": 3090,
    "large-v3-turbo": 1620,
    "turbo": 1620,
    "distil-large-v2": 1510,
    "distil-large-v3": 1510,
    "distil-large-v3.5": 1510,
    "distil-medium.en": 790,
    "distil-small.en": 330,
}


class AccessDecision:
    """Outcome of evaluating a model request against cache and policy."""
    LOAD_CACHED: Final[str] = "load_cached"
    DOWNLOAD_ALLOWED: Final[str] = "download_allowed"
    NEEDS_CONSENT: Final[str] = "needs_consent"
    BLOCKED_BY_ENV: Final[str] = "blocked_by_env"


class ConsentAction:
    """User choices returned by the Hugging Face consent dialog.

    Defined here (not in the Qt dialog) so business logic in ``services`` can
    interpret results without importing UI modules.
    """
    CANCEL: Final[str] = "cancel"
    DOWNLOAD_ONCE: Final[str] = "download_once"
    ALWAYS_ALLOW: Final[str] = "always_allow"
    OPEN_SETTINGS: Final[str] = "open_settings"


def resolve_model_repo(model_name: str) -> str:
    """Return the Hugging Face repo ID a faster-whisper model name maps to.

    Args:
        model_name: faster-whisper model name (e.g. ``"turbo"``) or a full
            repo ID / local path, which is returned unchanged.

    Returns:
        The Hugging Face repository ID for display purposes.
    """
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(model_name, model_name)
    except Exception:
        return model_name


def format_download_size(model_name: str) -> Optional[str]:
    """Return a human-readable approximate download size, if one is bundled.

    Args:
        model_name: faster-whisper model name.

    Returns:
        A string like ``"~1.5 GB"`` / ``"~145 MB"``, or None when no estimate
        is maintained for this model.
    """
    size_mb = MODEL_DOWNLOAD_SIZE_MB.get(model_name)
    if size_mb is None:
        return None
    if size_mb >= 1000:
        return f"~{size_mb / 1000:.1f} GB"
    return f"~{size_mb} MB"


def is_model_cached(model_name: str) -> bool:
    """Check whether a model is fully present in the local cache. No network.

    Uses ``local_files_only=True`` so huggingface_hub only inspects the local
    cache directory. Incomplete or corrupted cache entries (missing required
    files) raise and are treated as missing.

    Args:
        model_name: faster-whisper model name, repo ID, or local directory.

    Returns:
        True when the model can be loaded entirely from local files.
    """
    if os.path.isdir(model_name):
        return True
    try:
        from faster_whisper.utils import download_model
        download_model(model_name, local_files_only=True)
        return True
    except Exception as e:
        logger.debug(f"Model '{model_name}' not in local cache: {e}")
        return False


def download_model_files(model_name: str) -> str:
    """Download a model from Hugging Face into the local cache.

    Only call this after the access policy or an explicit user consent has
    permitted the download. Runs synchronously; callers are responsible for
    keeping it off the Qt thread.

    Args:
        model_name: faster-whisper model name or repo ID.

    Returns:
        Path to the downloaded model directory.

    Raises:
        Exception: If the download fails; the model must then be treated as
            missing (never as cached, never silently substituted).
    """
    from faster_whisper.utils import download_model

    logger.info(f"Downloading model '{model_name}' from Hugging Face...")
    path = download_model(model_name, local_files_only=False)
    logger.info(f"Model '{model_name}' downloaded to {path}")
    return path


@dataclass(frozen=True)
class CachedModelInfo:
    """Snapshot of one model repository present in the local HF cache."""
    repo_id: str
    size_bytes: int
    path: str
    revision_hashes: Tuple[str, ...]


def get_hf_cache_dir() -> str:
    """Return the Hugging Face hub cache directory the app reads models from.

    Honors ``HF_HOME`` / ``HF_HUB_CACHE`` overrides via huggingface_hub's own
    constants, falling back to the documented default location.

    Returns:
        Absolute path to the hub cache directory (may not exist yet).
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return os.path.abspath(HF_HUB_CACHE)
    except Exception:
        return os.path.abspath(
            os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
        )


def scan_cached_models() -> Dict[str, CachedModelInfo]:
    """Enumerate model repositories in the local HF cache. No network.

    Returns:
        Mapping of repo ID (e.g. ``"Systran/faster-whisper-base"``) to
        :class:`CachedModelInfo`. Empty when the cache directory does not
        exist or cannot be scanned. Callers resolve short model names to repo
        IDs with :func:`resolve_model_repo` before looking up entries; the
        reverse mapping is ambiguous (``turbo``/``large-v3-turbo`` share one
        repository).
    """
    try:
        from huggingface_hub import scan_cache_dir
        cache_info = scan_cache_dir()
    except Exception as e:
        logger.debug(f"HF cache scan unavailable: {e}")
        return {}

    cached: Dict[str, CachedModelInfo] = {}
    for repo in cache_info.repos:
        if getattr(repo, "repo_type", "model") != "model":
            continue
        cached[repo.repo_id] = CachedModelInfo(
            repo_id=repo.repo_id,
            size_bytes=repo.size_on_disk,
            path=str(repo.repo_path),
            revision_hashes=tuple(rev.commit_hash for rev in repo.revisions),
        )
    return cached


def delete_model_from_cache(model_name: str) -> None:
    """Remove all cached revisions of a model from the local HF cache.

    Uses huggingface_hub's delete strategy so snapshots, refs, and orphaned
    blobs are all cleaned up (never a manual directory removal).

    Args:
        model_name: faster-whisper model name or repo ID.

    Raises:
        ValueError: If the model is not present in the cache.
        OSError: If files cannot be removed (e.g. locked by a loaded model).
    """
    from huggingface_hub import scan_cache_dir

    repo_id = resolve_model_repo(model_name)
    info = scan_cached_models().get(repo_id)
    if info is None:
        raise ValueError(f"Model '{model_name}' ({repo_id}) is not in the local cache")

    strategy = scan_cache_dir().delete_revisions(*info.revision_hashes)
    logger.info(
        f"Deleting '{repo_id}' from HF cache "
        f"({format_size_bytes(strategy.expected_freed_size)} to free)"
    )
    strategy.execute()
    logger.info(f"Deleted '{repo_id}' from HF cache")


def format_size_bytes(size_bytes: int) -> str:
    """Return a human-readable size for an actual on-disk byte count.

    Args:
        size_bytes: Size in bytes.

    Returns:
        A string like ``"1.53 GB"`` / ``"145 MB"`` / ``"12 KB"``.
    """
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


class HuggingFaceAccessCoordinator:
    """Coordinates cache detection, policy evaluation, one-time grants, and
    download deduplication for Hugging Face model access.

    Thread-safe: model loads and consent requests can originate from worker
    threads while dialogs run on the Qt main thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._one_time_grants: Set[str] = set()
        self._active_requests: Set[str] = set()

    def get_policy(self) -> str:
        """Return the persisted access policy (with legacy migration)."""
        return settings_manager.load_hf_access_policy()

    def set_policy(self, policy: str) -> None:
        """Persist a new access policy (takes effect immediately)."""
        settings_manager.save_hf_access_policy(policy)

    def grant_once(self, model_name: str) -> None:
        """Authorize a single download of ``model_name`` without changing the
        persisted policy. Consumed by the next ``evaluate_access`` call."""
        with self._lock:
            self._one_time_grants.add(model_name)
        logger.info(f"One-time download grant issued for '{model_name}'")

    def evaluate_access(self, model_name: str, consume_grant: bool = True) -> str:
        """Evaluate a model request against cache, environment, and policy.

        Order matters: a cached model always loads locally; the external
        ``HF_HUB_OFFLINE`` env override blocks downloads even against a
        one-time grant; then grants and the persisted policy decide.

        Args:
            model_name: Resolved faster-whisper model name (not ``"auto"``).
            consume_grant: When True (default), a matching one-time grant is
                spent by this call. Pass False for advisory checks that do not
                themselves lead directly to a download.

        Returns:
            One of the ``AccessDecision`` values.
        """
        if is_model_cached(model_name):
            return AccessDecision.LOAD_CACHED

        if is_hf_hub_offline_env_set():
            return AccessDecision.BLOCKED_BY_ENV

        with self._lock:
            if model_name in self._one_time_grants:
                if consume_grant:
                    self._one_time_grants.discard(model_name)
                return AccessDecision.DOWNLOAD_ALLOWED

        if self.get_policy() == HuggingFaceAccessPolicy.ALWAYS:
            return AccessDecision.DOWNLOAD_ALLOWED

        return AccessDecision.NEEDS_CONSENT

    def begin_request(self, model_name: str) -> bool:
        """Try to claim the consent/download slot for a model.

        Deduplicates concurrent requests: while a claim is held, further
        requests for the same model are rejected so only one consent dialog
        and one download can exist per model.

        Args:
            model_name: Resolved model name being requested.

        Returns:
            True when the caller now owns the request; False when a request
            for this model is already in flight.
        """
        with self._lock:
            if model_name in self._active_requests:
                logger.debug(f"Request for '{model_name}' already in flight")
                return False
            self._active_requests.add(model_name)
            return True

    def end_request(self, model_name: str) -> None:
        """Release the consent/download slot claimed by ``begin_request``."""
        with self._lock:
            self._active_requests.discard(model_name)


# Global coordinator instance
hf_access_coordinator = HuggingFaceAccessCoordinator()
