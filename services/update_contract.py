"""Shared constants for native application update artifacts.

Stdlib-only so the windowless Windows updater helper can import this module
without pulling in Qt, settings, or the rest of the application.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Final, List, Optional, Tuple

APP_NAME: Final[str] = "OpenWhisper"
APP_ID: Final[str] = "{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}"
APP_ID_BARE: Final[str] = "CA36AD0A-13B9-4737-87AD-ADB54A28EFC9"
INNO_UNINSTALL_KEY: Final[str] = APP_ID + "_is1"

APP_EXE_NAME: Final[str] = "OpenWhisper.exe"
UPDATER_EXE_NAME: Final[str] = "OpenWhisperUpdater.exe"
INTERNAL_DIRNAME: Final[str] = "_internal"
MANIFEST_NAME: Final[str] = ".openwhisper-update.json"
SENTINEL_NAME: Final[str] = ".openwhisper-update-complete"
NVIDIA_RELATIVE: Final[str] = os.path.join(INTERNAL_DIRNAME, "nvidia")

MANIFEST_SCHEMA_VERSION: Final[int] = 1
TOPOLOGY_REVISION: Final[int] = 1
ARCHITECTURE: Final[str] = "win-x64"
MINIMUM_UPDATER_VERSION: Final[str] = "2.5.2"

SETUP_NAME_PREFIX: Final[str] = "OpenWhisper-Setup-"
ARCHIVE_NAME_SUFFIX: Final[str] = "-win64.tar.xz"

UPDATES_DIRNAME: Final[str] = "updates"
TRANSACTIONS_DIRNAME: Final[str] = "tx"
JOURNAL_NAME: Final[str] = "journal.json"
HEALTH_NAME: Final[str] = "healthy"
ERROR_NAME: Final[str] = "apply_error.txt"
NATIVE_RESULT_PREFIX: Final[str] = "native:"

APP_MUTEX_NAME: Final[str] = f"OpenWhisper-App-{APP_ID_BARE}"
UPDATE_MUTEX_NAME: Final[str] = f"OpenWhisper-Update-{APP_ID_BARE}"
GLOBAL_APP_MUTEX_NAME: Final[str] = "Global\\" + APP_MUTEX_NAME
GLOBAL_UPDATE_MUTEX_NAME: Final[str] = "Global\\" + UPDATE_MUTEX_NAME
SETUP_MUTEX_NAME: Final[str] = f"Global\\OpenWhisper-Setup-{APP_ID_BARE}"
APP_MUTEX_NAMES: Final[Tuple[str, ...]] = (APP_MUTEX_NAME, GLOBAL_APP_MUTEX_NAME)
UPDATE_MUTEX_NAMES: Final[Tuple[str, ...]] = (
    UPDATE_MUTEX_NAME,
    GLOBAL_UPDATE_MUTEX_NAME,
)
# The ``!`` prefix tells Windows to remove the RunOnce value only after the
# recovery command starts successfully, rather than before launch.
RUNONCE_VALUE_NAME: Final[str] = "!OpenWhisperUpdateRecover"

HEALTH_ARG: Final[str] = "--update-health"
TRANSACTION_ARG: Final[str] = "--transaction-id"
RECOVER_ARG: Final[str] = "--recover"
CLEANUP_ARG: Final[str] = "--cleanup-transaction"
PARENT_PID_ARG: Final[str] = "--parent-pid"

MANAGED_TOP_LEVEL_FILES: Final[Tuple[str, ...]] = (
    APP_EXE_NAME,
    UPDATER_EXE_NAME,
    MANIFEST_NAME,
)
MANAGED_TOP_LEVEL_DIRS: Final[Tuple[str, ...]] = (INTERNAL_DIRNAME,)

UNINSTALL_KEY_PATH: Final[str] = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" + INNO_UNINSTALL_KEY
)

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+\Z")
_TRANSACTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")


class TransactionState:
    """Durable helper states. Every transition must be idempotent."""

    PREPARING: Final[str] = "preparing"
    PREPARED: Final[str] = "prepared"
    OLD_MOVED: Final[str] = "old_moved"
    NEW_ACTIVE: Final[str] = "new_active"
    HEALTHY: Final[str] = "healthy"
    ROLLED_BACK: Final[str] = "rolled_back"
    SUPERSEDED: Final[str] = "superseded"


class ApplyMode:
    """How this process may apply a GitHub release."""

    NATIVE: Final[str] = "native"
    SETUP: Final[str] = "setup"
    NOTIFY_ONLY: Final[str] = "notify_only"


def normalize_version(raw: str) -> str:
    """Strip a leading ``v`` and surrounding whitespace from a version string."""
    text = (raw or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text.strip()


def parse_strict_version(raw: str) -> Tuple[int, int, int]:
    """Parse ``major.minor.patch`` and reject anything else.

    Used for apply authorization. Display/compare helpers may be more
    lenient; a native update must not treat ``2.4.0-rc.1`` as ``2.4.0``.
    """
    text = normalize_version(raw)
    if not _VERSION_RE.fullmatch(text):
        raise ValueError(f"Not a strict major.minor.patch version: {raw!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def is_newer_version(current: str, candidate: str) -> bool:
    """Return True when ``candidate`` is a strictly newer release than ``current``."""
    return parse_strict_version(candidate) > parse_strict_version(current)


def is_setup_bridge_release(version: str) -> bool:
    """Old installed helpers must receive setup before this native protocol."""
    return parse_strict_version(version) <= parse_strict_version(MINIMUM_UPDATER_VERSION)


def setup_asset_name(version: str) -> str:
    """Return the exact GitHub setup exe filename for ``version``."""
    return f"{SETUP_NAME_PREFIX}{normalize_version(version)}.exe"


def archive_asset_name(version: str) -> str:
    """Return the exact GitHub native-archive filename for ``version``."""
    return f"{APP_NAME}-{normalize_version(version)}{ARCHIVE_NAME_SUFFIX}"


def validate_transaction_id(transaction_id: str) -> str:
    """Return a canonical transaction id, rejecting path-like input."""
    token = (transaction_id or "").strip().lower()
    if not _TRANSACTION_ID_RE.fullmatch(token):
        raise ValueError("Invalid update transaction id.")
    return token


def local_app_dir() -> str:
    """Per-user data root (``%LOCALAPPDATA%\\OpenWhisper`` on Windows)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


def updates_root(appdata: Optional[str] = None) -> str:
    """Directory for verified downloads and update transactions."""
    root = appdata if appdata is not None else local_app_dir()
    return os.path.join(root, UPDATES_DIRNAME)


def transaction_dir(transaction_id: str, appdata: Optional[str] = None) -> str:
    """Return the exclusive directory for one update transaction."""
    token = validate_transaction_id(transaction_id)
    return os.path.join(updates_root(appdata), TRANSACTIONS_DIRNAME, token)


def journal_path(transaction_id: str, appdata: Optional[str] = None) -> str:
    return os.path.join(transaction_dir(transaction_id, appdata), JOURNAL_NAME)


def health_path(transaction_id: str, appdata: Optional[str] = None) -> str:
    return os.path.join(transaction_dir(transaction_id, appdata), HEALTH_NAME)


def apply_error_path(appdata: Optional[str] = None) -> str:
    return os.path.join(updates_root(appdata), ERROR_NAME)


def encode_native_result(transaction_id: str) -> str:
    return f"{NATIVE_RESULT_PREFIX}{validate_transaction_id(transaction_id)}"


def decode_native_result(raw: str) -> Optional[str]:
    if not raw.startswith(NATIVE_RESULT_PREFIX):
        return None
    token = raw[len(NATIVE_RESULT_PREFIX):].strip()
    try:
        return validate_transaction_id(token)
    except ValueError:
        return None


def dump_json(payload: Dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def required_manifest_files() -> List[str]:
    return list(MANAGED_TOP_LEVEL_FILES)
