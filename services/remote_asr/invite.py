"""Host-served network invite/download server, ``openwhisper://pair`` links, and IPC.

Enables zero-friction onboarding of client computers on the same LAN or Tailscale
tailnet:

* ``parse_invite_uri`` / ``build_invite_uri`` and ``.openwhisper-invite`` JSON
  helpers strictly validate ``openwhisper://pair?...`` links carrying the host
  address, port, 64-hex SHA-256 TLS fingerprint, 6-digit pairing code, display
  name, and optional alternate addresses.
* ``HostInviteServer`` is a lightweight opt-in stdlib HTTP server (no self-signed
  TLS browser warning) gated by a short-lived revocable token (15 minutes by
  default). It serves a self-contained dark-mode join page with OS-detected
  official GitHub Release client installers (or SHA-256-verified LAN cached
  installers), a one-click ``openwhisper://pair`` link, a downloadable
  ``.openwhisper-invite`` file, QR code, and manual fallback.
* ``InviteIpcServer`` and ``forward_invite_to_running_instance`` forward deep
  links to an already-running OpenWhisper instance over authenticated loopback
  IPC, and ``pair_with_invite`` enforces strict TLS fingerprint verification and
  never silently replaces a different paired host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import (
    Any,
    Final,
)

from _version import __version__
from services.remote_asr import protocol
from services.remote_asr.qr import render_invite_qr_svg

logger = logging.getLogger(__name__)

INVITE_SCHEME: Final[str] = "openwhisper"
INVITE_AUTHORITY: Final[str] = "pair"
INVITE_FILE_EXTENSION: Final[str] = ".openwhisper-invite"
INVITE_FILE_SCHEMA_VERSION: Final[int] = 1

DEFAULT_INVITE_PORT: Final[int] = 47822
DEFAULT_INVITE_TTL_S: Final[float] = 15.0 * 60.0
MAX_INVITE_URI_CHARS: Final[int] = 2048
MAX_INVITE_FILE_BYTES: Final[int] = 16 * 1024
MAX_ALTERNATES: Final[int] = 8
MAX_INSTALLER_BYTES: Final[int] = 512 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES: Final[int] = 64 * 1024

GITHUB_REPO: Final[str] = "Knuckles92/OpenWhisper"
GITHUB_RELEASES_URL: Final[str] = f"https://github.com/{GITHUB_REPO}/releases"
REMOTE_HOST_PROVIDER: Final[str] = "remote_host"

#: Host opt-in AI service keys matching Phase 1 (services/settings.py).
HOST_CAPABILITY_SPECS: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    (
        "text_cleanup",
        "REMOTE_HOST_TEXT_CLEANUP",
        "remote_host_text_cleanup",
        "AI Cleanup & Rewrites",
        "Let paired computers polish, clean up, and rewrite dictations using this host's AI provider.",
    ),
    (
        "fast_judgments",
        "REMOTE_HOST_FAST_JUDGMENTS",
        "remote_host_fast_judgments",
        "Fast Judgments",
        "Let paired computers route quick classification and judgment checks through this host.",
    ),
    (
        "walkthrough_writeup",
        "REMOTE_HOST_WALKTHROUGH_WRITEUP",
        "remote_host_walkthrough_writeup",
        "Walkthrough Write-Ups",
        "Let paired computers send walkthrough words and screenshots here for AI write-up generation.",
    ),
    (
        "meeting_ai",
        "REMOTE_HOST_MEETING_AI",
        "remote_host_meeting_ai",
        "Meeting AI & Speaker ID",
        "Let paired computers run speaker diarization embeddings and meeting insights on this host.",
    ),
    (
        "agent_ask",
        "REMOTE_HOST_AGENT_ASK",
        "remote_host_agent_ask",
        "Agent Ask",
        "Let paired computers run quick Ask-mode questions through this host's coding agent.",
    ),
)

HOST_INVITE_ENABLED_SPEC: Final[tuple[str, str]] = (
    "REMOTE_HOST_INVITE_ENABLED",
    "remote_host_invite_enabled",
)
HOST_INVITE_PORT_SPEC: Final[tuple[str, str]] = (
    "REMOTE_HOST_INVITE_PORT",
    "remote_host_invite_port",
)
HOST_INVITE_CACHE_SPEC: Final[tuple[str, str]] = (
    "REMOTE_HOST_INVITE_CACHE",
    "remote_host_invite_cache",
)

_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{64}$")
_CODE6_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{6}$")
_HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256SUM_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^([0-9a-fA-F]{64})\s+\*?([^\s/\\]+)$"
)
_SAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$")
_STRICT_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

_IPC_FILENAME: Final[str] = "invite_ipc.json"
_PENDING_LOCK = threading.Lock()
_PENDING_STARTUP_INVITE: InviteLink | None = None


class InviteValidationError(ValueError):
    """Raised when an invite URI, file, or cached asset fails strict validation."""


class InvitePairingDeclined(RuntimeError):
    """Raised when the user declines replacing an existing paired host."""


# ---- Edition & Settings compatibility helpers ----


def is_client_only_edition() -> bool:
    """Return True when running the Client-Only OpenWhisper edition."""
    try:
        from services.edition import is_client_only

        return bool(is_client_only())
    except ImportError:
        return os.environ.get("OPENWHISPER_EDITION", "").strip().lower() == "client"


def current_edition_name() -> str:
    """Return ``'client'`` or ``'full'``."""
    try:
        from services.edition import current_edition

        return str(current_edition())
    except ImportError:
        return "client" if is_client_only_edition() else "full"


def edition_display_label() -> str:
    """Return ``'OpenWhisper Client'`` or ``'OpenWhisper'``."""
    try:
        from services.edition import edition_label

        return str(edition_label())
    except ImportError:
        return "OpenWhisper Client" if is_client_only_edition() else "OpenWhisper"


def resolve_settings_key(attr_name: str, fallback_key: str) -> Any:
    """Return ``SettingsKey.<attr_name>`` when defined, else the raw key string."""
    from services.settings import SettingsKey

    return getattr(SettingsKey, attr_name, fallback_key)


def read_setting_compat(
    attr_name: str,
    fallback_key: str,
    default: Any,
    settings: Mapping[str, Any] | None = None,
    manager: Any = None,
) -> Any:
    """Read a host/client setting safely before or after SettingsKey registration."""
    key = resolve_settings_key(attr_name, fallback_key)
    if settings is None:
        if manager is None:
            from services.settings import settings_manager

            manager = settings_manager
        settings = manager.load_all_settings()
    if hasattr(key, "read"):
        return key.read(settings)
    raw = settings.get(fallback_key, default)
    if isinstance(default, bool):
        return raw is True
    if isinstance(default, int):
        try:
            if isinstance(raw, bool):
                return default
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def save_setting_compat(
    attr_name: str,
    fallback_key: str,
    value: Any,
    manager: Any = None,
) -> None:
    """Persist a host/client setting using SettingsKey when available."""
    if manager is None:
        from services.settings import settings_manager

        manager = settings_manager
    key = resolve_settings_key(attr_name, fallback_key)
    manager.save_setting(key, value)


# ---- Strict validation for openwhisper://pair & .openwhisper-invite ----


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _validate_host(raw: Any, *, field_name: str = "host") -> str:
    if not isinstance(raw, str):
        raise InviteValidationError(f"Invite {field_name} must be a string.")
    if _has_control_chars(raw):
        raise InviteValidationError(f"Invite {field_name} contains control characters.")
    host = raw.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 253:
        raise InviteValidationError(f"Invite {field_name} is empty or too long.")
    if any(ch.isspace() for ch in host) or any(ch in "/\\?#@%" for ch in host):
        raise InviteValidationError(f"Invite {field_name} contains invalid characters.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if ":" in host:
        raise InviteValidationError(f"Invite {field_name} is not a valid hostname or IP.")
    if not _HOSTNAME_RE.fullmatch(host):
        raise InviteValidationError(f"Invite {field_name} is not a valid hostname or IP.")
    return host


def _validate_port(raw: Any) -> int:
    if isinstance(raw, bool):
        raise InviteValidationError("Invite port must be an integer between 1 and 65535.")
    if isinstance(raw, str):
        if _has_control_chars(raw) or not raw.strip().isdigit():
            raise InviteValidationError("Invite port must be an integer between 1 and 65535.")
        port = int(raw.strip())
    elif isinstance(raw, int):
        port = raw
    else:
        raise InviteValidationError("Invite port must be an integer between 1 and 65535.")
    if not 1 <= port <= 65535:
        raise InviteValidationError("Invite port must be between 1 and 65535.")
    return port


def _validate_fingerprint(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InviteValidationError("Invite fingerprint must be a 64-character SHA-256 hex string.")
    if _has_control_chars(raw):
        raise InviteValidationError("Invite fingerprint contains control characters.")
    fp = raw.strip()
    if not _HEX64_RE.fullmatch(fp):
        raise InviteValidationError("Invite fingerprint must be 64 hexadecimal characters.")
    return fp.upper()


def _validate_code(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InviteValidationError("Invite pairing code must be a 6-digit string.")
    if _has_control_chars(raw):
        raise InviteValidationError("Invite pairing code contains control characters.")
    code = raw.strip()
    if not _CODE6_RE.fullmatch(code):
        raise InviteValidationError("Invite pairing code must be exactly 6 digits.")
    return code


def _validate_name(raw: Any, fallback: str) -> str:
    if raw is None or raw == "":
        return protocol.clean_name(fallback) or fallback[: protocol.MAX_NAME]
    if not isinstance(raw, str):
        raise InviteValidationError("Invite host name must be a string.")
    if _has_control_chars(raw):
        raise InviteValidationError("Invite host name contains control characters.")
    cleaned = protocol.clean_name(raw)
    return cleaned or protocol.clean_name(fallback) or fallback[: protocol.MAX_NAME]


def _validate_alternates(raw: Any, primary_host: str) -> tuple[str, ...]:
    if raw is None or raw == "" or raw == ():
        return ()
    if isinstance(raw, str):
        if _has_control_chars(raw):
            raise InviteValidationError("Invite alternate addresses contain control characters.")
        items: list[Any] = [part for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise InviteValidationError("Invite alternate addresses must be a list or CSV string.")
    if len(items) > MAX_ALTERNATES:
        raise InviteValidationError(f"Invite specifies too many alternate addresses (max {MAX_ALTERNATES}).")
    seen = {primary_host}
    result: list[str] = []
    for item in items:
        host = _validate_host(item, field_name="alternate address")
        if host not in seen:
            seen.add(host)
            result.append(host)
    return tuple(result)


@dataclass(frozen=True)
class InviteLink:
    """A strictly validated OpenWhisper host pairing invite."""

    host: str
    port: int
    fingerprint: str
    code: str
    name: str = ""
    alternates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        host = _validate_host(self.host)
        port = _validate_port(self.port)
        fp = _validate_fingerprint(self.fingerprint)
        code = _validate_code(self.code)
        name = _validate_name(self.name, host)
        alts = _validate_alternates(self.alternates, host)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "fingerprint", fp)
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "alternates", alts)

    @property
    def address(self) -> str:
        return protocol.format_address(self.host, self.port)

    @property
    def short_fingerprint(self) -> str:
        return protocol.short_fingerprint(self.fingerprint)

    @property
    def uri(self) -> str:
        return build_invite_uri(
            self.host,
            self.port,
            self.fingerprint,
            self.code,
            name=self.name,
            alternates=self.alternates,
        )


def build_invite_uri(
    host: str,
    port: int,
    fingerprint: str,
    code: str,
    *,
    name: str = "",
    alternates: Iterable[str] = (),
) -> str:
    """Build a canonical, strictly validated ``openwhisper://pair?...`` URI."""
    validated_host = _validate_host(host)
    validated_port = _validate_port(port)
    validated_fp = _validate_fingerprint(fingerprint)
    validated_code = _validate_code(code)
    validated_name = _validate_name(name, validated_host)
    validated_alts = _validate_alternates(tuple(alternates), validated_host)

    params: list[tuple[str, str]] = [
        ("host", validated_host),
        ("port", str(validated_port)),
        ("fp", validated_fp),
        ("code", validated_code),
    ]
    if validated_name:
        params.append(("name", validated_name))
    if validated_alts:
        params.append(("alt", ",".join(validated_alts)))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    uri = f"{INVITE_SCHEME}://{INVITE_AUTHORITY}?{query}"
    if len(uri) > MAX_INVITE_URI_CHARS:
        raise InviteValidationError("Invite URI exceeds maximum length.")
    return uri


def parse_invite_uri(uri: str) -> InviteLink:
    """Strictly parse and validate an ``openwhisper://pair?...`` URI."""
    if not isinstance(uri, str):
        raise InviteValidationError("Invite URI must be a string.")
    if _has_control_chars(uri):
        raise InviteValidationError("Invite URI contains control characters.")
    cleaned = uri.strip()
    if not cleaned:
        raise InviteValidationError("Invite URI is empty.")
    if len(cleaned) > MAX_INVITE_URI_CHARS:
        raise InviteValidationError("Invite URI is too long.")

    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() != INVITE_SCHEME:
        raise InviteValidationError(f"Expected {INVITE_SCHEME}:// URI scheme.")
    if parsed.netloc.lower() != INVITE_AUTHORITY:
        raise InviteValidationError(f"Expected {INVITE_SCHEME}://{INVITE_AUTHORITY} authority.")
    if parsed.path not in ("", "/"):
        raise InviteValidationError("Invite URI must not contain a path.")
    if parsed.fragment:
        raise InviteValidationError("Invite URI must not contain a fragment.")

    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise InviteValidationError(f"Duplicate query parameter: {key!r}")
        if _has_control_chars(key) or _has_control_chars(value):
            raise InviteValidationError("Invite URI contains control characters.")
        params[key] = value

    missing = [k for k in ("host", "port", "fp", "code") if k not in params]
    if missing:
        raise InviteValidationError(f"Invite URI is missing required parameter(s): {', '.join(missing)}.")

    return InviteLink(
        host=params["host"],
        port=_validate_port(params["port"]),
        fingerprint=params["fp"],
        code=params["code"],
        name=params.get("name", ""),
        alternates=_validate_alternates(params.get("alt", ""), _validate_host(params["host"])),
    )


def encode_invite_file(link: InviteLink) -> str:
    """Serialize ``link`` as a validated ``.openwhisper-invite`` JSON document."""
    payload: dict[str, Any] = {
        "schema": INVITE_FILE_SCHEMA_VERSION,
        "type": "openwhisper-invite",
        "host": link.host,
        "port": link.port,
        "fp": link.fingerprint,
        "code": link.code,
        "name": link.name,
        "alt": list(link.alternates),
        "uri": link.uri,
    }
    return json.dumps(payload, indent=2) + "\n"


def decode_invite_file(raw: str) -> InviteLink:
    """Parse and strictly validate a ``.openwhisper-invite`` JSON document."""
    if not isinstance(raw, str):
        raise InviteValidationError("Invite file content must be a UTF-8 string.")
    if len(raw.encode("utf-8")) > MAX_INVITE_FILE_BYTES:
        raise InviteValidationError("Invite file is too large.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InviteValidationError("Invite file is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise InviteValidationError("Invite file must contain a JSON object.")

    schema = data.get("schema", INVITE_FILE_SCHEMA_VERSION)
    if isinstance(schema, bool) or schema != INVITE_FILE_SCHEMA_VERSION:
        raise InviteValidationError(f"Unsupported invite file schema: {schema!r}.")
    doc_type = data.get("type", "openwhisper-invite")
    if doc_type != "openwhisper-invite":
        raise InviteValidationError("Invalid invite file type.")

    missing = [k for k in ("host", "port", "fp", "code") if k not in data]
    if missing:
        raise InviteValidationError(f"Invite file is missing required field(s): {', '.join(missing)}.")

    link = InviteLink(
        host=data["host"],
        port=_validate_port(data["port"]),
        fingerprint=data["fp"],
        code=data["code"],
        name=data.get("name", ""),
        alternates=_validate_alternates(data.get("alt", ()), _validate_host(data["host"])),
    )
    if "uri" in data:
        uri_link = parse_invite_uri(data["uri"])
        if (
            uri_link.host != link.host
            or uri_link.port != link.port
            or uri_link.fingerprint != link.fingerprint
            or uri_link.code != link.code
        ):
            raise InviteValidationError("Invite file URI does not match its fields.")
    return link


def load_invite_file(path: str | Path) -> InviteLink:
    """Load and validate a ``.openwhisper-invite`` file from disk safely."""
    file_path = Path(path)
    try:
        info = file_path.stat()
    except OSError as exc:
        raise InviteValidationError(f"Could not read invite file: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InviteValidationError("Invite path is not a regular file.")
    if info.st_size > MAX_INVITE_FILE_BYTES:
        raise InviteValidationError("Invite file is too large.")
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InviteValidationError(f"Could not read invite file: {exc}") from exc
    return decode_invite_file(content)


def parse_invite_input(text: str) -> InviteLink:
    """Parse either an ``openwhisper://pair?...`` URI or ``.openwhisper-invite`` JSON."""
    if not isinstance(text, str):
        raise InviteValidationError("Paste an openwhisper://pair link.")
    stripped = text.strip()
    if not stripped:
        raise InviteValidationError("Paste an openwhisper://pair link.")
    if stripped.startswith("{"):
        return decode_invite_file(stripped)
    return parse_invite_uri(stripped)


# ---- Client Installer Assets & SHA-256 Verified LAN Cache ----


@dataclass(frozen=True)
class ClientInstallerAsset:
    """One platform's Client-Only OpenWhisper installer artifact."""

    platform_id: str
    os_family: str  # "windows" | "macos" | "linux"
    label: str
    detail: str
    filename: str
    release_url: str


def _normalize_release_version(version: str) -> str:
    ver = (version or __version__).strip().lstrip("vV")
    if not _STRICT_VERSION_RE.fullmatch(ver):
        raise InviteValidationError(f"Invalid release version: {version!r}")
    return ver


def client_installer_assets(
    version: str = __version__,
    *,
    release_base_url: str | None = None,
) -> tuple[ClientInstallerAsset, ...]:
    """Return the official Client-Only release installers matching ``version``."""
    ver = _normalize_release_version(version)
    base = (
        release_base_url.rstrip("/")
        if release_base_url
        else f"https://github.com/{GITHUB_REPO}/releases/download/v{ver}"
    )
    specs = (
        (
            "windows",
            "windows",
            "Windows (.exe)",
            "Windows 10/11 (64-bit) installer",
            f"OpenWhisper-Client-Setup-{ver}.exe",
        ),
        (
            "macos",
            "macos",
            "macOS (.dmg)",
            "Apple Silicon disk image",
            f"OpenWhisper-Client-{ver}-macos-arm64.dmg",
        ),
        (
            "linux_deb",
            "linux",
            "Linux Debian / Ubuntu (.deb)",
            "Debian, Ubuntu, Mint (amd64)",
            f"OpenWhisper-Client-{ver}-linux-amd64.deb",
        ),
        (
            "linux_arch",
            "linux",
            "Linux Arch (.pkg.tar.zst)",
            "Arch Linux / Omarchy (x86_64)",
            f"OpenWhisper-Client-{ver}-linux-x86_64.pkg.tar.zst",
        ),
    )
    return tuple(
        ClientInstallerAsset(
            platform_id=pid,
            os_family=os_family,
            label=label,
            detail=detail,
            filename=filename,
            release_url=f"{base}/{filename}",
        )
        for pid, os_family, label, detail, filename in specs
    )


def allowed_installer_filenames(version: str = __version__) -> frozenset[str]:
    """Strict allowlist of Client-Only installer filenames for ``version``."""
    return frozenset(asset.filename for asset in client_installer_assets(version))


def detect_client_os(user_agent: str) -> str:
    """Detect ``'windows'``, ``'macos'``, or ``'linux'`` from a browser User-Agent."""
    ua = (user_agent or "").lower()
    if "windows" in ua or "win64" in ua or "win32" in ua:
        return "windows"
    if "macintosh" in ua or "mac os x" in ua or "darwin" in ua:
        return "macos"
    if "linux" in ua or "x11" in ua or "wayland" in ua or "ubuntu" in ua or "arch" in ua:
        return "linux"
    return "windows"


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``SHA256SUMS.txt`` into ``{filename: lowercase_sha256}``."""
    checksums: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SHA256SUM_LINE_RE.fullmatch(line)
        if not match:
            continue
        digest, filename = match.group(1).lower(), match.group(2)
        if _SAFE_FILENAME_RE.fullmatch(filename) and ".." not in filename:
            checksums[filename] = digest
    return checksums


def _sha256_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def default_invite_cache_dir() -> Path:
    from config import user_data_path

    return Path(user_data_path("invite_cache"))


def _validate_cache_asset_name(asset_name: str, version: str) -> str:
    if not isinstance(asset_name, str) or not asset_name:
        raise InviteValidationError("Asset name is empty.")
    if (
        _has_control_chars(asset_name)
        or "/" in asset_name
        or "\\" in asset_name
        or ".." in asset_name
        or os.path.basename(asset_name) != asset_name
        or not _SAFE_FILENAME_RE.fullmatch(asset_name)
    ):
        raise InviteValidationError("Invalid installer asset filename.")
    if asset_name not in allowed_installer_filenames(version):
        raise InviteValidationError(f"Asset {asset_name!r} is not an allowed client installer.")
    return asset_name


def _load_cache_checksums(cache_dir: Path) -> dict[str, str]:
    sums_path = cache_dir / "SHA256SUMS.txt"
    try:
        if sums_path.is_symlink() or not sums_path.is_file():
            return {}
        return parse_sha256sums(sums_path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _save_cache_checksum(cache_dir: Path, filename: str, sha256_hex: str) -> None:
    sums = _load_cache_checksums(cache_dir)
    sums[filename] = sha256_hex.lower()
    lines = [f"{digest}  {name}" for name, digest in sorted(sums.items())]
    sums_path = cache_dir / "SHA256SUMS.txt"
    tmp_path = cache_dir / "SHA256SUMS.txt.part"
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, sums_path)


def resolve_cached_installer(
    cache_dir: str | Path,
    asset_name: str,
    *,
    version: str = __version__,
    expected_sha256: str | None = None,
) -> Path | None:
    """Return the verified cached installer path, or None if missing/unverified."""
    try:
        safe_name = _validate_cache_asset_name(asset_name, version)
    except InviteValidationError:
        return None
    root = Path(cache_dir)
    if not root.is_dir():
        return None
    candidate = root / safe_name
    try:
        if candidate.is_symlink():
            return None
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
        if resolved_candidate.parent != resolved_root:
            return None
        info = os.lstat(candidate)
        if not stat.S_ISREG(info.st_mode):
            return None
    except OSError:
        return None

    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        expected = _load_cache_checksums(root).get(safe_name, "")
    if not _HEX64_RE.fullmatch(expected):
        return None
    try:
        actual = _sha256_regular_file(candidate)
    except OSError:
        return None
    if not hmac.compare_digest(actual, expected):
        logger.warning("Cached installer %s failed SHA-256 verification", safe_name)
        return None
    return candidate


def _default_https_opener(url: str):
    from services.http_tls import verified_context
    from services.verified_download import open_url

    return open_url(url, context=verified_context())


def fetch_and_verify_client_installer(
    asset_name: str,
    *,
    version: str = __version__,
    cache_dir: str | Path | None = None,
    opener: Callable[[str], Any] | None = None,
    release_base_url: str | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Download and SHA-256-verify a Client-Only installer into the host's LAN cache."""
    safe_name = _validate_cache_asset_name(asset_name, version)
    ver = _normalize_release_version(version)
    base = (
        release_base_url.rstrip("/")
        if release_base_url
        else f"https://github.com/{GITHUB_REPO}/releases/download/v{ver}"
    )
    target_dir = Path(cache_dir) if cache_dir is not None else default_invite_cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    open_fn = opener or _default_https_opener

    # Step 1: Fetch SHA256SUMS.txt and extract the pinned digest for safe_name.
    sums_url = f"{base}/SHA256SUMS.txt"
    with open_fn(sums_url) as resp:
        raw_sums = resp.read(64 * 1024 + 1)
    if len(raw_sums) > 64 * 1024:
        raise InviteValidationError("SHA256SUMS.txt exceeds maximum allowed size.")
    checksums = parse_sha256sums(raw_sums.decode("utf-8", errors="strict"))
    expected_sha = checksums.get(safe_name)
    if not expected_sha or not _HEX64_RE.fullmatch(expected_sha):
        raise InviteValidationError(f"SHA256SUMS.txt does not contain a checksum for {safe_name}.")

    # Already cached & verified? Return immediately.
    existing = resolve_cached_installer(
        target_dir, safe_name, version=ver, expected_sha256=expected_sha
    )
    if existing is not None:
        _save_cache_checksum(target_dir, safe_name, expected_sha)
        return existing

    # Step 2: Stream into .part file while computing SHA-256.
    asset_url = f"{base}/{safe_name}"
    part_path = target_dir / f"{safe_name}.part"
    dest_path = target_dir / safe_name
    if part_path.is_symlink() or dest_path.is_symlink():
        raise InviteValidationError("Refusing to write through a symbolic link in invite cache.")

    digest = hashlib.sha256()
    total_written = 0
    try:
        with open_fn(asset_url) as resp, open(part_path, "wb") as out:
            content_length = 0
            headers = getattr(resp, "headers", None)
            if headers is not None:
                raw_len = headers.get("Content-Length")
                if raw_len and str(raw_len).isdigit():
                    content_length = int(raw_len)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("Installer download canceled.")
                chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > MAX_INSTALLER_BYTES:
                    raise InviteValidationError("Downloaded installer exceeds maximum allowed size.")
                out.write(chunk)
                digest.update(chunk)
                if progress_callback is not None:
                    progress_callback(total_written, content_length)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        try:
            if part_path.exists() and not part_path.is_symlink():
                part_path.unlink()
        except OSError:
            pass
        raise

    actual_sha = digest.hexdigest().lower()
    if not hmac.compare_digest(actual_sha, expected_sha):
        try:
            part_path.unlink()
        except OSError:
            pass
        raise InviteValidationError(
            f"SHA-256 mismatch for {safe_name}: expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
        )

    os.replace(part_path, dest_path)
    _save_cache_checksum(target_dir, safe_name, expected_sha)
    return dest_path


# ---- Host-Served Network Invite & Download HTTP Server ----


@dataclass
class InviteSession:
    """One revocable, time-limited invite token served by ``HostInviteServer``."""

    token: str
    created_at: float
    expires_at: float
    pairing_code: str = ""
    revoked: bool = False

    def remaining_seconds(self, now: float) -> float:
        if self.revoked:
            return 0.0
        return max(0.0, self.expires_at - now)

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


_CAPABILITY_LABELS: Final[tuple[tuple[str, str, str], ...]] = (
    ("speech", "Speech Transcription", "Transcribe dictation and meetings using this host's GPU/CPU engine"),
    ("text_cleanup", "AI Cleanup & Rewrites", "Polish punctuation, grammar, and custom styles on the host"),
    ("fast_judgments", "Fast Judgments", "Low-latency routing and classification via the host"),
    ("walkthrough_writeup", "Walkthrough Write-Ups", "Generate polished walkthrough documents on the host"),
    ("meeting_ai", "Meeting AI & Speaker ID", "Speaker diarization and live meeting summaries on the host"),
    ("records", "Record Storage", "Store dictations, meetings, and walkthroughs on the host"),
    ("agent_ask", "Agent Ask", "Run quick Q&A commands via this host's coding agent"),
)


def _render_join_html(
    *,
    token: str,
    host_info: Mapping[str, Any],
    invite_link: InviteLink,
    assets: Sequence[ClientInstallerAsset],
    cached_assets: Mapping[str, bool],
    detected_os: str,
    ttl_seconds: float,
    nonce: str,
) -> str:
    """Build the self-contained dark-mode join page with strict HTML escaping."""
    host_name = html.escape(invite_link.name or str(host_info.get("host_name") or invite_link.host), quote=True)
    version = html.escape(str(host_info.get("version") or __version__), quote=True)
    raw_engine = host_info.get("engine")
    engine: Mapping[str, Any] = raw_engine if isinstance(raw_engine, Mapping) else {}
    engine_label = str(engine.get("label") or "")
    engine_device = str(engine.get("device") or "")
    if engine_label and engine_device:
        engine_text = f"{engine_label} ({engine_device})"
    elif engine_label:
        engine_text = engine_label
    else:
        engine_text = "Host speech engine"
    engine_html = html.escape(engine_text, quote=True)

    caps_obj = host_info.get("capabilities")
    raw_caps: Mapping[str, Any] = caps_obj if isinstance(caps_obj, Mapping) else {}
    cap_items: list[str] = []
    for key, label, desc in _CAPABILITY_LABELS:
        enabled = True if key == "speech" else bool(raw_caps.get(key))
        state_cls = "cap-on" if enabled else "cap-off"
        badge = "Enabled" if enabled else "Off"
        cap_items.append(
            f'<li class="cap-item {state_cls}">'
            f'<span class="cap-dot"></span>'
            f'<div class="cap-copy"><strong>{html.escape(label)}</strong>'
            f'<span>{html.escape(desc)}</span></div>'
            f'<span class="cap-badge">{badge}</span>'
            "</li>"
        )
    caps_html = "\n".join(cap_items)

    # Pick primary download matching detected OS.
    os_to_primary = {"windows": "windows", "macos": "macos", "linux": "linux_deb"}
    primary_id = os_to_primary.get(detected_os, "windows")
    primary_asset = next((a for a in assets if a.platform_id == primary_id), assets[0])

    def _asset_href(asset: ClientInstallerAsset) -> tuple[str, str]:
        if cached_assets.get(asset.filename):
            return f"/download/{urllib.parse.quote(token, safe='')}/{urllib.parse.quote(asset.filename, safe='')}", "LAN cached (verified)"
        return asset.release_url, f"GitHub Release v{version}"

    primary_href, primary_source = _asset_href(primary_asset)
    primary_btn_label = html.escape(f"Download OpenWhisper Client for {primary_asset.label}", quote=True)

    platform_links: list[str] = []
    for asset in assets:
        href, source_tag = _asset_href(asset)
        is_primary = " primary-choice" if asset.platform_id == primary_asset.platform_id else ""
        platform_links.append(
            f'<a class="dl-card{is_primary}" href="{html.escape(href, quote=True)}">'
            f'<span class="dl-title">{html.escape(asset.label)}</span>'
            f'<span class="dl-meta">{html.escape(asset.detail)} · {html.escape(source_tag)}</span>'
            "</a>"
        )
    platforms_html = "\n".join(platform_links)

    pair_uri = invite_link.uri
    pair_uri_attr = html.escape(pair_uri, quote=True)
    invite_file_href = html.escape(
        f"/invite/{urllib.parse.quote(token, safe='')}{INVITE_FILE_EXTENSION}", quote=True
    )
    formatted_code = html.escape(protocol.format_sas(invite_link.code))
    short_fp = html.escape(invite_link.short_fingerprint)
    host_addr = html.escape(invite_link.address)
    mins_left = max(1, int((ttl_seconds + 59) // 60))

    qr_svg = render_invite_qr_svg(pair_uri, module_size=5, quiet_zone=3, dark="#0f172a", light="#ffffff")
    qr_data_uri = "data:image/svg+xml;base64," + base64.b64encode(qr_svg.encode("utf-8")).decode("ascii")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect to {host_name} — OpenWhisper</title>
  <style nonce="{nonce}">
    :root {{
      --bg: #0b1120;
      --surface: #111827;
      --raised: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #7dd3fc;
      --success: #34d399;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 28px 16px 48px;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.5;
    }}
    .wrap {{ max-width: 780px; margin: 0 auto; }}
    .hero {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 16px; padding: 24px 26px; margin-bottom: 18px;
    }}
    .eyebrow {{
      display: inline-block; font-size: 12px; font-weight: 600; letter-spacing: .06em;
      text-transform: uppercase; color: var(--accent); margin-bottom: 8px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 26px; font-weight: 700; }}
    .lede {{ margin: 0; color: var(--muted); font-size: 15px; }}
    .meta-row {{
      display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px;
      font-size: 13px; color: var(--muted);
    }}
    .pill {{
      background: var(--raised); border: 1px solid var(--border);
      border-radius: 999px; padding: 5px 12px; color: var(--text);
    }}
    .card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 14px; padding: 20px 24px; margin-bottom: 16px;
    }}
    .card h2 {{ margin: 0 0 6px; font-size: 17px; font-weight: 600; }}
    .card p {{ margin: 0 0 14px; color: var(--muted); font-size: 14px; }}
    .btn-row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .btn-row-spaced {{ margin-bottom: 12px; }}
    .btn {{
      display: inline-flex; align-items: center; justify-content: center;
      padding: 11px 18px; border-radius: 10px; font-weight: 600; font-size: 14px;
      text-decoration: none; cursor: pointer; border: 1px solid var(--border);
      background: var(--raised); color: var(--text);
    }}
    .btn:hover {{ border-color: var(--accent); }}
    .btn-primary {{
      background: var(--accent); color: #082f49; border-color: transparent;
    }}
    .btn-primary:hover {{ background: var(--accent-hover); }}
    .dl-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px; margin-top: 14px;
    }}
    .dl-card {{
      display: flex; flex-direction: column; gap: 3px;
      padding: 12px 14px; border-radius: 10px; text-decoration: none;
      background: var(--raised); border: 1px solid var(--border); color: var(--text);
    }}
    .dl-card.primary-choice {{ border-color: var(--accent); }}
    .dl-card:hover {{ border-color: var(--accent-hover); }}
    .dl-title {{ font-weight: 600; font-size: 14px; }}
    .dl-meta {{ font-size: 12px; color: var(--muted); }}
    .cap-list {{ list-style: none; padding: 0; margin: 12px 0 0; display: grid; gap: 8px; }}
    .cap-item {{
      display: flex; align-items: center; gap: 12px;
      padding: 10px 12px; border-radius: 10px;
      background: var(--raised); border: 1px solid var(--border);
    }}
    .cap-off {{ opacity: .55; }}
    .cap-dot {{
      width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
      background: var(--muted);
    }}
    .cap-on .cap-dot {{ background: var(--success); }}
    .cap-copy {{ display: flex; flex-direction: column; flex: 1; font-size: 13px; }}
    .cap-copy span {{ color: var(--muted); font-size: 12px; }}
    .cap-badge {{
      font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
      background: rgba(148, 163, 184, .16); color: var(--muted);
    }}
    .cap-on .cap-badge {{ background: rgba(52, 211, 153, .16); color: var(--success); }}
    .uri-box {{
      width: 100%; padding: 10px 12px; border-radius: 8px;
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px; margin-bottom: 10px;
    }}
    .fallback-grid {{
      display: grid; grid-template-columns: 1fr auto; gap: 18px; align-items: center;
    }}
    .manual-table {{
      display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; font-size: 13px;
    }}
    .manual-table dt {{ color: var(--muted); }}
    .manual-table dd {{
      margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-weight: 600; color: var(--text);
    }}
    .qr-frame {{
      background: #ffffff; padding: 8px; border-radius: 10px;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .qr-frame img {{ display: block; width: 148px; height: 148px; }}
    .copy-status {{ font-size: 13px; color: var(--success); margin-left: 6px; }}
    @media (max-width: 600px) {{
      .fallback-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="eyebrow">OpenWhisper Host Invite · Expires in ~{mins_left} min</div>
      <h1>Connect to {host_name}</h1>
      <p class="lede">
        Install lightweight OpenWhisper Client on this computer and pair in one click —
        no local AI models, GPU drivers, or API keys needed here.
      </p>
      <div class="meta-row">
        <span class="pill">Speech engine: {engine_html}</span>
        <span class="pill">Host version: v{version}</span>
        <span class="pill">Identity: {short_fp}</span>
      </div>
    </section>

    <section class="card">
      <h2>1. Install OpenWhisper Client</h2>
      <p>
        Already have OpenWhisper installed? Skip straight to step 2.
      </p>
      <div class="btn-row">
        <a class="btn btn-primary" href="{html.escape(primary_href, quote=True)}">{primary_btn_label}</a>
        <span class="dl-meta">{html.escape(primary_source)}</span>
      </div>
      <div class="dl-grid">
        {platforms_html}
      </div>
    </section>

    <section class="card">
      <h2>2. Pair in one click</h2>
      <p>
        Includes {host_name}'s verified TLS certificate fingerprint and a fresh pairing code.
      </p>
      <div class="btn-row btn-row-spaced">
        <a class="btn btn-primary" href="{pair_uri_attr}">Connect OpenWhisper to {host_name}</a>
        <a class="btn" href="{invite_file_href}">Download .openwhisper-invite</a>
        <button type="button" class="btn" id="copyUriBtn">Copy invite link</button>
        <span class="copy-status" id="copyStatus" aria-live="polite"></span>
      </div>
      <input class="uri-box" id="inviteUriInput" type="text" readonly value="{pair_uri_attr}" aria-label="Invite link">
      <div class="fallback-grid">
        <dl class="manual-table">
          <dt>Host address</dt><dd>{host_addr}</dd>
          <dt>Pairing code</dt><dd>{formatted_code}</dd>
          <dt>Host fingerprint</dt><dd>{short_fp}</dd>
        </dl>
        <div class="qr-frame">
          <img src="{qr_data_uri}" alt="QR code to pair with {host_name}">
        </div>
      </div>
    </section>

    <section class="card">
      <h2>Host capabilities offered by {host_name}</h2>
      <p>
        Capabilities stay controlled on {host_name}. Host API keys are never shared with clients.
      </p>
      <ul class="cap-list">
        {caps_html}
      </ul>
    </section>
  </div>
  <script nonce="{nonce}">
    (function () {{
      var btn = document.getElementById("copyUriBtn");
      var input = document.getElementById("inviteUriInput");
      var status = document.getElementById("copyStatus");
      if (!btn || !input) return;
      btn.addEventListener("click", function () {{
        var text = input.value;
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(text).then(function () {{
            status.textContent = "Copied!";
          }}, function () {{
            input.select();
            status.textContent = "Press Ctrl+C / Cmd+C to copy";
          }});
        }} else {{
          input.select();
          status.textContent = "Press Ctrl+C / Cmd+C to copy";
        }}
      }});
    }})();
  </script>
</body>
</html>
"""


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class HostInviteServer:
    """Opt-in LAN/Tailscale HTTP invite and optional cached installer server."""

    def __init__(
        self,
        host_info_provider: Callable[[], Mapping[str, Any]],
        *,
        pairing_code_provider: Callable[[], str | None] | None = None,
        cache_enabled_provider: Callable[[], bool] | None = None,
        cache_dir: str | Path | None = None,
        release_base_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host_info_provider = host_info_provider
        self._pairing_code_provider = pairing_code_provider
        self._cache_enabled_provider = cache_enabled_provider or (lambda: False)
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._release_base_url = release_base_url
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, InviteSession] = {}
        self._current_token: str | None = None
        self._server: _QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._server is not None

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    def _effective_cache_dir(self) -> Path:
        return self._cache_dir if self._cache_dir is not None else default_invite_cache_dir()

    def start(self, port: int = DEFAULT_INVITE_PORT, *, bind: str = "0.0.0.0") -> int:
        """Start serving invites on ``(bind, port)``. Returns the bound port."""
        with self._lock:
            if self._server is not None:
                return self._port
            parent = self

            class _Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format: str, *args: Any) -> None:
                    logger.debug("HostInviteServer: " + format, *args)

                def do_GET(self) -> None:  # noqa: N802
                    parent._handle_get(self)

                def do_HEAD(self) -> None:  # noqa: N802
                    parent._handle_get(self, head_only=True)

                def do_POST(self) -> None:  # noqa: N802
                    parent._send_error_response(self, HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")

                def do_PUT(self) -> None:  # noqa: N802
                    parent._send_error_response(self, HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")

                def do_DELETE(self) -> None:  # noqa: N802
                    parent._send_error_response(self, HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")

            server = _QuietThreadingHTTPServer((bind, int(port)), _Handler)
            bound_port = int(server.server_address[1])
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="openwhisper-host-invite",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._port = bound_port
            thread.start()
            return bound_port

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            self._port = 0
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                logger.debug("HostInviteServer shutdown error", exc_info=True)
            try:
                server.server_close()
            except Exception:
                logger.debug("HostInviteServer close error", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def create_invite(
        self,
        *,
        ttl_seconds: float = DEFAULT_INVITE_TTL_S,
        pairing_code: str | None = None,
    ) -> InviteSession:
        """Create a fresh revocable invite token with ``ttl_seconds`` lifetime."""
        if ttl_seconds <= 0:
            raise ValueError("Invite TTL must be positive.")
        code = ""
        if pairing_code is not None:
            code = _validate_code(pairing_code)
        elif self._pairing_code_provider is not None:
            generated = self._pairing_code_provider()
            if generated:
                code = _validate_code(str(generated))
        now = self._clock()
        token = secrets.token_urlsafe(24)
        session = InviteSession(
            token=token,
            created_at=now,
            expires_at=now + float(ttl_seconds),
            pairing_code=code,
            revoked=False,
        )
        with self._lock:
            self._sessions[token] = session
            self._current_token = token
        return session

    def revoke_invite(self, token: str | None = None) -> bool:
        """Revoke ``token``, or all active tokens when ``token`` is None."""
        with self._lock:
            if token is None:
                revoked_any = False
                for existing in self._sessions.values():
                    if not existing.revoked:
                        existing.revoked = True
                        revoked_any = True
                self._current_token = None
                return revoked_any
            matched = self._lookup_session_unlocked(token)
            if matched is None:
                return False
            matched.revoked = True
            if self._current_token == matched.token:
                self._current_token = None
            return True

    def active_invite(self) -> InviteSession | None:
        """Return the current non-revoked, non-expired session, if any."""
        now = self._clock()
        with self._lock:
            if not self._current_token:
                return None
            session = self._sessions.get(self._current_token)
            if session is None or session.revoked or session.is_expired(now):
                return None
            return session

    def invite_url(self, host: str, token: str | None = None) -> str:
        """Return ``http://<host>:<port>/join/<token>`` for sharing."""
        clean_host = _validate_host(host)
        with self._lock:
            tok = token or self._current_token
            port = self._port or DEFAULT_INVITE_PORT
        if not tok:
            raise ValueError("No active invite token.")
        return f"http://{protocol.format_address(clean_host, port)}/join/{tok}"

    def _lookup_session_unlocked(self, token: str) -> InviteSession | None:
        if not _TOKEN_RE.fullmatch(token or ""):
            return None
        matched: InviteSession | None = None
        for key, session in self._sessions.items():
            if hmac.compare_digest(key, token):
                matched = session
        return matched

    def _authorize_token(self, token: str) -> tuple[InviteSession | None, HTTPStatus | None]:
        now = self._clock()
        with self._lock:
            session = self._lookup_session_unlocked(token)
            if session is None:
                return None, HTTPStatus.NOT_FOUND
            if session.revoked or session.is_expired(now):
                return None, HTTPStatus.GONE
            return session, None

    def _apply_security_headers(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        csp: str | None = None,
    ) -> None:
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header(
            "Content-Security-Policy",
            csp
            or "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'",
        )

    def _send_error_response(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
        *,
        head_only: bool = False,
    ) -> None:
        body = (message + "\n").encode("utf-8")
        handler.send_response(int(status))
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        self._apply_security_headers(handler)
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    def _build_session_invite_link(
        self,
        session: InviteSession,
        host_info: Mapping[str, Any],
        request_host: str,
    ) -> InviteLink:
        code = session.pairing_code or str(host_info.get("pairing_code") or "")
        if not code and self._pairing_code_provider is not None:
            fresh = self._pairing_code_provider()
            if fresh:
                code = str(fresh)
                with self._lock:
                    session.pairing_code = code
        known_alts: list[str] = []
        for entry in host_info.get("alternates") or ():
            try:
                known_alts.append(_validate_host(str(entry)))
            except InviteValidationError:
                pass
        primary_host = str(host_info.get("address") or "")
        if primary_host:
            try:
                primary_host = _validate_host(primary_host)
            except InviteValidationError:
                primary_host = ""
        # Prefer request_host ONLY when it is already one of this host's own
        # known addresses (e.g. client reached the invite page over Tailscale).
        # Never inject an arbitrary untrusted HTTP Host header into the invite.
        if request_host and request_host in known_alts:
            if primary_host and primary_host != request_host:
                known_alts.append(primary_host)
            primary_host = request_host
        if not primary_host:
            primary_host = known_alts[0] if known_alts else "127.0.0.1"
        return InviteLink(
            host=primary_host,
            port=int(host_info.get("port") or protocol.DEFAULT_PORT),
            fingerprint=str(host_info.get("fingerprint") or ""),
            code=code,
            name=str(host_info.get("host_name") or primary_host),
            alternates=tuple(known_alts),
        )

    def _request_hostname(self, handler: BaseHTTPRequestHandler) -> str:
        raw_header = (handler.headers.get("Host") or "").strip()
        if raw_header:
            try:
                host, _port = protocol.parse_address(raw_header)
                return _validate_host(host)
            except (ValueError, InviteValidationError):
                pass
        addr = handler.server.server_address
        return str(addr[0]) if isinstance(addr, tuple) and addr else "127.0.0.1"

    def _handle_get(self, handler: BaseHTTPRequestHandler, *, head_only: bool = False) -> None:
        raw_path = urllib.parse.urlsplit(handler.path).path
        # Reject raw traversal segments before any unquoting/routing.
        if ".." in raw_path or "\\" in raw_path or "%2e" in raw_path.lower() or "%2f" in raw_path.lower() or "%5c" in raw_path.lower():
            self._send_error_response(handler, HTTPStatus.NOT_FOUND, "Not found.", head_only=head_only)
            return

        parts = [seg for seg in raw_path.split("/") if seg]
        if len(parts) == 2 and parts[0] == "join":
            self._serve_join_page(handler, parts[1], head_only=head_only)
            return
        if len(parts) == 2 and parts[0] == "invite" and parts[1].endswith(INVITE_FILE_EXTENSION):
            token = parts[1][: -len(INVITE_FILE_EXTENSION)]
            self._serve_invite_file(handler, token, head_only=head_only)
            return
        if len(parts) == 3 and parts[0] == "download":
            self._serve_cached_download(handler, parts[1], parts[2], head_only=head_only)
            return
        self._send_error_response(handler, HTTPStatus.NOT_FOUND, "Not found.", head_only=head_only)

    def _serve_join_page(
        self,
        handler: BaseHTTPRequestHandler,
        token: str,
        *,
        head_only: bool = False,
    ) -> None:
        session, err = self._authorize_token(token)
        if err is not None or session is None:
            msg = "This invite link has expired or been revoked." if err == HTTPStatus.GONE else "Invite not found."
            self._send_error_response(handler, err or HTTPStatus.NOT_FOUND, msg, head_only=head_only)
            return

        host_info = dict(self._host_info_provider() or {})
        try:
            invite_link = self._build_session_invite_link(
                session, host_info, self._request_hostname(handler)
            )
        except InviteValidationError as exc:
            logger.warning("HostInviteServer could not build invite link: %s", exc)
            self._send_error_response(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Host sharing is not ready for invites yet.",
                head_only=head_only,
            )
            return

        version = str(host_info.get("version") or __version__)
        assets = client_installer_assets(version, release_base_url=self._release_base_url)
        cache_enabled = bool(self._cache_enabled_provider())
        cached_map: dict[str, bool] = {}
        if cache_enabled:
            cache_root = self._effective_cache_dir()
            for asset in assets:
                cached_map[asset.filename] = (
                    resolve_cached_installer(cache_root, asset.filename, version=version)
                    is not None
                )

        nonce = secrets.token_urlsafe(16)
        detected_os = detect_client_os(handler.headers.get("User-Agent") or "")
        page = _render_join_html(
            token=token,
            host_info=host_info,
            invite_link=invite_link,
            assets=assets,
            cached_assets=cached_map,
            detected_os=detected_os,
            ttl_seconds=session.remaining_seconds(self._clock()),
            nonce=nonce,
        )
        body = page.encode("utf-8")
        csp = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "img-src 'self' data:; form-action 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; object-src 'none'"
        )
        handler.send_response(int(HTTPStatus.OK))
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        self._apply_security_headers(handler, csp=csp)
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    def _serve_invite_file(
        self,
        handler: BaseHTTPRequestHandler,
        token: str,
        *,
        head_only: bool = False,
    ) -> None:
        session, err = self._authorize_token(token)
        if err is not None or session is None:
            msg = "This invite link has expired or been revoked." if err == HTTPStatus.GONE else "Invite not found."
            self._send_error_response(handler, err or HTTPStatus.NOT_FOUND, msg, head_only=head_only)
            return
        host_info = dict(self._host_info_provider() or {})
        try:
            invite_link = self._build_session_invite_link(
                session, host_info, self._request_hostname(handler)
            )
        except InviteValidationError:
            self._send_error_response(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Host sharing is not ready for invites yet.",
                head_only=head_only,
            )
            return
        body = encode_invite_file(invite_link).encode("utf-8")
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", invite_link.name).strip("-") or "openwhisper"
        filename = f"{safe_stem[:40]}{INVITE_FILE_EXTENSION}"
        handler.send_response(int(HTTPStatus.OK))
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        handler.send_header("Content-Length", str(len(body)))
        self._apply_security_headers(handler)
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    def _serve_cached_download(
        self,
        handler: BaseHTTPRequestHandler,
        token: str,
        raw_asset: str,
        *,
        head_only: bool = False,
    ) -> None:
        session, err = self._authorize_token(token)
        if err is not None or session is None:
            msg = "This invite link has expired or been revoked." if err == HTTPStatus.GONE else "Invite not found."
            self._send_error_response(handler, err or HTTPStatus.NOT_FOUND, msg, head_only=head_only)
            return
        if not self._cache_enabled_provider():
            self._send_error_response(
                handler, HTTPStatus.NOT_FOUND, "LAN installer caching is disabled.", head_only=head_only
            )
            return

        host_info = dict(self._host_info_provider() or {})
        version = str(host_info.get("version") or __version__)
        asset_name = urllib.parse.unquote(raw_asset)
        cached = resolve_cached_installer(self._effective_cache_dir(), asset_name, version=version)
        if cached is None:
            self._send_error_response(
                handler, HTTPStatus.NOT_FOUND, "Installer not available in host cache.", head_only=head_only
            )
            return

        try:
            size = cached.stat().st_size
            handler.send_response(int(HTTPStatus.OK))
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Disposition", f'attachment; filename="{cached.name}"')
            handler.send_header("Content-Length", str(size))
            self._apply_security_headers(handler)
            handler.end_headers()
            if not head_only:
                with open(cached, "rb") as stream:
                    while True:
                        chunk = stream.read(_DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
        except OSError:
            logger.debug("Client disconnected while streaming cached installer", exc_info=True)


# ---- CLI Deep-Link Parsing & Cross-Platform Single-Instance Invite IPC ----


def extract_invite_arg(argv: Sequence[str]) -> str | None:
    """Find an ``openwhisper://pair?...`` URI or ``.openwhisper-invite`` path in ``argv``."""
    for arg in argv:
        if not isinstance(arg, str):
            continue
        trimmed = arg.strip()
        if not trimmed or trimmed.startswith("-"):
            continue
        if trimmed.lower().startswith(f"{INVITE_SCHEME}://"):
            return trimmed
        if trimmed.lower().endswith(INVITE_FILE_EXTENSION):
            return trimmed
    return None


def resolve_invite_arg(arg: str) -> InviteLink:
    """Validate a CLI argument as either an invite URI or ``.openwhisper-invite`` file."""
    trimmed = (arg or "").strip()
    if trimmed.lower().startswith(f"{INVITE_SCHEME}://"):
        return parse_invite_uri(trimmed)
    if trimmed.lower().endswith(INVITE_FILE_EXTENSION):
        return load_invite_file(trimmed)
    raise InviteValidationError("Argument is neither an openwhisper://pair URI nor an invite file.")


def set_pending_startup_invite(invite: InviteLink | None) -> None:
    with _PENDING_LOCK:
        global _PENDING_STARTUP_INVITE
        _PENDING_STARTUP_INVITE = invite


def peek_pending_startup_invite() -> InviteLink | None:
    with _PENDING_LOCK:
        return _PENDING_STARTUP_INVITE


def consume_pending_startup_invite() -> InviteLink | None:
    with _PENDING_LOCK:
        global _PENDING_STARTUP_INVITE
        invite, _PENDING_STARTUP_INVITE = _PENDING_STARTUP_INVITE, None
        return invite


def _ipc_file_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _IPC_FILENAME


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Refusing to write invite IPC descriptor through a symbolic link.")
    tmp = path.with_suffix(".json.tmp")
    if tmp.is_symlink():
        raise OSError("Refusing to write temporary invite IPC descriptor through a symbolic link.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle)
            handle.flush()
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class InviteIpcServer:
    """Authenticated loopback TCP receiver for forwarding deep links to a running instance."""

    def __init__(
        self,
        data_dir: str | Path,
        on_invite: Callable[[InviteLink], None],
    ) -> None:
        self._data_dir = Path(data_dir)
        self._ipc_path = _ipc_file_path(self._data_dir)
        self._on_invite = on_invite
        self._token = secrets.token_hex(32)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._port = 0

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        port = int(sock.getsockname()[1])
        self._sock = sock
        self._port = port
        _write_private_json(
            self._ipc_path,
            {"port": port, "token": self._token, "pid": os.getpid()},
        )
        self._thread = threading.Thread(
            target=self._accept_loop, name="openwhisper-invite-ipc", daemon=True
        )
        self._thread.start()
        return port

    def _accept_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._closed:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(1.5)
                data = b""
                while b"\n" not in data and len(data) < MAX_INVITE_URI_CHARS + 512:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\n", 1)[0].decode("utf-8", errors="strict")
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    conn.sendall(b'{"ok":false}\n')
                    continue
                token = str(msg.get("token") or "")
                if not hmac.compare_digest(token, self._token):
                    conn.sendall(b'{"ok":false,"error":"unauthorized"}\n')
                    continue
                invite = parse_invite_uri(str(msg.get("uri") or ""))
                conn.sendall(b'{"ok":true}\n')
                try:
                    self._on_invite(invite)
                except Exception:
                    logger.warning("Invite IPC callback raised", exc_info=True)
            except (OSError, ValueError, InviteValidationError):
                try:
                    conn.sendall(b'{"ok":false}\n')
                except OSError:
                    pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            if self._ipc_path.exists() and not self._ipc_path.is_symlink():
                raw = json.loads(self._ipc_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("token") == self._token:
                    self._ipc_path.unlink()
        except (OSError, ValueError):
            pass


def forward_invite_to_running_instance(
    data_dir: str | Path,
    invite: InviteLink,
    *,
    timeout: float = 1.0,
) -> bool:
    """Forward ``invite`` to an already-running instance for ``data_dir``.

    Returns True when the running instance authenticated and accepted the invite.
    """
    ipc_path = _ipc_file_path(data_dir)
    try:
        if ipc_path.is_symlink() or not ipc_path.is_file():
            return False
        info = json.loads(ipc_path.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
        if not (1 <= port <= 65535) or not token:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False

    payload = (json.dumps({"token": token, "uri": invite.uri}) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(payload)
            reply = b""
            while b"\n" not in reply and len(reply) < 256:
                chunk = conn.recv(256)
                if not chunk:
                    break
                reply += chunk
        parsed = json.loads(reply.decode("utf-8", errors="strict"))
        return isinstance(parsed, dict) and parsed.get("ok") is True
    except (OSError, ValueError):
        return False


# ---- Host Fingerprint-Verified Invite Pairing ----


def pair_with_invite(
    service: Any,
    invite: InviteLink,
    *,
    confirm_replace: Callable[[Any, InviteLink], bool] | None = None,
    device_name: str | None = None,
) -> Any:
    """Pair ``service`` using ``invite`` with strict TLS fingerprint verification.

    Security invariants:
    1. If already paired to a different host (different SHA-256 fingerprint),
       refuses to overwrite unless ``confirm_replace(existing, invite)`` returns True.
    2. Probes the host's TLS certificate and checks ``hmac.compare_digest`` against
       ``invite.fingerprint`` BEFORE transmitting the pairing code, trying
       ``invite.host`` followed by ``invite.alternates``.
    3. Re-verifies the certificate fingerprint returned by ``pair_with_host``
       before committing the pairing.
    """
    from services.remote_asr import client as remote_client

    existing = service.client_pairing() if hasattr(service, "client_pairing") else None
    if existing is not None:
        existing_fp = str(getattr(existing, "fingerprint", "") or "").upper()
        if existing_fp and not hmac.compare_digest(existing_fp, invite.fingerprint):
            if confirm_replace is None or not confirm_replace(existing, invite):
                raise InvitePairingDeclined(
                    f"Already paired with {getattr(existing, 'host_name', 'another host')}. "
                    "Confirm replacing the paired host to continue."
                )

    candidates = (invite.host, *invite.alternates)
    last_error: Exception | None = None
    verified_host: str | None = None

    for candidate in candidates:
        try:
            probe = remote_client.probe_host(candidate, invite.port)
        except remote_client.RemoteUnreachable as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue
        observed_fp = str(getattr(probe, "fingerprint", "") or "").upper()
        if not observed_fp or not hmac.compare_digest(observed_fp, invite.fingerprint):
            raise remote_client.RemoteEngineError(
                f"The host at {protocol.format_address(candidate, invite.port)} presented "
                f"a different security identity ({protocol.short_fingerprint(observed_fp) or 'unknown'}) "
                f"than the invite ({invite.short_fingerprint}). Pairing was stopped."
            )
        verified_host = candidate
        break

    if verified_host is None:
        if last_error is not None:
            raise last_error
        raise remote_client.RemoteUnreachable(
            f"Couldn't reach {invite.name} at {invite.address}."
        )

    peer_name = device_name or socket.gethostname()
    result = remote_client.pair_with_host(
        verified_host,
        invite.port,
        invite.code,
        peer_name,
        tailscale=False,
        expected_fingerprint=invite.fingerprint,
    )
    paired_fp = str(getattr(result, "fingerprint", "") or "").upper()
    if not paired_fp or not hmac.compare_digest(paired_fp, invite.fingerprint):
        raise remote_client.RemoteEngineError(
            "The host's certificate changed during pairing. Pairing was stopped."
        )

    # Merge any invite alternates that the host didn't list.
    merged_alts = tuple(
        dict.fromkeys(
            a
            for a in (*(getattr(result, "alternates", ()) or ()), invite.host, *invite.alternates)
            if a and a != verified_host
        )
    )
    if hasattr(result, "token") and merged_alts != getattr(result, "alternates", ()):
        result = remote_client.PairingResult(
            token=result.token,
            device_id=result.device_id,
            host_name=result.host_name or invite.name,
            fingerprint=result.fingerprint,
            alternates=merged_alts,
            via=getattr(result, "via", "code"),
        )

    if hasattr(service, "_paired"):
        return service._paired(verified_host, invite.port, result)
    # Fallback for stand-in services in tests without _paired.
    from services.remote_asr import settings as remote_settings

    return remote_settings.save_client_pairing(verified_host, invite.port, result)


# ---- Host-Info Snapshot Builder for RemoteEngineService ----


def host_capabilities_from_settings(settings: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """Read this computer's host-offered capabilities from settings."""
    if settings is None:
        from services.settings import settings_manager

        settings = settings_manager.load_all_settings()
    caps: dict[str, bool] = {
        "speech": True,
        "records": bool(
            read_setting_compat(
                "REMOTE_HOST_KEEP_RECORDS", "remote_host_keep_records", False, settings
            )
        ),
        "model_management": bool(
            read_setting_compat(
                "REMOTE_HOST_MODEL_MANAGEMENT", "remote_host_model_management", False, settings
            )
        ),
        "mcp_control": bool(
            read_setting_compat(
                "REMOTE_HOST_MANAGE_MCP", "remote_host_manage_mcp", False, settings
            )
        ),
    }
    for cap_id, attr_name, fallback_key, _label, _desc in HOST_CAPABILITY_SPECS:
        caps[cap_id] = bool(read_setting_compat(attr_name, fallback_key, False, settings))
    return caps


def build_host_info_for_service(service: Any, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Construct the ``HostInviteServer`` info dict from a ``RemoteEngineService``."""
    state = service.host_state() if service is not None and hasattr(service, "host_state") else {}
    bind_addr = str(getattr(service, "_bind", "") or "").strip()
    alternates: list[str] = []
    if bind_addr in ("127.0.0.1", "::1"):
        primary_address = bind_addr
        alternates.append(bind_addr)
    else:
        if state.get("address"):
            alternates.append(str(state["address"]))
        tailnet = state.get("tailscale")
        if tailnet is not None and getattr(tailnet, "running", False) and getattr(tailnet, "address", ""):
            alternates.append(str(tailnet.address))
        primary_address = str(state.get("address") or (alternates[0] if alternates else ""))
    pairing = state.get("pairing")
    pairing_code = pairing[0] if isinstance(pairing, (list, tuple)) and pairing else ""
    return {
        "host_name": state.get("host_name") or socket.gethostname(),
        "version": __version__,
        "address": primary_address,
        "port": int(state.get("port") or protocol.DEFAULT_PORT),
        "fingerprint": state.get("fingerprint") or "",
        "alternates": alternates,
        "engine": state.get("engine") or {},
        "pairing_code": pairing_code,
        "capabilities": host_capabilities_from_settings(settings),
    }


def apply_host_ai_defaults(
    capabilities: Mapping[str, Any],
    *,
    enable_records: bool = False,
    manager: Any = None,
) -> dict[str, Any]:
    """Configure this client to use the paired host for available AI/storage services.

    Only updates services the host actually advertises as enabled (plus speech
    always set to ``'remote'``). Never requires local models or API keys.
    Returns the dictionary of saved settings.
    """
    from services.settings import SettingsKey

    if manager is None:
        from services.settings import settings_manager

        manager = settings_manager
    updates: dict[Any, Any] = {
        SettingsKey.SELECTED_MODEL: "remote",
    }
    if capabilities.get("text_cleanup"):
        updates[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED] = True
        updates[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] = REMOTE_HOST_PROVIDER
    if capabilities.get("fast_judgments"):
        updates[SettingsKey.TYPESAFE_ENABLED] = True
        updates[SettingsKey.TYPESAFE_PROVIDER] = REMOTE_HOST_PROVIDER
    if capabilities.get("walkthrough_writeup"):
        updates[SettingsKey.WALKTHROUGH_WRITE_UP] = True
        updates[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] = REMOTE_HOST_PROVIDER
    if capabilities.get("meeting_ai"):
        updates[SettingsKey.MEETING_AGENT_CORE] = REMOTE_HOST_PROVIDER
    if capabilities.get("speaker_diarization"):
        updates[SettingsKey.MEETING_SPEAKER_ID_BACKEND] = REMOTE_HOST_PROVIDER
    if enable_records and capabilities.get("records"):
        updates[SettingsKey.REMOTE_RECORDS_LOCATION] = "host"
    manager.update_settings(updates)
    return updates


def fetch_host_capabilities(
    service: Any,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Query the paired host's ``ready`` handshake for live ``capabilities`` and ``ai``.

    Blocking network call; invoke off the Qt UI thread. Returns
    ``{"capabilities": {...}, "ai": {...}, "engine": {...}, "host_name": str}``.
    """
    from services.remote_asr import client as remote_client
    from services.remote_asr import settings as remote_settings

    pairing = service.client_pairing() if service is not None and hasattr(service, "client_pairing") else remote_settings.load_client_pairing()
    token = remote_settings.load_client_token()
    if pairing is None or not token:
        return {"capabilities": {}, "ai": {}, "engine": {}, "host_name": ""}
    connection = remote_client.RemoteConnection(
        pairing.host,
        pairing.port,
        token,
        pairing.fingerprint,
        alternates=pairing.alternates,
        timeout=timeout,
    )
    try:
        ready = connection.connect()
    finally:
        connection.close()
    raw_caps = ready.get("capabilities")
    raw_ai = ready.get("ai")
    raw_engine = ready.get("engine")
    raw_host_info = ready.get("host")
    caps: dict[str, Any] = dict(raw_caps) if isinstance(raw_caps, dict) else {}
    ai: dict[str, Any] = dict(raw_ai) if isinstance(raw_ai, dict) else {}
    engine: dict[str, Any] = dict(raw_engine) if isinstance(raw_engine, dict) else {}
    host_info: dict[str, Any] = dict(raw_host_info) if isinstance(raw_host_info, dict) else {}
    return {
        "capabilities": caps,
        "ai": ai,
        "engine": engine,
        "host_name": str(host_info.get("name") or pairing.host_name),
    }


def _qt_confirm_replace_host(parent: Any, existing: Any, invite: InviteLink) -> bool:
    """Prompt before overwriting an existing pairing with a different host."""
    from PyQt6.QtWidgets import QMessageBox

    old_name = str(getattr(existing, "host_name", "") or "another host")
    old_fp = protocol.short_fingerprint(str(getattr(existing, "fingerprint", "") or ""))
    answer = QMessageBox.question(
        parent,
        "Switch paired host?",
        f"This computer is already paired with {old_name} ({old_fp}).\n\n"
        f"Replace it and pair with {invite.name} ({invite.short_fingerprint}) at {invite.address}?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def install_qt_file_open_filter(
    deliver: Callable[[InviteLink], None],
    *,
    qt_app: Any = None,
) -> Any:
    """Install a Qt ``FileOpen`` event filter for macOS ``openwhisper://`` deep links."""
    from PyQt6.QtCore import QEvent, QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication

    class _InviteBridge(QObject):
        invite_received = pyqtSignal(object)

        def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
            if event is not None and event.type() == QEvent.Type.FileOpen:
                try:
                    url = event.url()
                    raw = url.toString() if url and url.isValid() else ""
                    if not raw and hasattr(event, "file"):
                        raw = str(event.file() or "")
                    if raw and (
                        raw.lower().startswith(f"{INVITE_SCHEME}://")
                        or raw.lower().endswith(INVITE_FILE_EXTENSION)
                    ):
                        invite = resolve_invite_arg(raw)
                        self.invite_received.emit(invite)
                        return True
                except InviteValidationError as exc:
                    logger.warning("Ignored invalid FileOpen invite: %s", exc)
            return super().eventFilter(watched, event)

    app = qt_app if qt_app is not None else QApplication.instance()
    bridge = _InviteBridge(app)
    bridge.invite_received.connect(deliver)
    if app is not None:
        app.installEventFilter(bridge)
    return bridge


def install_qt_invite_bridge(
    *,
    data_dir: str | Path,
    deliver: Callable[[InviteLink], None],
    qt_app: Any = None,
) -> tuple[InviteIpcServer, Any]:
    """Start ``InviteIpcServer`` and macOS ``FileOpen`` URL hook bridged onto the Qt thread.

    Returns ``(ipc_server, bridge_qobject)``; keep a reference to ``bridge_qobject``
    for the lifetime of the app and call ``ipc_server.stop()`` on shutdown.
    """
    bridge = install_qt_file_open_filter(deliver, qt_app=qt_app)
    ipc_server = InviteIpcServer(data_dir, lambda invite: bridge.invite_received.emit(invite))
    ipc_server.start()
    return ipc_server, bridge

