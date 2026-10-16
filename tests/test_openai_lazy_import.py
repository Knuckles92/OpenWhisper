"""The openai SDK stays unloaded until a session actually sends a request.

It measured about 0.7 s of a 1.3 s startup import, and a Local Whisper,
Parakeet, Nemotron or Remote session never needs it unless AI cleanup or the
API engine is in use.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# At collection, before test_application_controller swaps stubs into
# sys.modules; see test_engine_warmup.
from services import application_controller
from services.application_controller import ApplicationController
from services.cleanup_profiles import cleanup_may_run
from services.settings import SettingsKey
from services.transcript_cleanup import TranscriptCleanup
from transcriber.openai_backend import OpenAIBackend

REPO = Path(__file__).resolve().parents[1]


def _fresh_interpreter(code: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, '.');" + code],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.split()


def test_startup_imports_leave_openai_unloaded():
    """Nor do Settings or the meeting dashboard, which a Remote host imports."""
    assert _fresh_interpreter(
        "import services.application_controller, ui_qt.ui_controller;"
        "print(int('openai' in sys.modules));"
        "import ui_qt.dialogs.settings_dialog, meeting.web.server;"
        "print(int('openai' in sys.modules))"
    ) == ["0", "0"]


def test_startup_clients_with_saved_keys_leave_openai_unloaded():
    """The API adapter never imports the SDK into the main process."""
    assert _fresh_interpreter(
        "from transcriber.openai_backend import OpenAIBackend;"
        "from services.transcript_cleanup import TranscriptCleanup;"
        "from services import credentials;"
        "credentials.set_store(credentials.CredentialStore(backend_factory=credentials.memory_backend));"
        "credentials.store().set('OPENAI_API_KEY', 'sk-test');"
        "backend = OpenAIBackend('api');"
        "TranscriptCleanup(provider='openai', api_key='sk-test', defer_client=True);"
        "print(int('openai' in sys.modules), int(backend.is_available()));"
        "backend.prepare_client();"
        "print(int('openai' in sys.modules), int(backend.client is not None))"
    ) == ["0", "1", "0", "1"]


def _save_key(name, value):
    from services import credentials

    credentials.store().set(name, value)


@pytest.fixture
def built(monkeypatch):
    """API keys the isolated-client factory received, in order."""
    keys = []

    class FakeOpenAI:
        def __init__(self, api_key, base_url=None, read_timeout=None):
            keys.append(api_key)
            self.api_key = api_key
            self.base_url = base_url
            self.read_timeout = read_timeout

        def close(self):
            pass

    monkeypatch.setattr("services.isolated.IsolatedOpenAIClient", FakeOpenAI)
    _save_key("OPENAI_API_KEY", "sk-test")
    return keys


class TestApiBackendClient:
    def test_available_before_the_client_exists(self, built):
        backend = OpenAIBackend("api")
        # Recording start asks on the UI thread; that must not build it.
        assert backend.is_available()
        assert built == []
        assert backend.client is None

    def test_first_request_builds_it_once(self, built, monkeypatch):
        backend = OpenAIBackend("api")
        monkeypatch.setattr(backend, "large_file_size_mb", lambda _path: None)
        monkeypatch.setattr(
            backend, "_transcribe_file",
            lambda _path, _endpoint, _model: backend.client.api_key,
        )
        assert backend.transcribe("a.wav") == "sk-test"
        assert backend.transcribe("b.wav") == "sk-test"
        assert built == ["sk-test"]

    def test_saved_key_reaches_the_next_request(self, built):
        backend = OpenAIBackend("api")
        backend.prepare_client()
        _save_key("OPENAI_API_KEY", "sk-new")
        # Saving in Settings neither imports nor builds anything.
        assert built == ["sk-test"]
        assert backend.is_available()
        backend.prepare_client()
        assert backend.client.api_key == "sk-new"

    def test_removed_key_is_unavailable(self, built):
        from services import credentials

        backend = OpenAIBackend("api")
        backend.prepare_client()
        credentials.store().delete("OPENAI_API_KEY")
        assert not backend.is_available()

    def test_chosen_server_reaches_the_next_request(self, built):
        from services import speech_endpoints

        backend = OpenAIBackend("api")
        backend.prepare_client()
        speech_endpoints.save_selection("speaches")
        # A local server needs no key; the client is rebuilt for its address.
        assert backend.is_available()
        backend.prepare_client()
        assert backend.client.base_url == "http://127.0.0.1:8000/v1"
        assert backend.client.read_timeout == speech_endpoints.SELF_HOSTED_TIMEOUT_S
        assert built == ["sk-test", speech_endpoints.api_key(
            speech_endpoints.selected_endpoint())]

    def test_failed_build_is_unavailable(self, monkeypatch):
        def broken(**_connection):
            raise RuntimeError("no SDK")

        monkeypatch.setattr("services.isolated.IsolatedOpenAIClient", broken)
        _save_key("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend("api")
        backend.prepare_client()
        assert not backend.is_available()
        with pytest.raises(Exception, match="not available"):
            backend.transcribe("a.wav")

    def test_build_in_progress_counts_as_available(self, monkeypatch):
        """A warm-up and a hotkey press racing build one client, never refuse."""
        started, release, keys = threading.Event(), threading.Event(), []

        class SlowOpenAI:
            def __init__(self, api_key, **_connection):
                keys.append(api_key)
                started.set()
                release.wait(5)

        monkeypatch.setattr("services.isolated.IsolatedOpenAIClient", SlowOpenAI)
        _save_key("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend("api")
        workers = [threading.Thread(target=backend.prepare_client) for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            assert started.wait(5)
            assert backend.is_available()
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        assert keys == ["sk-test"]
        assert backend.client is not None


def test_deferred_cleanup_builds_its_client_on_first_configure(monkeypatch):
    monkeypatch.setattr(
        "services.transcript_cleanup.find_api_key", lambda _provider: "sk-test"
    )
    with patch("services.transcript_cleanup.create_openai_client") as create:
        cleaner = TranscriptCleanup(provider="openai", defer_client=True)
        assert cleaner.client is None
        assert not create.called
        cleaner.configure("openai", "gpt-test")
        assert create.call_count == 1
        assert cleaner.is_available()
        cleaner.configure("openai", "gpt-test")
        assert create.call_count == 1


@pytest.mark.parametrize("settings, expected", [
    ({}, False),
    ({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}, True),
    ({SettingsKey.QUICK_RECORD_PROFILE: "email"}, True),
    ({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [
        {"id": "memo", "name": "Memo", "instructions": "Tidy.", "hotkey": "ctrl+alt+m"},
    ]}, True),
    ({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [
        {"id": "memo", "name": "Memo", "instructions": "Tidy."},
    ]}, False),
])
def test_cleanup_may_run(settings, expected):
    assert cleanup_may_run(settings) is expected


@pytest.fixture
def warmups(monkeypatch):
    """(name, target) of each thread the warm-up starts, without running it."""
    started = []

    def thread(target, name, daemon):
        return SimpleNamespace(start=lambda: started.append((name, target)))

    monkeypatch.setattr(application_controller, "threading", SimpleNamespace(Thread=thread))
    return started


def _warm(backend, monkeypatch, settings):
    monkeypatch.setattr(
        application_controller.settings_manager, "load_all_settings", lambda: settings
    )
    ApplicationController._warm_openai_sdk(SimpleNamespace(current_backend=backend))


def test_api_session_builds_its_client_in_the_background(warmups, monkeypatch):
    backend = SimpleNamespace(prepare_client=lambda: None)
    _warm(backend, monkeypatch, {})
    assert warmups == [("openai-sdk-warmup", backend.prepare_client)]


def test_cleanup_session_imports_the_sdk_in_the_background(warmups, monkeypatch):
    _warm(SimpleNamespace(), monkeypatch, {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
    assert warmups == [("openai-sdk-warmup", application_controller._import_openai_sdk)]


def test_session_using_neither_never_loads_it(warmups, monkeypatch):
    _warm(SimpleNamespace(), monkeypatch, {})
    assert warmups == []
