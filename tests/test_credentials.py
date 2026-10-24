"""The OS-backed credential store and the shared key-resolution order."""
from __future__ import annotations

import logging
import sys

import httpx
import openai
import pytest

from services import credentials
from services.credentials import (
    KEYRING_NO_RESPONSE,
    UNRESPONSIVE_KEYRING_STATUS,
    CredentialSource,
    CredentialStore,
    CredentialStoreError,
    CredentialStoreTimeout,
    credential_source,
    environment_shadowed,
    keyring_did_not_respond,
    mask_key,
    memory_backend,
    resolve_credential,
    validate_api_key,
)
from services.text_llm import (
    builtin_profile,
    credential_status,
    lookup_env_value,
    verify_api_key,
)

NAME = "OPENWHISPER_TEST_KEY"
SECRET = "sk-test-secret-value-1234abcd"


@pytest.fixture(autouse=True)
def _no_dotenv_and_clean_env(monkeypatch, tmp_path):
    """Keep the repo's real .env and shell out of these tests."""
    from services import process_env

    monkeypatch.setattr(credentials, "env_file_path", lambda: str(tmp_path / ".env"))
    # Loading .env records what it set as the app's own; keep that to the test.
    monkeypatch.setattr(process_env, "_original", dict(process_env._original))
    for name in (NAME, "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield tmp_path / ".env"


def _failing_backend():
    raise RuntimeError("no D-Bus session")


class TestSecretServiceTimeout:
    def test_run_with_timeout_returns_and_raises(self):
        import time

        assert credentials._run_with_timeout(lambda: 7, 1.0) == 7

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            credentials._run_with_timeout(boom, 1.0)
        started = time.monotonic()
        with pytest.raises(CredentialStoreTimeout, match=KEYRING_NO_RESPONSE):
            credentials._run_with_timeout(lambda: time.sleep(30), 0.2)
        assert time.monotonic() - started < 2.0


class TestCredentialStore:
    def test_round_trip_and_delete(self):
        store = CredentialStore(backend_factory=memory_backend)
        assert store.status().available
        assert store.get(NAME) is None
        store.set(NAME, f"  {SECRET}\n")
        assert store.get(NAME) == SECRET
        assert store.has(NAME)
        assert store.delete(NAME) is True
        assert store.get(NAME) is None
        assert store.delete(NAME) is False

    def test_unavailable_backend_reads_none_and_refuses_writes(self):
        store = CredentialStore(backend_factory=_failing_backend)
        status = store.status()
        assert not status.available
        assert "no D-Bus session" in status.reason
        assert store.get(NAME) is None
        with pytest.raises(CredentialStoreError):
            store.set(NAME, SECRET)
        with pytest.raises(CredentialStoreError):
            store.delete(NAME)

    def test_refresh_sees_a_store_that_appears_later(self):
        available = {"now": False}

        def factory():
            if not available["now"]:
                raise RuntimeError("no D-Bus session")
            return memory_backend()

        store = CredentialStore(backend_factory=factory)
        assert not store.status().available
        assert store.get(NAME) is None
        available["now"] = True
        assert not store.status().available
        assert store.refresh().available
        store.set(NAME, SECRET)
        assert store.get(NAME) == SECRET

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "sk-with space", "sk-tab\tinside", "sk-“smart”", "x" * 513],
    )
    def test_rejects_values_that_cannot_be_a_key(self, raw):
        with pytest.raises(ValueError):
            validate_api_key(raw)

    def test_secret_never_reaches_the_log(self, caplog):
        caplog.set_level(logging.DEBUG)
        store = CredentialStore(backend_factory=memory_backend)
        store.set(NAME, SECRET)
        store.get(NAME)
        store.delete(NAME)
        failing = CredentialStore(backend_factory=_failing_backend)
        with pytest.raises(CredentialStoreError) as excinfo:
            failing.set(NAME, SECRET)
        assert SECRET not in caplog.text
        assert SECRET not in str(excinfo.value)
        assert NAME in caplog.text

    def test_mask_shows_at_most_four_characters(self):
        assert mask_key(None) == "••••"
        assert mask_key("short") == "••••"
        assert mask_key(SECRET) == "••••abcd"

    def test_failed_read_is_not_retried_until_the_key_is_saved(self):
        class _PromptingBackend(credentials.MemoryCredentialBackend):
            reads = 0

            def get_password(self, service, username):
                self.reads += 1
                raise RuntimeError("prompt dismissed")

        backend = _PromptingBackend()
        store = CredentialStore(backend_factory=lambda: (backend, "fake"))
        assert store.get(NAME) is None
        assert store.get(NAME) is None
        assert backend.reads == 1
        store.set(NAME, SECRET)
        assert store.get(NAME) == SECRET

    def test_hung_read_gives_up_and_marks_the_store_unusable(self, monkeypatch):
        class _SilentBackend(credentials.MemoryCredentialBackend):
            def get_password(self, service, username):
                raise credentials.CredentialStoreTimeout(KEYRING_NO_RESPONSE)

        store = CredentialStore(
            backend_factory=lambda: (_SilentBackend(), "Secret Service")
        )
        assert store.get(NAME) is None
        status = store.status()
        assert not status.available
        assert status.reason == KEYRING_NO_RESPONSE
        assert keyring_did_not_respond(status)
        monkeypatch.setenv(NAME, "env-value")
        previous = credentials.set_store(store)
        try:
            assert resolve_credential(NAME) == "env-value"
        finally:
            credentials.set_store(previous)

    def test_explicit_access_request_retries_a_denied_read_without_logging_secrets(self, caplog):
        class PromptingBackend(credentials.MemoryCredentialBackend):
            denied = True
            reads = 0

            def get_password(self, service, username):
                self.reads += 1
                if self.denied:
                    raise RuntimeError(SECRET)
                return super().get_password(service, username)

        backend = PromptingBackend()
        backend.set_password(credentials.SERVICE_NAME, NAME, SECRET)
        store = CredentialStore(backend_factory=lambda: (backend, "test store"))
        assert store.get(NAME) is None
        with pytest.raises(CredentialStoreError) as error:
            store.request_access(NAME)
        assert SECRET not in str(error.value)
        assert SECRET not in caplog.text
        assert store.get(NAME) is None
        assert backend.reads == 2
        backend.denied = False
        store.request_access(NAME)
        assert store.get(NAME) == SECRET
        assert backend.reads == 3

    def test_explicit_access_request_reprobes_an_unavailable_store(self):
        backend = credentials.MemoryCredentialBackend()
        available = False

        def factory():
            if not available:
                raise RuntimeError("locked")
            return backend, "test store"

        store = CredentialStore(backend_factory=factory)
        assert not store.status().available
        available = True
        store.request_access(NAME)
        store.set(NAME, SECRET)
        assert store.get(NAME) == SECRET


class _FakeItem:
    def __init__(self, secret: str, locked: bool) -> None:
        self.secret = secret
        self.locked = locked
        self.unlock_calls = 0
        self.deleted = False

    def is_locked(self) -> bool:
        return self.locked

    def unlock(self) -> bool:
        self.unlock_calls += 1
        self.locked = False
        return False

    def get_secret(self) -> bytes:
        assert not self.locked
        return self.secret.encode()

    def delete(self) -> None:
        assert not self.locked
        self.deleted = True


class _FakeSecretStorage:
    """Just the module-level calls the lookup-only backend makes."""

    def __init__(self, items: list[_FakeItem]) -> None:
        self.items = items
        self.queries: list[dict] = []

    def dbus_init(self):
        class _Connection:
            def close(self):
                pass

        return _Connection()

    def search_items(self, connection, attributes):
        self.queries.append(attributes)
        return iter(self.items)

    def get_default_collection(self, *args, **kwargs):
        raise AssertionError("a lookup must not create the Default keyring")


class TestSecretServiceLookups:
    def _backend(self, monkeypatch, items):
        fake = _FakeSecretStorage(items)
        monkeypatch.setitem(sys.modules, "secretstorage", fake)
        return credentials._secret_service_backend_class()(), fake

    def test_missing_key_never_prompts(self, monkeypatch):
        backend, fake = self._backend(monkeypatch, [])
        assert backend.get_password("OpenWhisper", "OPENAI_API_KEY") is None
        assert fake.queries == [
            {"username": "OPENAI_API_KEY", "service": "OpenWhisper"}
        ]

    def test_unlocked_copy_is_read_before_a_locked_one(self, monkeypatch):
        locked = _FakeItem("locked-value", locked=True)
        unlocked = _FakeItem("unlocked-value", locked=False)
        backend, _ = self._backend(monkeypatch, [locked, unlocked])
        assert backend.get_password("OpenWhisper", NAME) == "unlocked-value"
        assert locked.unlock_calls == 0

    def test_saved_locked_key_asks_to_unlock(self, monkeypatch):
        item = _FakeItem(SECRET, locked=True)
        backend, _ = self._backend(monkeypatch, [item])
        assert backend.get_password("OpenWhisper", NAME) == SECRET
        assert item.unlock_calls == 1

    def test_delete_without_a_saved_key_never_prompts(self, monkeypatch):
        from keyring.errors import PasswordDeleteError

        backend, _ = self._backend(monkeypatch, [])
        with pytest.raises(PasswordDeleteError):
            backend.delete_password("OpenWhisper", NAME)
        item = _FakeItem(SECRET, locked=True)
        backend, _ = self._backend(monkeypatch, [item])
        backend.delete_password("OpenWhisper", NAME)
        assert item.deleted

    def test_search_that_never_replies_times_out(self, monkeypatch):
        import time

        class _SilentSecretStorage(_FakeSecretStorage):
            def search_items(self, connection, attributes):
                time.sleep(30)
                return iter([])

        monkeypatch.setattr(credentials, "SECRET_SERVICE_TIMEOUT_S", 0.2)
        fake = _SilentSecretStorage([])
        monkeypatch.setitem(sys.modules, "secretstorage", fake)
        credentials._secret_service_backend_class.cache_clear()
        backend = credentials._secret_service_backend_class()()
        started = time.monotonic()
        with pytest.raises(CredentialStoreTimeout, match=KEYRING_NO_RESPONSE):
            backend.get_password("OpenWhisper", NAME)
        assert time.monotonic() - started < 2.0


    def test_unlock_prompt_gets_time_to_answer(self, monkeypatch):
        import time

        class _SlowUnlockItem(_FakeItem):
            def unlock(self):
                time.sleep(0.5)  # the user typing the keyring password
                return super().unlock()

        monkeypatch.setattr(credentials, "SECRET_SERVICE_TIMEOUT_S", 0.2)
        monkeypatch.setattr(credentials, "SECRET_SERVICE_PROMPT_TIMEOUT_S", 5.0)
        monkeypatch.setitem(sys.modules, "secretstorage",
                            _FakeSecretStorage([_SlowUnlockItem(SECRET, locked=True)]))
        credentials._secret_service_backend_class.cache_clear()
        backend = credentials._secret_service_backend_class()()
        assert backend.get_password("OpenWhisper", NAME) == SECRET

    def test_prompt_wait_still_ends(self):
        import time

        def stuck_prompt():
            credentials._note_prompt()
            time.sleep(30)

        started = time.monotonic()
        with pytest.raises(CredentialStoreTimeout, match=KEYRING_NO_RESPONSE):
            credentials._run_with_timeout(stuck_prompt, 0.1, prompt_timeout_s=0.4)
        assert 0.3 < time.monotonic() - started < 2.0


class TestResolutionOrder:
    def test_saved_key_wins_over_environment(self, monkeypatch):
        monkeypatch.setenv(NAME, "env-value")
        credentials.store().set(NAME, SECRET)
        assert resolve_credential(NAME) == SECRET
        assert lookup_env_value(NAME) == SECRET
        assert credential_source(NAME) == CredentialSource.STORED
        assert environment_shadowed(NAME)

    def test_environment_is_used_when_nothing_is_saved(self, monkeypatch):
        monkeypatch.setenv(NAME, "env-value")
        assert resolve_credential(NAME) == "env-value"
        assert credential_source(NAME) == CredentialSource.ENVIRONMENT
        assert not environment_shadowed(NAME)

    def test_dotenv_is_the_last_resort(self, monkeypatch, _no_dotenv_and_clean_env):
        _no_dotenv_and_clean_env.write_text(f"{NAME}=file-value\n", encoding="utf-8")
        # Loading .env copies it into os.environ; make sure teardown removes it.
        monkeypatch.setenv(NAME, "placeholder")
        monkeypatch.delenv(NAME)
        assert credential_source(NAME) == CredentialSource.DOTENV
        assert resolve_credential(NAME) == "file-value"
        # Loaded into the environment, yet still reported as the file's value.
        assert credential_source(NAME) == CredentialSource.DOTENV

    def test_dotenv_edit_is_seen_despite_the_parse_cache(
        self, monkeypatch, _no_dotenv_and_clean_env
    ):
        import os

        env_file = _no_dotenv_and_clean_env
        env_file.write_text(f"{NAME}=first\n", encoding="utf-8")
        assert credentials._dotenv_values()[NAME] == "first"
        env_file.write_text(f"{NAME}=second-value\n", encoding="utf-8")
        stat_result = env_file.stat()
        # Force a distinct mtime even on filesystems with coarse timestamps.
        os.utime(env_file, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000))
        assert credentials._dotenv_values()[NAME] == "second-value"
        env_file.unlink()
        assert credentials._dotenv_values() == {}

    def test_nothing_set(self):
        assert resolve_credential(NAME) is None
        assert credential_source(NAME) == CredentialSource.NONE
        assert resolve_credential("") is None
        assert not keyring_did_not_respond()
        assert UNRESPONSIVE_KEYRING_STATUS

    def test_credential_status_copy_points_at_settings(self):
        profile = builtin_profile("openrouter")
        available, text = credential_status(profile)
        assert not available
        assert text == "Requires OpenRouter API key — add it in Settings → App → API keys"
        credentials.store().set("OPENROUTER_API_KEY", SECRET)
        assert credential_status(profile) == (True, "OpenRouter API key found")


class _FakeModels:
    def __init__(self, error=None):
        self.error = error

    def list(self):
        if self.error is not None:
            raise self.error
        return []


class _FakeClient:
    def __init__(self, error=None):
        self.models = _FakeModels(error)
        self.get_calls = []
        self.closed = False

    def get(self, path, *, cast_to):
        self.get_calls.append((path, cast_to))
        if self.models.error is not None:
            raise self.models.error

    def close(self):
        self.closed = True


def _status_error(cls, status):
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status, request=request, text=f"bad key {SECRET}")
    return cls("Incorrect API key provided: " + SECRET, response=response, body=None)


class TestVerifyApiKey:
    def test_success_closes_client(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(
            "services.text_llm.create_openai_client", lambda *a, **k: client
        )
        ok, detail = verify_api_key(builtin_profile("openai"), SECRET)
        assert ok and detail == "api.openai.com accepted the key."
        assert client.closed

    def test_openrouter_probes_auth_endpoint_not_public_catalog(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(
            "services.text_llm.create_openai_client", lambda *a, **k: client
        )
        ok, detail = verify_api_key(builtin_profile("openrouter"), SECRET)
        assert ok
        assert client.get_calls == [("/auth/key", httpx.Response)]
        assert "openrouter.ai" in detail

    @pytest.mark.parametrize(
        "error, expected",
        [
            (_status_error(openai.AuthenticationError, 401), "HTTP 401"),
            (_status_error(openai.PermissionDeniedError, 403), "HTTP 403"),
            (_status_error(openai.InternalServerError, 502), "HTTP 502"),
            (
                openai.APIConnectionError(
                    request=httpx.Request("GET", "https://example.test")
                ),
                "Couldn't reach",
            ),
            (ValueError(SECRET), "ValueError"),
        ],
    )
    def test_failures_report_status_class_only(self, monkeypatch, error, expected):
        client = _FakeClient(error)
        monkeypatch.setattr(
            "services.text_llm.create_openai_client", lambda *a, **k: client
        )
        ok, detail = verify_api_key(builtin_profile("openai"), SECRET)
        assert not ok
        assert expected in detail
        assert SECRET not in detail
        assert client.closed
