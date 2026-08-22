"""Application update check and installer apply.

Qt-free. GitHub Releases is the only network source of truth — nothing is
fetched from the project website. ``_version.__version__`` is compared as
semver against the latest stable tag; git commit distance is ignored.

Installer copies may download the verified setup exe and relaunch Inno.
Source and git checkouts are notify-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Final, Optional, Tuple

from _version import __version__
from config import bundle_root, config, is_frozen, local_app_dir
from services.format_utils import format_size_bytes
from services.settings import (
    SettingsKey,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    resolve_update_skipped_version,
    settings_manager,
)

logger = logging.getLogger(__name__)

GITHUB_REPO: Final[str] = "Knuckles92/OpenWhisper"
LATEST_RELEASE_URL: Final[str] = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
_USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
_NETWORK_TIMEOUT_S: Final[int] = 30
_CHUNK_BYTES: Final[int] = 1 << 20
_SETUP_PREFIX: Final[str] = "OpenWhisper-Setup-"
_UPDATES_DIRNAME: Final[str] = "updates"


class InstallChannel:
    """How the running copy was installed."""

    INSTALLER: Final[str] = "installer"
    GIT: Final[str] = "git"
    SOURCE: Final[str] = "source"


class UpdateStatus:
    """Result of comparing the local version to the latest stable tag."""

    UPDATE_AVAILABLE: Final[str] = "update_available"
    UP_TO_DATE: Final[str] = "up_to_date"
    DEVELOPMENT: Final[str] = "development"


class DownloadPhase:
    """Phases reported by :func:`download_installer`."""

    DOWNLOADING: Final[str] = "downloading"
    VERIFYING: Final[str] = "verifying"


class AppUpdateError(Exception):
    """User-facing update failure."""


@dataclass(frozen=True)
class ReleaseAsset:
    """Windows setup exe attached to a GitHub release."""

    url: str
    name: str
    size_bytes: int
    sha256: Optional[str]


@dataclass(frozen=True)
class ReleaseInfo:
    """Parsed ``/releases/latest`` payload."""

    version: str
    tag_name: str
    html_url: str
    notes: str
    asset: Optional[ReleaseAsset]


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of a local-vs-latest comparison, with enough data to apply."""

    status: str
    current_version: str
    channel: str
    release: Optional[ReleaseInfo]
    can_apply: bool
    git_hint: Optional[str] = None
    git_summary: Optional[str] = None


ProgressCallback = Callable[[str, int, int], None]


def detect_channel(repo_root: Optional[str] = None) -> str:
    """Return the install channel of the running copy.

    Args:
        repo_root: Directory to inspect for a ``.git`` folder when not frozen.
            Defaults to :func:`bundle_root` (the repository root from source).

    Returns:
        One of :class:`InstallChannel`.
    """
    if is_frozen():
        return InstallChannel.INSTALLER
    root = repo_root if repo_root is not None else bundle_root()
    if os.path.isdir(os.path.join(root, ".git")):
        return InstallChannel.GIT
    return InstallChannel.SOURCE


def channel_label(channel: str) -> str:
    """Return a short human label for ``channel``."""
    if channel == InstallChannel.INSTALLER:
        return "Windows installer"
    if channel == InstallChannel.GIT:
        return "Source checkout (git)"
    return "Source copy"


def normalize_version(raw: str) -> str:
    """Strip a leading ``v`` and surrounding whitespace from a version string."""
    text = (raw or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text.strip()


def parse_version(raw: str) -> Tuple[int, int, int]:
    """Parse a version string into a ``(major, minor, patch)`` tuple.

    A leading ``v`` and any pre-release / build suffix are ignored so
    ``v2.1.1`` and ``2.1.1-rc.1`` both become ``(2, 1, 1)``. Non-numeric
    segments become ``0``. Missing segments are padded with zeros.

    Args:
        raw: Version or git tag.

    Returns:
        Three-integer tuple suitable for ordering.
    """
    text = normalize_version(raw)
    for separator in ("-", "+", " "):
        if separator in text:
            text = text.split(separator, 1)[0]
    parts = [part for part in text.split(".") if part != ""]
    numbers = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def compare_versions(current: str, latest: str) -> str:
    """Compare two version strings as ``major.minor.patch``.

    Args:
        current: Local ``_version.__version__``.
        latest: GitHub ``tag_name`` (``v`` prefix optional).

    Returns:
        :attr:`UpdateStatus.UPDATE_AVAILABLE` when ``current < latest``,
        :attr:`UpdateStatus.DEVELOPMENT` when ``current > latest``,
        otherwise :attr:`UpdateStatus.UP_TO_DATE`.
    """
    current_tuple = parse_version(current)
    latest_tuple = parse_version(latest)
    if current_tuple < latest_tuple:
        return UpdateStatus.UPDATE_AVAILABLE
    if current_tuple > latest_tuple:
        return UpdateStatus.DEVELOPMENT
    return UpdateStatus.UP_TO_DATE


def parse_last_check_at(raw: object) -> Optional[datetime]:
    """Parse a stored ``update_last_check_at`` ISO timestamp, or ``None``."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def should_auto_check(
    settings: Optional[Dict] = None,
    now: Optional[datetime] = None,
    interval_s: Optional[int] = None,
) -> bool:
    """Return whether a background GitHub check should run.

    Args:
        settings: Loaded settings dict. Loaded from disk when omitted.
        now: Clock to compare against ``update_last_check_at``.
        interval_s: Minimum seconds between automatic checks.

    Returns:
        ``False`` when the user disabled checks, when the last check is
        newer than ``interval_s``, or when ``now`` cannot be compared.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    if not resolve_update_check_enabled(settings):
        return False
    if interval_s is None:
        interval_s = config.UPDATE_CHECK_INTERVAL_S
    last = parse_last_check_at(settings.get(SettingsKey.UPDATE_LAST_CHECK_AT))
    if last is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if last > now:
        return True
    return (now - last).total_seconds() >= interval_s


def should_auto_notify(
    status: str,
    version: str,
    settings: Optional[Dict] = None,
) -> bool:
    """Return whether an automatic update-available dialog should open.

    Auto-notify is only for :attr:`UpdateStatus.UPDATE_AVAILABLE`. A skipped
    version, a disabled notify preference, or a disabled check preference
    all suppress the dialog. ``up_to_date`` and ``development`` never nag.

    Args:
        status: A :class:`UpdateStatus` value.
        version: Latest release version (``v`` prefix optional).
        settings: Loaded settings dict. Loaded from disk when omitted.
    """
    if status != UpdateStatus.UPDATE_AVAILABLE:
        return False
    if settings is None:
        settings = settings_manager.load_all_settings()
    if not resolve_update_check_enabled(settings):
        return False
    if not resolve_update_notify_enabled(settings):
        return False
    skipped = resolve_update_skipped_version(settings)
    if skipped and parse_version(skipped) == parse_version(version):
        return False
    return True


def can_apply(channel: str, release: Optional[ReleaseInfo]) -> bool:
    """Return whether this process may download and launch the setup exe."""
    if channel != InstallChannel.INSTALLER:
        return False
    if sys.platform != "win32":
        return False
    if release is None or release.asset is None:
        return False
    asset = release.asset
    return bool(asset.url and asset.sha256)


def source_update_hint(channel: str) -> Optional[str]:
    """Copy-paste commands for a source/git checkout, or ``None``."""
    if channel == InstallChannel.GIT:
        return "git pull --ff-only\npip install -r requirements.txt"
    if channel == InstallChannel.SOURCE:
        return None
    return None


def local_git_summary(repo_root: Optional[str] = None) -> Optional[str]:
    """Return ``git describe`` for a checkout, or ``None`` if unavailable."""
    if detect_channel(repo_root) != InstallChannel.GIT:
        return None
    root = repo_root if repo_root is not None else bundle_root()
    try:
        completed = subprocess.run(
            ["git", "-C", root, "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    summary = (completed.stdout or "").strip()
    return summary or None


def parse_release_payload(payload: Dict) -> ReleaseInfo:
    """Parse a GitHub ``/releases/latest`` JSON object.

    Args:
        payload: Decoded API body.

    Returns:
        Structured release metadata. ``asset.sha256`` is ``None`` when the
        setup exe is missing a ``digest`` — notify is still allowed, apply
        is not.

    Raises:
        AppUpdateError: The payload has no usable ``tag_name``.
    """
    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not normalize_version(tag_name):
        raise AppUpdateError("The update server did not report a release version.")
    version = normalize_version(tag_name)
    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"https://github.com/{GITHUB_REPO}/releases/tag/{tag_name}"
    notes = payload.get("body") if isinstance(payload.get("body"), str) else ""
    asset = _parse_setup_asset(payload.get("assets") or [])
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        html_url=html_url,
        notes=notes.strip(),
        asset=asset,
    )


def _parse_setup_asset(assets: object) -> Optional[ReleaseAsset]:
    if not isinstance(assets, list):
        return None
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if not isinstance(name, str):
            continue
        if not name.startswith(_SETUP_PREFIX) or not name.lower().endswith(".exe"):
            continue
        url = item.get("browser_download_url") or ""
        if not isinstance(url, str) or not url:
            return None
        try:
            size_bytes = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        sha256 = _digest_to_sha256(item.get("digest"))
        return ReleaseAsset(
            url=url,
            name=name,
            size_bytes=max(0, size_bytes),
            sha256=sha256,
        )
    return None


def _digest_to_sha256(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    digest = raw.strip()
    prefix = "sha256:"
    if not digest.lower().startswith(prefix):
        return None
    hex_digest = digest[len(prefix):].strip().lower()
    if len(hex_digest) != 64:
        return None
    try:
        int(hex_digest, 16)
    except ValueError:
        return None
    return hex_digest


def fetch_latest_release() -> ReleaseInfo:
    """Fetch and parse GitHub ``/releases/latest``.

    Raises:
        AppUpdateError: Network, HTTP, or payload failure.
    """
    try:
        with _open(
            LATEST_RELEASE_URL,
            extra_headers={"Accept": "application/vnd.github+json"},
        ) as response:
            raw = response.read()
    except Exception as exc:
        raise AppUpdateError(_describe_network_error(exc)) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppUpdateError(
            "The update server returned a response that could not be read."
        ) from exc
    if not isinstance(payload, dict):
        raise AppUpdateError("The update server returned a response that could not be read.")
    return parse_release_payload(payload)


def check_for_update(*, persist: bool = True) -> UpdateCheckResult:
    """Compare the running version to GitHub's latest stable release.

    Args:
        persist: When True, store ``update_last_check_at`` after a completed
            fetch (success only).

    Returns:
        Structured comparison. ``can_apply`` is True only for a frozen
        Windows install whose release asset has a SHA-256 digest.

    Raises:
        AppUpdateError: The GitHub request failed.
    """
    channel = detect_channel()
    release = fetch_latest_release()
    status = compare_versions(__version__, release.version)
    if persist:
        mark_check_completed()
    return UpdateCheckResult(
        status=status,
        current_version=__version__,
        channel=channel,
        release=release,
        can_apply=can_apply(channel, release),
        git_hint=source_update_hint(channel),
        git_summary=local_git_summary() if channel == InstallChannel.GIT else None,
    )


def mark_check_completed(now: Optional[datetime] = None) -> None:
    """Persist ``update_last_check_at`` as an ISO-8601 UTC timestamp."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        settings_manager.save_setting(
            SettingsKey.UPDATE_LAST_CHECK_AT, now.isoformat()
        )
    except Exception as exc:
        logger.warning("Could not persist update check timestamp: %s", exc)


def persist_prompt_choices(
    *,
    notify_enabled: bool,
    check_enabled: bool,
    skipped_version: Optional[str] = None,
) -> None:
    """Save first-prompt / Settings-equivalent updater preferences.

    Args:
        notify_enabled: When False, no automatic update dialog.
        check_enabled: When False, no background GitHub fetch.
        skipped_version: When set, this release is not auto-notified again.
    """
    settings = settings_manager.load_all_settings()
    settings[SettingsKey.UPDATE_NOTIFY_ENABLED] = bool(notify_enabled)
    settings[SettingsKey.UPDATE_CHECK_ENABLED] = bool(check_enabled)
    if skipped_version:
        settings[SettingsKey.UPDATE_SKIPPED_VERSION] = normalize_version(
            skipped_version
        )
    settings_manager.save_all_settings(settings)


def updates_dir() -> str:
    """Directory for verified setup downloads (under the user-data root)."""
    path = os.path.join(local_app_dir(), _UPDATES_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def download_installer(
    release: ReleaseInfo,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Download and SHA-256-verify the Windows setup exe.

    Refuses to run unless this process is a frozen Windows installer copy
    and the release asset carries a digest. Never writes a setup exe into
    a source checkout.

    Args:
        release: Parsed latest release with a setup asset.
        progress: Optional ``(phase, done_bytes, total_bytes)`` sink.
        cancel: Optional event; checked once per chunk.

    Returns:
        Absolute path of the verified setup exe.

    Raises:
        AppUpdateError: Channel, digest, hash, or network failure.
    """
    if not can_apply(detect_channel(), release):
        raise AppUpdateError(
            "This copy of OpenWhisper cannot install the Windows setup package."
        )
    asset = release.asset
    assert asset is not None and asset.sha256
    if not progress:
        progress = lambda _phase, _done, _total: None
    if cancel is None:
        cancel = threading.Event()

    destination = os.path.join(updates_dir(), os.path.basename(asset.name))
    if os.path.isfile(destination):
        existing = _file_sha256(destination)
        if existing == asset.sha256:
            progress(DownloadPhase.VERIFYING, asset.size_bytes, asset.size_bytes)
            return destination
        try:
            os.unlink(destination)
        except OSError:
            pass

    _download_verified(
        url=asset.url,
        sha256_hex=asset.sha256,
        size_bytes=asset.size_bytes,
        destination=destination,
        progress=progress,
        cancel=cancel,
    )
    return destination


def _open(url: str, extra_headers: Optional[Dict[str, str]] = None):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for key, value in (extra_headers or {}).items():
        request.add_header(key, value)
    return urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_S)


def _describe_network_error(exc: Exception) -> str:
    """Translate a urllib failure into something a user can act on."""
    import ssl

    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            "The update server's certificate could not be verified. This is "
            "usually caused by network security software that inspects HTTPS "
            "traffic. Ask your IT team to allow api.github.com and github.com."
        )
    if isinstance(exc, urllib.error.HTTPError):
        return f"The update server returned an error ({exc.code} {exc.reason})."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the update server ({exc.reason})."
    return str(exc) or "The update check failed."


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_verified(
    url: str,
    sha256_hex: str,
    size_bytes: int,
    destination: str,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> None:
    """Fetch ``url`` to ``destination``, resuming and verifying its hash."""
    part_path = destination + ".part"
    digest = hashlib.sha256()
    resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    if resume_from:
        with open(part_path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(block)

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
    try:
        with _open(url, headers) as response:
            status = getattr(response, "status", None)
            if resume_from and status != 206:
                logger.info("Server ignored Range header; restarting download")
                resume_from, digest = 0, hashlib.sha256()
                if os.path.exists(part_path):
                    os.unlink(part_path)

            mode = "ab" if resume_from else "wb"
            with open(part_path, mode) as out:
                while True:
                    if cancel.is_set():
                        raise AppUpdateError("The download was cancelled.")
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    progress(
                        DownloadPhase.DOWNLOADING,
                        out.tell(),
                        size_bytes or out.tell(),
                    )
    except AppUpdateError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise AppUpdateError(_describe_network_error(exc)) from exc

    actual_size = os.path.getsize(part_path)
    if size_bytes and actual_size != size_bytes:
        raise AppUpdateError(
            "The download did not complete "
            f"({format_size_bytes(actual_size)} of {format_size_bytes(size_bytes)})."
        )

    progress(DownloadPhase.VERIFYING, actual_size, actual_size or size_bytes)
    if digest.hexdigest() != sha256_hex.lower():
        try:
            os.unlink(part_path)
        except OSError:
            pass
        raise AppUpdateError(
            "The download failed its integrity check and was discarded. "
            "Please try again."
        )

    os.replace(part_path, destination)
