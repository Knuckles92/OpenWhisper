"""API keys in the operating system's per-user credential store.

A key saved from Settings goes to Windows Credential Manager, the macOS
Keychain, or the freedesktop Secret Service under the service name
``OpenWhisper``, with the credential's variable name (``OPENAI_API_KEY``) as
the account. Keys never enter ``openwhisper_settings.json`` or the log.

Resolution order for every caller is saved key, then the process environment,
then the ``.env`` file. When no OS store is usable there is deliberately no
file fallback: an obfuscated file would only look secure, and the environment
path still works.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from config import env_file_path

logger = logging.getLogger(__name__)

SERVICE_NAME = "OpenWhisper"
MAX_API_KEY_LEN = 512
_MIN_MASK_SUFFIX_LEN = 12


class CredentialSource:
    """Where the active value of a credential comes from."""

    STORED = "stored"
    ENVIRONMENT = "environment"
    DOTENV = "dotenv"
    NONE = "none"


@dataclass(frozen=True)
class CredentialStoreStatus:
    available: bool
    backend_name: str
    reason: str = ""


class CredentialStoreError(RuntimeError):
    """The OS store refused an operation. The message never contains a secret."""


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class MemoryCredentialBackend:
    """Process-local backend for tests and for callers that must not touch the OS."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._values:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError(username)
        del self._values[(service, username)]


BackendFactory = Callable[[], tuple[CredentialBackend, str]]


def platform_backend_name() -> str:
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "macOS Keychain"
    return "Secret Service (GNOME Keyring or KWallet)"


def platform_backend() -> tuple[CredentialBackend, str]:
    """Pick the OS backend for this platform by name, never by discovery.

    ``keyring``'s default resolution scans entry points and a config file,
    which lets any third-party backend on the machine (including plaintext
    ones) win. Choosing the class directly keeps the trust boundary at the OS
    store. The backend's ``priority`` property is keyring's own viability probe
    and raises when the store cannot be used.
    """
    name = platform_backend_name()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Backend
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as Backend
    else:
        from keyring.backends.SecretService import Keyring as Backend
    Backend.priority  # noqa: B018 - raises RuntimeError when unusable
    return Backend(), name


def memory_backend() -> tuple[CredentialBackend, str]:
    return MemoryCredentialBackend(), "in-memory store"


class CredentialStore:
    """Thread-safe front for one credential backend with a lookup cache.

    Lookups are cached because ``connection_fingerprint`` and the Model Manager
    resolve credentials on every render; the cache is invalidated by ``set``
    and ``delete``. A change made directly in the OS store while the app runs
    is therefore picked up on the next launch.
    """

    def __init__(self, backend_factory: BackendFactory = platform_backend) -> None:
        self._factory = backend_factory
        self._backend: CredentialBackend | None = None
        self._status: CredentialStoreStatus | None = None
        self._cache: dict[str, str | None] = {}
        self._lock = threading.RLock()

    def status(self) -> CredentialStoreStatus:
        with self._lock:
            if self._status is None:
                self._status = self._probe()
            return self._status

    def _probe(self) -> CredentialStoreStatus:
        try:
            backend, name = self._factory()
        except ImportError as exc:
            logger.warning("Credential store unavailable: %s", exc)
            return CredentialStoreStatus(
                False, platform_backend_name(), "the keyring package is not installed"
            )
        except Exception as exc:
            logger.warning(
                "Credential store unavailable: %s: %s", type(exc).__name__, exc
            )
            return CredentialStoreStatus(
                False, platform_backend_name(), str(exc) or type(exc).__name__
            )
        self._backend = backend
        return CredentialStoreStatus(True, name)

    def _require_backend(self) -> CredentialBackend:
        status = self.status()
        if not status.available or self._backend is None:
            raise CredentialStoreError(
                f"{status.backend_name} is not available: {status.reason}"
            )
        return self._backend

    def get(self, name: str) -> str | None:
        name = validate_credential_name(name)
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            if not self.status().available:
                return None
            try:
                value = self._backend.get_password(SERVICE_NAME, name)  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning(
                    "Reading %s from the credential store failed: %s",
                    name, type(exc).__name__,
                )
                return None
            value = value or None
            self._cache[name] = value
            return value

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def set(self, name: str, value: str) -> None:
        name = validate_credential_name(name)
        value = validate_api_key(value)
        with self._lock:
            backend = self._require_backend()
            try:
                backend.set_password(SERVICE_NAME, name, value)
            except Exception as exc:
                logger.error(
                    "Saving %s to the credential store failed: %s",
                    name, type(exc).__name__,
                )
                raise CredentialStoreError(
                    f"Couldn't save the key to {self.status().backend_name} "
                    f"({type(exc).__name__})."
                ) from exc
            self._cache[name] = value
        logger.info("Saved %s to %s", name, self.status().backend_name)

    def delete(self, name: str) -> bool:
        """Remove a saved key. Returns False when nothing was stored."""
        from keyring.errors import PasswordDeleteError

        name = validate_credential_name(name)
        with self._lock:
            backend = self._require_backend()
            try:
                backend.delete_password(SERVICE_NAME, name)
            except PasswordDeleteError:
                self._cache[name] = None
                return False
            except Exception as exc:
                logger.error(
                    "Removing %s from the credential store failed: %s",
                    name, type(exc).__name__,
                )
                raise CredentialStoreError(
                    f"Couldn't remove the key from {self.status().backend_name} "
                    f"({type(exc).__name__})."
                ) from exc
            self._cache[name] = None
        logger.info("Removed %s from %s", name, self.status().backend_name)
        return True

    def forget_cached(self) -> None:
        with self._lock:
            self._cache.clear()


_store = CredentialStore()


def store() -> CredentialStore:
    return _store


def set_store(replacement: CredentialStore | None) -> CredentialStore:
    """Swap the process-wide store (tests). ``None`` restores the OS-backed one."""
    global _store
    previous = _store
    _store = replacement if replacement is not None else CredentialStore()
    return previous


def validate_credential_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Credential name is empty")
    return cleaned


def validate_api_key(raw: str) -> str:
    """Return a key ready to store, or raise ``ValueError`` with user-facing copy.

    Surrounding whitespace is dropped because keys are usually pasted. Anything
    non-ASCII or non-printable inside is rejected outright rather than
    repaired: a smart quote or zero-width character from a rich-text paste
    would otherwise be saved and fail every request with an opaque 401.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("Paste a key first.")
    if len(cleaned) > MAX_API_KEY_LEN:
        raise ValueError(
            f"That is longer than any API key ({MAX_API_KEY_LEN} characters max)."
        )
    for char in cleaned:
        if not (32 < ord(char) < 127):
            raise ValueError(
                "The key contains a space or a character outside plain ASCII. "
                "Copy it again from the provider's dashboard."
            )
    return cleaned


def mask_key(value: str | None) -> str:
    """Show only the last four characters; shorter keys show nothing at all."""
    if not value or len(value) < _MIN_MASK_SUFFIX_LEN:
        return "••••"
    return f"••••{value[-4:]}"


def _dotenv_values() -> dict[str, str | None]:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    try:
        path = env_file_path()
        if not os.path.isfile(path):
            return {}
        return dict(dotenv_values(path))
    except Exception as exc:
        logger.warning("Failed to read .env file: %s", exc)
        return {}


def _load_dotenv_into_environ() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file_path())
    except ImportError:
        logger.warning("python-dotenv not installed. Skipping .env file loading.")
    except Exception as exc:
        logger.warning("Failed to load .env file: %s", exc)


def resolve_credential(name: str) -> str | None:
    """Saved key, then the process environment, then ``.env``. Never logs the value.

    ``.env`` is loaded into ``os.environ`` (without overriding) so child
    processes such as the meeting sidecar inherit it exactly as before.
    """
    if not name:
        return None
    stored = _store.get(name)
    if stored:
        return stored
    value = os.getenv(name)
    if value:
        return value
    _load_dotenv_into_environ()
    return os.getenv(name) or None


def credential_source(name: str) -> str:
    """Report which layer ``resolve_credential`` would answer from."""
    if not name:
        return CredentialSource.NONE
    if _store.get(name):
        return CredentialSource.STORED
    env_value = os.getenv(name)
    dotenv_value = _dotenv_values().get(name) or None
    if env_value:
        # load_dotenv copies .env into os.environ, so a matching value is
        # the file's own entry rather than a variable the user exported.
        if dotenv_value == env_value:
            return CredentialSource.DOTENV
        return CredentialSource.ENVIRONMENT
    if dotenv_value:
        return CredentialSource.DOTENV
    return CredentialSource.NONE


def environment_shadowed(name: str) -> bool:
    """True when a saved key hides a value that the environment also provides."""
    if not name or not _store.get(name):
        return False
    return bool(os.getenv(name) or _dotenv_values().get(name))
