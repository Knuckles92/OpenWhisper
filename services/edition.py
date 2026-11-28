"""Edition detection and build-time manifest helpers for OpenWhisper.

OpenWhisper ships in two editions:
- ``"full"``: Complete standalone desktop app with bundled local speech
  runtimes and full host/standalone capabilities.
- ``"client"``: Lightweight Client-Only edition that omits local ML/FFmpeg
  runtimes (CTranslate2, faster-whisper, ONNX Runtime, PyAV, etc.) and pairs
  with a remote OpenWhisper Host or cloud APIs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from config import bundle_root

EDITION_FULL: Final[str] = "full"
EDITION_CLIENT: Final[str] = "client"
VALID_EDITIONS: Final[frozenset[str]] = frozenset({EDITION_FULL, EDITION_CLIENT})
EDITION_ENV_VAR: Final[str] = "OPENWHISPER_EDITION"
EDITION_MANIFEST_NAME: Final[str] = "edition.json"
EDITION_MANIFEST_SCHEMA_VERSION: Final[int] = 1


def normalize_edition(raw: object) -> str | None:
    """Return ``"full"`` or ``"client"`` for a valid value, else ``None``."""
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized in VALID_EDITIONS:
        return normalized
    return None


def read_edition_manifest(bundle_dir: Path | str | None = None) -> str | None:
    """Read ``edition.json`` from ``bundle_dir`` (defaults to ``config.bundle_root()``)."""
    root = Path(bundle_dir) if bundle_dir is not None else Path(bundle_root())
    manifest_path = root / EDITION_MANIFEST_NAME
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_edition(payload.get("edition"))


def current_edition(*, bundle_dir: Path | str | None = None) -> str:
    """Return the active edition (``"full"`` or ``"client"``).

    Resolution order:
    1. ``OPENWHISPER_EDITION`` environment variable (case-insensitive) for
       development, CI, and test overrides.
    2. ``edition.json`` in ``bundle_dir`` / ``config.bundle_root()`` for frozen
       installations.
    3. Default ``"full"``.
    """
    env_edition = normalize_edition(os.environ.get(EDITION_ENV_VAR))
    if env_edition is not None:
        return env_edition
    manifest_edition = read_edition_manifest(bundle_dir)
    if manifest_edition is not None:
        return manifest_edition
    return EDITION_FULL


def is_client_only(*, bundle_dir: Path | str | None = None) -> bool:
    """Return ``True`` when running the Client-Only edition."""
    return current_edition(bundle_dir=bundle_dir) == EDITION_CLIENT


def edition_label(edition: str | None = None) -> str:
    """Return the user-facing display label for ``edition`` (or active edition)."""
    resolved = (
        normalize_edition(edition) if edition is not None else current_edition()
    )
    if resolved == EDITION_CLIENT:
        return "OpenWhisper Client"
    return "OpenWhisper"


def write_edition_manifest(dest_dir: Path | str, edition: str) -> Path:
    """Write ``edition.json`` into ``dest_dir`` and return its path.

    Raises:
        ValueError: If ``edition`` is neither ``"full"`` nor ``"client"``.
    """
    normalized = normalize_edition(edition)
    if normalized is None:
        raise ValueError(
            f"Unsupported OpenWhisper edition {edition!r}; "
            f"expected one of {sorted(VALID_EDITIONS)}"
        )
    target_dir = Path(dest_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / EDITION_MANIFEST_NAME
    payload = {
        "edition": normalized,
        "schema_version": EDITION_MANIFEST_SCHEMA_VERSION,
    }
    tmp_path = target_dir / f".{EDITION_MANIFEST_NAME}.tmp"
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, target_path)
    return target_path
