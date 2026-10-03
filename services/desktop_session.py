"""Desktop capabilities without importing Qt or opening an X connection."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def is_wayland_session() -> bool:
    return sys.platform.startswith("linux") and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def omarchy_theme_paths() -> tuple[Path, ...]:
    """Omarchy 4 stores state separately; keep the Omarchy 3 location too."""
    home = Path.home()
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return (
        state / "omarchy/current/theme/colors.toml",
        config / "omarchy/current/theme/colors.toml",
    )


def use_omarchy_ui() -> bool:
    """Auto-select on Omarchy, with an explicit preview/rollback override."""
    override = os.environ.get("OPENWHISPER_UI", "auto").lower()
    if override in ("omarchy", "classic"):
        return override == "omarchy"
    if not sys.platform.startswith("linux"):
        return False
    return _detect_omarchy(os.environ.get("OMARCHY_PATH", ""), omarchy_theme_paths())


@lru_cache(maxsize=4)
def _detect_omarchy(install_path: str, theme_paths: tuple[Path, ...]) -> bool:
    # Called for every widget stylesheet. Probe the filesystem once per session.
    if install_path or any(p.is_file() for p in theme_paths):
        return True
    try:
        return any(
            line.strip().strip('"') == "ID=omarchy" or line.strip() == 'ID="omarchy"'
            for line in Path("/etc/os-release").read_text().splitlines()
        )
    except OSError:
        return False
