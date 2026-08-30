"""Application update checks and verified platform handoff.

Qt-free. GitHub Releases is the production network source of truth — nothing
is fetched from the project website. ``_version.__version__`` is compared as
semver against the latest stable tag; git commit distance is ignored.

Validated HKCU Windows installs may hand a verified ``tar.xz`` to
``OpenWhisperUpdater.exe``; other Windows installs use the setup exe. Native
Linux package installs, source copies, and git checkouts are notify-only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Final, List, Optional, Tuple

from _version import __version__
from config import bundle_root, config, is_frozen, local_app_dir
from services.app_update_apply import (
    InstallRegistration,
    UpdateApplyError,
    UpdateCanceled,
    discover_install_registration,
    prepare_candidate,
    resolve_apply_mode,
    running_app_dir,
)
from services.format_utils import format_size_bytes
from services.settings import (
    SettingsKey,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    resolve_update_skipped_version,
    settings_manager,
)
from services.update_contract import (
    ApplyMode,
    archive_asset_name,
    encode_native_result,
    setup_asset_name,
    updates_root,
)

logger = logging.getLogger(__name__)

GITHUB_REPO: Final[str] = "Knuckles92/OpenWhisper"
LATEST_RELEASE_URL: Final[str] = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
RELEASES_PAGE_URL: Final[str] = f"https://github.com/{GITHUB_REPO}/releases"
UPDATE_FEED_ENV: Final[str] = "OPENWHISPER_UPDATE_FEED_URL"
_RATE_LIMIT_STATUS: Final[str] = (
    "Could not check for updates (GitHub rate limit)."
)
_RATE_LIMIT_MESSAGE: Final[str] = (
    "GitHub rate-limited this update check. Try again later, or open "
    f"{RELEASES_PAGE_URL}."
)
_USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
_NETWORK_TIMEOUT_S: Final[int] = 30
_CHUNK_BYTES: Final[int] = 1 << 20
_MAX_ASSET_BYTES: Final[int] = 2 * 1024 * 1024 * 1024
_DOWNLOAD_KEEP_WINDOW_S: Final[int] = 30
_MAX_REDIRECTS: Final[int] = 5
_GITHUB_RELEASE_ASSET_HOSTS: Final[Tuple[str, ...]] = (
    "release-assets.githubusercontent.com",
)


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
    """Phases reported by download / prepare, plus the two commit phases."""

    DOWNLOADING: Final[str] = "downloading"
    VERIFYING: Final[str] = "verifying"
    EXTRACTING: Final[str] = "extracting"
    OPENING: Final[str] = "opening"
    RESTARTING: Final[str] = "restarting"
    ROLLING_BACK: Final[str] = "rolling_back"


class AppUpdateError(Exception):
    """User-facing update failure."""


@dataclass(frozen=True)
class ReleaseAsset:
    """A recognized update asset attached to a GitHub release."""

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
    setup_asset: Optional[ReleaseAsset] = None
    native_asset: Optional[ReleaseAsset] = None

    @property
    def asset(self) -> Optional[ReleaseAsset]:
        """Preferred download for size display: native archive, else setup."""
        return self.native_asset or self.setup_asset


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of a local-vs-latest comparison, with enough data to apply."""

    status: str
    current_version: str
    channel: str
    release: Optional[ReleaseInfo]
    can_apply: bool
    apply_mode: str = ApplyMode.NOTIFY_ONLY
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


def channel_label(channel: str, platform_name: Optional[str] = None) -> str:
    """Return a short human label for ``channel`` on the current platform."""
    if channel == InstallChannel.INSTALLER:
        platform_id = platform_name if platform_name is not None else sys.platform
        if platform_id.startswith("linux"):
            return "Linux package"
        if platform_id == "darwin":
            return "macOS application"
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


def _asset_ready(asset: Optional[ReleaseAsset]) -> bool:
    return bool(asset and asset.url and asset.sha256 and asset.size_bytes > 0)


def resolve_release_apply_mode(
    channel: str,
    release: Optional[ReleaseInfo],
    *,
    registration: Optional[InstallRegistration] = None,
    app_dir: Optional[str] = None,
    helper_present: Optional[bool] = None,
    platform_name: Optional[str] = None,
) -> str:
    """Return :class:`ApplyMode` for this channel and release."""
    if channel != InstallChannel.INSTALLER:
        return ApplyMode.NOTIFY_ONLY
    platform_id = platform_name if platform_name is not None else sys.platform
    if platform_id.startswith("linux"):
        # The system package manager owns /usr/lib/openwhisper. The
        # application may notify users of a release, but it must never
        # bypass that ownership.
        return ApplyMode.NOTIFY_ONLY
    native_ready = _asset_ready(release.native_asset if release else None)
    setup_ready = _asset_ready(release.setup_asset if release else None)
    return resolve_apply_mode(
        frozen=True,
        platform_name=platform_id,
        native_ready=native_ready,
        setup_ready=setup_ready,
        registration=registration,
        app_dir=app_dir,
        helper_present=helper_present,
    )


def can_apply(
    channel: str,
    release: Optional[ReleaseInfo],
    *,
    registration: Optional[InstallRegistration] = None,
    app_dir: Optional[str] = None,
    helper_present: Optional[bool] = None,
    platform_name: Optional[str] = None,
) -> bool:
    """Return whether this process may download a verified apply payload."""
    mode = resolve_release_apply_mode(
        channel,
        release,
        registration=registration,
        app_dir=app_dir,
        helper_present=helper_present,
        platform_name=platform_name,
    )
    return mode in (ApplyMode.NATIVE, ApplyMode.SETUP)


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
    assets = payload.get("assets") or []
    stable = not bool(payload.get("draft")) and not bool(payload.get("prerelease"))
    setup_asset = None
    native_asset = None
    if stable:
        setup_asset = _parse_named_asset(
            assets,
            expected_name=setup_asset_name(version),
            tag_name=tag_name,
        )
        native_asset = _parse_named_asset(
            assets,
            expected_name=archive_asset_name(version),
            tag_name=tag_name,
        )
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        html_url=html_url,
        notes=notes.strip(),
        setup_asset=setup_asset,
        native_asset=native_asset,
    )


def _expected_asset_url(tag_name: str, filename: str) -> str:
    return (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{tag_name}/{filename}"
    )


def _parse_named_asset(
    assets: object,
    *,
    expected_name: str,
    tag_name: str,
) -> Optional[ReleaseAsset]:
    if not isinstance(assets, list):
        return None
    matches: list = []
    expected_url = _expected_asset_url(tag_name, expected_name)
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if not isinstance(name, str) or name != expected_name:
            continue
        state = item.get("state")
        if state not in (None, "uploaded"):
            continue
        url = item.get("browser_download_url") or ""
        if not isinstance(url, str) or not _asset_url_allowed(url, expected_url):
            continue
        try:
            size_bytes = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes <= 0 or size_bytes > _MAX_ASSET_BYTES:
            continue
        matches.append(
            ReleaseAsset(
                url=url,
                name=name,
                size_bytes=size_bytes,
                sha256=_digest_to_sha256(item.get("digest")),
            )
        )
    if len(matches) != 1:
        return None
    return matches[0]


def _asset_url_allowed(url: str, expected_url: str) -> bool:
    """Accept GitHub's exact download URL, or the same file on the feed's origin."""
    if url == expected_url:
        return True
    feed = update_feed_override()
    if not feed:
        return False
    same_file = url.rsplit("/", 1)[-1] == expected_url.rsplit("/", 1)[-1]
    return same_file and _url_origin(url) == _url_origin(feed)


def _url_origin(url: str) -> Tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


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


def _is_loopback_feed_url(url: str) -> bool:
    """Return whether ``url`` is the exact local soak-feed endpoint."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        address = ipaddress.ip_address(host)
        port = parts.port
    except (ValueError, TypeError):
        return False
    return bool(
        parts.scheme.lower() in ("http", "https")
        and address.is_loopback
        and port is not None
        and parts.username is None
        and parts.password is None
        and parts.path == "/releases/latest"
        and not parts.query
        and not parts.fragment
    )


def update_feed_override() -> Optional[str]:
    """Return a loopback-only stand-in named by ``OPENWHISPER_UPDATE_FEED_URL``.

    Production metadata stays pinned to GitHub. The override exists only for
    local pre-release soaking via :mod:`scripts.serve_update_feed`; accepting
    an arbitrary remote feed would let that feed supply both a package and its
    supposedly trusted digest.
    """
    value = os.environ.get(UPDATE_FEED_ENV, "").strip()
    if not value:
        return None
    if _is_loopback_feed_url(value):
        return value
    logger.warning("Ignoring unsafe %s value", UPDATE_FEED_ENV)
    return None


def latest_release_url() -> str:
    override = update_feed_override()
    if override:
        logger.warning("Update feed overridden by %s: %s", UPDATE_FEED_ENV, override)
        return override
    return LATEST_RELEASE_URL


def fetch_latest_release() -> ReleaseInfo:
    """Fetch and parse GitHub ``/releases/latest`` (or its configured stand-in).

    Raises:
        AppUpdateError: Network, HTTP, or payload failure.
    """
    try:
        with _open(
            latest_release_url(),
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
    apply_mode = resolve_release_apply_mode(channel, release)
    return UpdateCheckResult(
        status=status,
        current_version=__version__,
        channel=channel,
        release=release,
        can_apply=apply_mode in (ApplyMode.NATIVE, ApplyMode.SETUP),
        apply_mode=apply_mode,
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
    """Directory for verified update downloads (under the user-data root)."""
    path = updates_root(local_app_dir())
    os.makedirs(path, exist_ok=True)
    return path


def prune_stale_downloads() -> List[str]:
    """Delete finished update downloads, returning the names removed.

    Native-update transaction directories are owned by the updater recovery
    path. Regular setup/archive files are collected after a short startup
    grace period so this cannot race an active download.
    """
    cutoff = time.time() - _DOWNLOAD_KEEP_WINDOW_S
    removed: List[str] = []
    try:
        entries = list(os.scandir(updates_dir()))
    except OSError as exc:
        logger.debug("Could not read the updates directory: %s", exc)
        return removed
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                continue
            os.unlink(entry.path)
        except OSError as exc:
            # A Windows installer that is running stays locked; the next
            # startup collects it.
            logger.debug("Could not delete %s: %s", entry.path, exc)
            continue
        removed.append(entry.name)
    return removed


def _regular_file_stat(path: str) -> Optional[os.stat_result]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppUpdateError("The downloaded update could not be inspected.") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AppUpdateError("The update download path is not a regular file.")
    return info


def download_release_asset(
    asset: ReleaseAsset,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
    *,
    destination_dir: Optional[str] = None,
) -> str:
    """Download and SHA-256-verify one authorized release asset."""
    digest = (asset.sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AppUpdateError("The update package is missing an integrity digest.")
    if asset.name != os.path.basename(asset.name) or not asset.name:
        raise AppUpdateError("The update package has an invalid filename.")
    if asset.size_bytes <= 0 or asset.size_bytes > _MAX_ASSET_BYTES:
        raise AppUpdateError("The update package has an invalid size.")
    if not progress:
        progress = lambda _phase, _done, _total: None
    if cancel is None:
        cancel = threading.Event()
    if cancel.is_set():
        raise AppUpdateError("The download was cancelled.")

    directory = destination_dir if destination_dir is not None else updates_dir()
    destination = os.path.join(directory, asset.name)
    info = _regular_file_stat(destination)
    if info is not None:
        existing = (
            _file_sha256(destination)
            if info.st_size == asset.size_bytes
            else ""
        )
        if cancel.is_set():
            raise AppUpdateError("The download was cancelled.")
        if existing == digest:
            progress(DownloadPhase.VERIFYING, asset.size_bytes, asset.size_bytes)
            return destination
        try:
            os.unlink(destination)
        except OSError as exc:
            raise AppUpdateError("The old update download could not be replaced.") from exc

    _download_verified(
        url=asset.url,
        sha256_hex=digest,
        size_bytes=asset.size_bytes,
        destination=destination,
        progress=progress,
        cancel=cancel,
    )
    return destination


def download_installer(
    release: ReleaseInfo,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Download and SHA-256-verify the Windows setup exe.

    Refuses to run unless this process may apply a verified setup asset.
    Never writes a setup exe into a source checkout.
    """
    if not _asset_ready(release.setup_asset) or detect_channel() != InstallChannel.INSTALLER:
        raise AppUpdateError(
            "This copy of OpenWhisper cannot install the Windows setup package."
        )
    if sys.platform != "win32":
        raise AppUpdateError(
            "This copy of OpenWhisper cannot install the Windows setup package."
        )
    assert release.setup_asset is not None
    return download_release_asset(release.setup_asset, progress=progress, cancel=cancel)


def apply_update(
    result: UpdateCheckResult,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
    *,
    force_setup: bool = False,
) -> str:
    """Download and prepare a verified Windows setup or native payload.

    Returns:
        A setup exe path or ``native:<transaction_id>``.
    """
    release = result.release
    if release is None:
        raise AppUpdateError("No installer is available.")
    mode = ApplyMode.SETUP if force_setup else result.apply_mode
    if mode == ApplyMode.NATIVE and release.native_asset is not None:
        try:
            archive_path = download_release_asset(
                release.native_asset, progress=progress, cancel=cancel
            )
            registration = discover_install_registration()
            if registration is None:
                raise UpdateApplyError(
                    "This installation is not registered for native updates."
                )
            journal = prepare_candidate(
                archive_path,
                release_version=release.version,
                current_version=result.current_version,
                app_dir=running_app_dir(),
                registration=registration,
                progress=progress,
                cancel=cancel,
                parent_pid=os.getpid(),
            )
            return encode_native_result(journal.transaction_id)
        except UpdateCanceled:
            raise AppUpdateError("The update was cancelled.") from None
        except UpdateApplyError as exc:
            raise AppUpdateError(str(exc)) from exc
    if mode in (ApplyMode.SETUP, ApplyMode.NATIVE) and _asset_ready(release.setup_asset):
        return download_installer(release, progress=progress, cancel=cancel)
    raise AppUpdateError(
        "This copy of OpenWhisper cannot install the Windows setup package."
    )


def _parsed_url_is_well_formed(parts: urllib.parse.SplitResult) -> bool:
    try:
        port = parts.port
    except ValueError:
        return False
    common = bool(
        parts.scheme
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )
    loopback = _is_loopback_feed_url(
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, "/releases/latest", "", "")
        )
    )
    return common and (port in (None, 80, 443) or loopback)


def _redirect_url_allowed(initial_url: str, candidate_url: str) -> bool:
    """Validate a redirect/final URL against the initial trusted endpoint."""
    initial = urllib.parse.urlsplit(initial_url)
    candidate = urllib.parse.urlsplit(candidate_url)
    if not _parsed_url_is_well_formed(candidate):
        return False
    initial_host = (initial.hostname or "").lower()
    host = (candidate.hostname or "").lower()
    if _is_loopback_feed_url(
        urllib.parse.urlunsplit(
            (initial.scheme, initial.netloc, "/releases/latest", "", "")
        )
    ):
        return _url_origin(initial_url) == _url_origin(candidate_url)
    if initial.scheme.lower() != "https" or candidate.scheme.lower() != "https":
        return False
    if candidate.port not in (None, 443):
        return False
    if initial_host == "api.github.com":
        return host == "api.github.com"
    if initial_host == "github.com":
        if host == "github.com":
            return True
        return (
            host in _GITHUB_RELEASE_ASSET_HOSTS
            and candidate.path.startswith("/github-production-release-asset/")
        )
    return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, initial_url: str):
        super().__init__()
        self._initial_url = initial_url
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirects += 1
        if (
            self._redirects > _MAX_REDIRECTS
            or not _redirect_url_allowed(self._initial_url, newurl)
        ):
            raise urllib.error.URLError("The update server redirected to an unsafe URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, extra_headers: Optional[Dict[str, str]] = None):
    if not _redirect_url_allowed(url, url):
        raise urllib.error.URLError("The update URL is not trusted.")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for key, value in (extra_headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(_SafeRedirectHandler(url))
    response = opener.open(request, timeout=_NETWORK_TIMEOUT_S)
    final_url = response.geturl()
    if not _redirect_url_allowed(url, final_url):
        response.close()
        raise urllib.error.URLError("The update server returned an unsafe URL.")
    return response


def update_check_failure_status(error: str) -> str:
    """Short status-bar copy for a failed automatic update check."""
    text = (error or "").lower()
    if "rate-limited" in text or "rate limit" in text:
        return _RATE_LIMIT_STATUS
    return "Could not check for updates."


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
        if exc.code in (403, 429):
            return _RATE_LIMIT_MESSAGE
        return f"The update server returned an error ({exc.code} {exc.reason})."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the update server ({exc.reason})."
    return str(exc) or "The update check failed."


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AppUpdateError("The update download path is not a regular file.")
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _response_header(response: object, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get(name)
        return str(value) if value is not None else None
    getter = getattr(response, "getheader", None)
    if getter is not None:
        value = getter(name)
        return str(value) if value is not None else None
    return None


def _discard_regular_file(path: str) -> None:
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            os.unlink(path)
    except OSError:
        pass


def _download_verified(
    url: str,
    sha256_hex: str,
    size_bytes: int,
    destination: str,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> None:
    """Fetch ``url`` with bounded resume, exact-size, and SHA-256 checks."""
    if size_bytes <= 0 or size_bytes > _MAX_ASSET_BYTES:
        raise AppUpdateError("The update package has an invalid size.")
    part_path = destination + ".part"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(part_path, flags, 0o600)
    except OSError as exc:
        raise AppUpdateError("The update download could not be created safely.") from exc

    try:
        with os.fdopen(fd, "r+b") as out:
            info = os.fstat(out.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AppUpdateError("The update partial is not a regular file.")
            try:
                os.chmod(part_path, 0o600, follow_symlinks=False)
            except (NotImplementedError, OSError):
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(out.fileno(), 0o600)

            resume_from = info.st_size
            if resume_from > size_bytes:
                out.seek(0)
                out.truncate(0)
                resume_from = 0

            digest = hashlib.sha256()
            out.seek(0)
            remaining = resume_from
            while remaining:
                if cancel.is_set():
                    raise AppUpdateError("The download was cancelled.")
                block = out.read(min(_CHUNK_BYTES, remaining))
                if not block:
                    raise AppUpdateError("The partial update could not be read.")
                digest.update(block)
                remaining -= len(block)

            if cancel.is_set():
                raise AppUpdateError("The download was cancelled.")
            headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
            with _open(url, headers) as response:
                status = getattr(response, "status", None)
                if resume_from and status != 206:
                    logger.info("Server ignored Range header; restarting download")
                    resume_from = 0
                    digest = hashlib.sha256()
                    out.seek(0)
                    out.truncate(0)
                elif resume_from:
                    content_range = _response_header(response, "Content-Range") or ""
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if (
                        match is None
                        or int(match.group(1)) != resume_from
                        or int(match.group(2)) >= size_bytes
                        or int(match.group(3)) != size_bytes
                    ):
                        raise AppUpdateError(
                            "The update server returned an invalid resume response."
                        )

                expected_response_bytes = size_bytes - resume_from
                content_length = _response_header(response, "Content-Length")
                if content_length is not None:
                    try:
                        declared_response_bytes = int(content_length)
                    except ValueError as exc:
                        raise AppUpdateError(
                            "The update server returned an invalid download size."
                        ) from exc
                    if declared_response_bytes != expected_response_bytes:
                        raise AppUpdateError(
                            "The update server returned an unexpected download size."
                        )

                out.seek(resume_from)
                written = resume_from
                while True:
                    if cancel.is_set():
                        raise AppUpdateError("The download was cancelled.")
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    if written + len(chunk) > size_bytes:
                        raise AppUpdateError(
                            "The update server returned more data than expected."
                        )
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    progress(DownloadPhase.DOWNLOADING, written, size_bytes)

            if cancel.is_set():
                raise AppUpdateError("The download was cancelled.")
            actual_size = out.tell()
            if actual_size != size_bytes:
                raise AppUpdateError(
                    "The download did not complete "
                    f"({format_size_bytes(actual_size)} of "
                    f"{format_size_bytes(size_bytes)})."
                )
            progress(DownloadPhase.VERIFYING, actual_size, actual_size)
            if cancel.is_set():
                raise AppUpdateError("The download was cancelled.")
            if digest.hexdigest() != sha256_hex.lower():
                raise AppUpdateError(
                    "The download failed its integrity check and was discarded. "
                    "Please try again."
                )
            out.flush()
            os.fsync(out.fileno())
            if cancel.is_set():
                raise AppUpdateError("The download was cancelled.")
    except AppUpdateError:
        _discard_regular_file(part_path)
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise AppUpdateError(_describe_network_error(exc)) from exc

    if cancel.is_set():
        _discard_regular_file(part_path)
        raise AppUpdateError("The download was cancelled.")
    try:
        os.replace(part_path, destination)
        try:
            os.chmod(destination, 0o600, follow_symlinks=False)
        except NotImplementedError:
            os.chmod(destination, 0o600)
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(os.path.dirname(destination), directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        _discard_regular_file(part_path)
        raise AppUpdateError("The verified update could not be finalized.") from exc
    if cancel.is_set():
        _discard_regular_file(destination)
        raise AppUpdateError("The download was cancelled.")
