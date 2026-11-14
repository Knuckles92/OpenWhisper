"""Notice a macOS Accessibility grant that an update took away, and clear it.

macOS ties the grant to the signed build. An ad-hoc signed update is a new
build, so the old entry stays in System Settings, still switched on, while the
running app has no access. Removing that entry is the only way the switch
works again.
"""

import logging
import subprocess
import sys

from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)

_TCCUTIL = "/usr/bin/tccutil"
_TCCUTIL_TIMEOUT_S = 10


def grant_lost(trusted: bool) -> bool:
    """Whether auto-paste had access that this launch lacks.

    A copy that never recorded its grant counts once setup has been seen: it
    was installed before the record existed, so it lost access in the very
    update that brought it.
    """
    if trusted:
        return False
    settings = settings_manager.load_all_settings()
    granted = SettingsKey.MACOS_ACCESSIBILITY_GRANTED.read(settings)
    if granted is None:
        return SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN.read(settings)
    return granted is True


def remember_grant(trusted: bool) -> None:
    """Record whether this launch has access, for the next one to compare."""
    try:
        settings = settings_manager.load_all_settings()
        if SettingsKey.MACOS_ACCESSIBILITY_GRANTED.read(settings) is not trusted:
            settings_manager.save_setting(SettingsKey.MACOS_ACCESSIBILITY_GRANTED, trusted)
    except Exception:
        logger.exception("Could not remember the Accessibility grant")


def _frozen_bundle_id() -> str:
    """This app's bundle identifier; empty for a development launch.

    A development launch runs as Python.app, whose entry is not ours to remove.
    """
    from services.app_update_macos import bundle_info, running_bundle

    bundle = running_bundle()
    if bundle is None:
        return ""
    try:
        identifier = bundle_info(bundle).get("CFBundleIdentifier")
    except (OSError, ValueError):
        logger.warning("Could not read this app's bundle identifier", exc_info=True)
        return ""
    return identifier if isinstance(identifier, str) else ""


def reset_stale_grant() -> bool:
    """Remove this app's Accessibility entry so macOS lists the current build."""
    if sys.platform != "darwin":
        return False
    bundle_id = _frozen_bundle_id()
    if not bundle_id:
        return False
    try:
        result = subprocess.run(
            [_TCCUTIL, "reset", "Accessibility", bundle_id],
            capture_output=True, text=True, timeout=_TCCUTIL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not reset the Accessibility entry: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "tccutil could not reset the Accessibility entry: %s",
            (result.stderr or result.stdout).strip(),
        )
        return False
    logger.info("Removed the stale Accessibility entry for %s", bundle_id)
    return True
