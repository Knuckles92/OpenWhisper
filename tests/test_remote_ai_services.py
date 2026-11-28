"""Tests for Phase 1 host-facilitated AI services, admission isolation, and provider wiring."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import text_rewrite, typesafe
from services.actions.model import AgentMode
from services.actions.runtime import ActionResult, _agent
from services.remote_asr import ai_client, protocol
from services.remote_asr import settings as remote_settings
from services.remote_asr.ai_client import (
    RemoteAICanceled,
    RemoteAIConnectionPool,
    remote_agent_ask,
    remote_text_cleanup,
    remote_walkthrough_writeup,
)
from services.remote_asr.ai_host import HostAIServices
from services.remote_asr.client import (
    RemoteConnection,
    RemoteEngineError,
    RemoteRequestError,
    pair_with_host,
)
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.service import RemoteEngineService
from services.remote_asr.tls import ensure_host_identity
from services.settings import (
    MeetingAgentCore,
    MeetingSpeakerIdBackend,
    SettingsKey,
    SettingsManager,
    TranscriptCleanupProvider,
    TypeSafeProvider,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
)
from services.transcript_cleanup import CANCELED_REASON, TranscriptCleanup
from services.walkthrough import write_up
from services.walkthrough.session import Note, WalkthroughResult
from tests.fakes.remote import _wait_for


def _make_png(path: Path, width: int = 80, height: int = 60) -> Path:
    from PyQt6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#2050a0"))
    assert image.save(str(path), "PNG")
    return path


class _DummyEngine(HostEngine):
    identity = ("fake", "ai-test")

    def __init__(self, *, block_transcribe: bool = False) -> None:
        self.block_transcribe = block_transcribe
        self.transcribe_entered = threading.Event()
        self.transcribe_release = threading.Event()
        self.transcribe_calls = 0

    def describe(self) -> dict:
        return {"family": "fake", "label": "Fake Speech", "available": True, "streaming": False}

    def transcribe(self, audio, language):
        self.transcribe_calls += 1
        if self.block_transcribe:
            self.transcribe_entered.set()
            assert self.transcribe_release.wait(5)
        return {"text": f"speech-{self.transcribe_calls}", "segments": []}

    def stream(self, session, audio, language, finish):
        return {"events": []}


class _FakeCleaner:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        reasoning: str = "off",
        cancel_event: threading.Event | None = None,
        transform=None,
        available: bool = True,
        block_event: threading.Event | None = None,
        entered_event: threading.Event | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.reasoning = reasoning
        self.cancel_event = cancel_event
        self._transform = transform
        self._available = available
        self._block = block_event
        self._entered = entered_event
        self.last_error: str | None = "not run"

    def is_available(self) -> bool:
        return self._available

    def cleanup(
        self, text: str, system_prompt=None, timeout_s=None, deadline_s=None,
    ) -> str:
        if self._entered is not None:
            self._entered.set()
        if self._block is not None:
            deadline = time.monotonic() + 5.0
            while not self._block.is_set():
                if self.cancel_event is not None and self.cancel_event.is_set():
                    self.last_error = CANCELED_REASON
                    return text
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.last_error = CANCELED_REASON
            return text
        self.last_error = None
        if self._transform is not None:
            return self._transform(text, system_prompt, self.reasoning)
        return f"CLEANED[{self.provider}:{self.model}]: {text.strip()}"

    def close(self) -> None:
        pass


class _FakeJudge:
    model = "jev-host-test"

    def __init__(self, *, answers=None, route_id: str = "typesafe") -> None:
        self.route = SimpleNamespace(id=route_id)
        self._answers = answers
        self.calls: list = []

    def ask(self, state, questions, timeout_s=None):
        self.calls.append((state, questions, timeout_s))
        if self._answers is not None:
            return self._answers
        out = {}
        for qid, spec in questions.items():
            if spec["type"] == "noul":
                out[qid] = {"type": "noul", "noul": 0.85}
            elif spec["type"] == "choice":
                first = next(iter(spec["criteria"]))
                out[qid] = {
                    "type": "choice",
                    "choice": first,
                    "confidence": 0.9,
                    "probabilities": {first: 0.9},
                }
        return out


def _start_host(
    tmp_path: Path,
    *,
    ai_services: HostAIServices | None = None,
    engine: _DummyEngine | None = None,
    meeting_ai_enabled=None,
    meeting_ai_capabilities=None,
    meeting_ai_handler=None,
    file_decode_handler=None,
    records=None,
    records_enabled=None,
    record_kinds=None,
    max_active_jobs: int = 2,
    max_queued_jobs: int = 2,
    max_active_ai_jobs: int = 2,
    max_queued_ai_jobs: int = 2,
):
    entries: list[dict] = []

    def save(updated):
        entries[:] = [dict(item) for item in updated]

    eng = engine or _DummyEngine()
    host = SpeechHost(
        engine_provider=lambda: eng,
        registry=DeviceRegistry(lambda: list(entries), save),
        identity=ensure_host_identity(str(tmp_path / "identity")),
        host_name="StudioHost",
        ai_handler=ai_services.handle if ai_services is not None else None,
        ai_capabilities=ai_services.advertised if ai_services is not None else None,
        meeting_ai_enabled=meeting_ai_enabled,
        meeting_ai_capabilities=meeting_ai_capabilities,
        meeting_ai_handler=meeting_ai_handler,
        file_decode_handler=file_decode_handler,
        records=records,
        records_enabled=records_enabled,
        record_kinds=record_kinds,
        max_active_jobs=max_active_jobs,
        max_queued_jobs=max_queued_jobs,
        max_active_ai_jobs=max_active_ai_jobs,
        max_queued_ai_jobs=max_queued_ai_jobs,
    )
    host.start(port=0, bind="127.0.0.1")
    pairing = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "LaptopA")
    client_pairing = remote_settings.ClientPairing(
        host="127.0.0.1",
        port=host.port,
        fingerprint=pairing.fingerprint,
        host_name=pairing.host_name,
        device_id=pairing.device_id,
    )
    pool = RemoteAIConnectionPool(
        pairing_loader=lambda: client_pairing,
        token_loader=lambda: pairing.token,
    )
    return host, eng, client_pairing, pairing.token, pool


# ---- 1. Settings & contract tests ----


def test_host_ai_settings_keys_defaults_and_helpers():
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.DEFAULT_INVITE_PORT == 47822

    assert SettingsKey.REMOTE_HOST_TEXT_CLEANUP == "remote_host_text_cleanup"
    assert SettingsKey.REMOTE_HOST_FAST_JUDGMENTS == "remote_host_fast_judgments"
    assert SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP == "remote_host_walkthrough_writeup"
    assert SettingsKey.REMOTE_HOST_MEETING_AI == "remote_host_meeting_ai"
    assert SettingsKey.REMOTE_HOST_AGENT_ASK == "remote_host_agent_ask"
    assert SettingsKey.REMOTE_HOST_INVITE_ENABLED == "remote_host_invite_enabled"
    assert SettingsKey.REMOTE_HOST_INVITE_PORT == "remote_host_invite_port"
    assert SettingsKey.REMOTE_HOST_INVITE_CACHE == "remote_host_invite_cache"

    assert remote_settings.host_text_cleanup({}) is False
    assert remote_settings.host_fast_judgments({}) is False
    assert remote_settings.host_walkthrough_writeup({}) is False
    assert remote_settings.host_meeting_ai({}) is False
    assert remote_settings.host_agent_ask({}) is False
    assert remote_settings.host_invite_enabled({}) is False
    assert remote_settings.host_invite_port({}) == 47822
    assert remote_settings.host_invite_port({SettingsKey.REMOTE_HOST_INVITE_PORT: 50000}) == 50000
    assert remote_settings.host_invite_port({SettingsKey.REMOTE_HOST_INVITE_PORT: 99999}) == 47822
    assert remote_settings.host_invite_cache({}) is False


def test_remote_host_provider_constants_and_resolvers():
    assert TranscriptCleanupProvider.REMOTE_HOST == "remote_host"
    assert TypeSafeProvider.REMOTE_HOST == "remote_host"
    assert TypeSafeProvider.REMOTE_HOST in TypeSafeProvider.ALL_WITH_REMOTE
    assert SettingsKey.TYPESAFE_PROVIDER.read(
        {SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.REMOTE_HOST}
    ) == "remote_host"
    assert MeetingAgentCore.REMOTE_HOST == "remote_host"
    assert MeetingAgentCore.REMOTE_HOST in MeetingAgentCore.ALL
    assert MeetingAgentCore.REMOTE_HOST not in MeetingAgentCore.INSTALLED
    assert MeetingSpeakerIdBackend.REMOTE_HOST == "remote_host"
    assert MeetingSpeakerIdBackend.REMOTE_HOST in MeetingSpeakerIdBackend.ALL
    assert MeetingAgentCore.REMOTE_HOST in write_up.AGENTS
    assert write_up.agent_name(MeetingAgentCore.REMOTE_HOST) == "Paired host"

    # Cleanup resolves "remote_host" even when no model is explicitly saved.
    cfg = {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: TranscriptCleanupProvider.REMOTE_HOST}
    assert resolve_transcript_cleanup_provider(cfg) == "remote_host"
    assert resolve_transcript_cleanup_model(cfg) == ""
    assert SettingsKey.WALKTHROUGH_WRITE_UP_AGENT.read(
        {SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: "remote_host"}
    ) == "remote_host"


def test_remote_engine_service_host_state_setters_and_grants(tmp_path, monkeypatch):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    monkeypatch.setattr("services.settings.settings_manager", manager)
    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    try:
        assert service.pairing_grants() == []
        state = service.host_state()
        for key in (
            "text_cleanup", "fast_judgments", "walkthrough_writeup",
            "meeting_ai", "agent_ask", "invite_enabled", "invite_cache",
        ):
            assert state[key] is False
        assert state["invite_port"] == 47822

        service.set_text_cleanup(True)
        service.set_fast_judgments(True)
        service.set_walkthrough_writeup(True)
        service.set_meeting_ai(True)
        service.set_agent_ask(True)
        service.set_invite_enabled(True)
        service.set_invite_port(48100)
        service.set_invite_cache(True)

        updated = service.host_state()
        assert updated["text_cleanup"] is True
        assert updated["fast_judgments"] is True
        assert updated["walkthrough_writeup"] is True
        assert updated["meeting_ai"] is True
        assert updated["agent_ask"] is True
        assert updated["invite_enabled"] is True
        assert updated["invite_port"] == 48100
        assert updated["invite_cache"] is True
        assert len(service.pairing_grants()) == 5
    finally:
        service.shutdown()


# ---- 2. Capabilities, privacy (no API key leakage), and per-request opt-in ----


def test_ready_advertises_capabilities_without_leaking_secrets(tmp_path, monkeypatch):
    secret_key = "sk-super-secret-host-key-1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-token-99999")
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.REMOTE_HOST_FAST_JUDGMENTS: True,
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: False,
        SettingsKey.REMOTE_HOST_AGENT_ASK: False,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: TranscriptCleanupProvider.OPENAI,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-4.1-mini",
        SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.TYPESAFE,
    }
    ai = HostAIServices(settings_loader=lambda: dict(host_settings), host_name="StudioHost")
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        ready = conn.connect()
        serialized = json.dumps(ready)
        assert secret_key not in serialized
        assert "ts-secret-token-99999" not in serialized
        assert "OPENAI_API_KEY" not in serialized
        assert "TYPESAFE_API_KEY" not in serialized

        caps = ready["capabilities"]
        assert caps["text_cleanup"] is True
        assert caps["fast_judgments"] is True
        assert caps["walkthrough_writeup"] is False
        assert caps["agent_ask"] is False

        ai_meta = ready["ai"]
        assert ai_meta["text_cleanup"]["enabled"] is True
        assert ai_meta["text_cleanup"]["available"] is True
        assert ai_meta["text_cleanup"]["provider"] == "openai"
        assert "gpt-4.1-mini" in ai_meta["text_cleanup"]["label"]
        assert ai_meta["fast_judgments"]["enabled"] is True
        assert ai_meta["fast_judgments"]["available"] is True
        # Disabled features leak nothing about local host configuration.
        assert ai_meta["walkthrough_writeup"] == {"enabled": False, "available": False}
        assert ai_meta["agent_ask"] == {"enabled": False, "available": False}
    finally:
        conn.close()
        pool.close_all()
        host.stop()


def test_opt_in_is_rechecked_per_request_even_on_open_connection(tmp_path):
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.REMOTE_HOST_FAST_JUDGMENTS: True,
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: True,
        SettingsKey.REMOTE_HOST_AGENT_ASK: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "qwen3",
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        cleaner_factory=lambda **kw: _FakeCleaner(**kw),
        judge_factory=lambda _s: _FakeJudge(),
        agent_ask_runner=lambda agent_id, text, **kw: ActionResult(True, "ok", output="42"),
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        conn.connect()
        assert conn.request("text_cleanup", text="hello")["text"].startswith("CLEANED")
        assert conn.request(
            "fast_judge", state={"x": 1}, questions={"q": {"type": "noul", "instructions": "y?"}},
        )["answers"]["q"]["noul"] == 0.85
        assert conn.request("agent_ask", text="2+2")["output"] == "42"

        # Revoke all opt-ins while the WebSocket stays open!
        host_settings[SettingsKey.REMOTE_HOST_TEXT_CLEANUP] = False
        host_settings[SettingsKey.REMOTE_HOST_FAST_JUDGMENTS] = False
        host_settings[SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP] = False
        host_settings[SettingsKey.REMOTE_HOST_AGENT_ASK] = False

        for op, fields in (
            ("text_cleanup", {"text": "hello"}),
            ("fast_judge", {"state": {"x": 1}, "questions": {"q": {"type": "noul", "instructions": "y?"}}}),
            ("walkthrough_writeup_start", {"markdown": "# Hi", "shots": []}),
            ("agent_ask", {"text": "2+2"}),
        ):
            with pytest.raises(RemoteRequestError) as exc:
                conn.request(op, **fields)
            assert exc.value.code == "forbidden"
    finally:
        conn.close()
        pool.close_all()
        host.stop()


def test_host_refuses_recursive_remote_host_loop(tmp_path):
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.REMOTE_HOST_FAST_JUDGMENTS: True,
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: TranscriptCleanupProvider.REMOTE_HOST,
        SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.REMOTE_HOST,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.REMOTE_HOST,
    }
    ai = HostAIServices(settings_loader=lambda: dict(host_settings), host_name="StudioHost")
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        conn.connect()
        with pytest.raises(RemoteRequestError) as exc1:
            conn.request("text_cleanup", text="loop check")
        assert exc1.value.code == "unavailable"

        with pytest.raises(RemoteRequestError) as exc2:
            conn.request("fast_judge", state={"a": 1}, questions={"q": {"type": "noul", "instructions": "?"}})
        assert exc2.value.code == "unavailable"

        with pytest.raises(RemoteRequestError) as exc3:
            conn.request("walkthrough_writeup_start", markdown="# Loop", shots=[])
        assert exc3.value.code == "unavailable"
    finally:
        conn.close()
        pool.close_all()
        host.stop()


# ---- 3. Separate non-ASR admission lane isolation ----


def test_slow_ai_calls_never_block_speech_decode_and_vice_versa(tmp_path):
    ai_entered = threading.Event()
    ai_release = threading.Event()
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "qwen3",
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        cleaner_factory=lambda **kw: _FakeCleaner(
            **kw, block_event=ai_release, entered_event=ai_entered,
        ),
        host_name="StudioHost",
    )
    engine = _DummyEngine(block_transcribe=False)
    host, _eng, pairing, token, pool = _start_host(
        tmp_path,
        ai_services=ai,
        engine=engine,
        max_active_jobs=1,
        max_queued_jobs=0,
        max_active_ai_jobs=1,
        max_queued_ai_jobs=0,
    )
    ai_conn1 = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    ai_conn2 = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    speech_conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        ai_conn1.connect()
        ai_conn2.connect()
        speech_conn.connect()

        outcome: dict = {}

        def run_slow_ai():
            outcome["ai1"] = ai_conn1.request("text_cleanup", text="slow text", timeout=10)

        t = threading.Thread(target=run_slow_ai)
        t.start()
        assert ai_entered.wait(3)

        # AI lane is full (active=1, queued=0), so a second AI request gets busy.
        with pytest.raises(RemoteRequestError) as busy:
            ai_conn2.request("text_cleanup", text="second", timeout=5)
        assert busy.value.code == "busy"
        assert busy.value.retryable

        # Speech decode lane is completely unaffected!
        speech = speech_conn.request(
            "transcribe", audio=np.zeros(1600, dtype=np.float32), timeout=5,
        )
        assert speech["text"] == "speech-1"

        ai_release.set()
        t.join(5)
        assert not t.is_alive()
        assert outcome["ai1"]["text"].startswith("CLEANED")
    finally:
        ai_release.set()
        ai_conn1.close()
        ai_conn2.close()
        speech_conn.close()
        pool.close_all()
        host.stop()


def test_full_speech_pool_does_not_block_ai_calls(tmp_path):
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "qwen3",
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        cleaner_factory=lambda **kw: _FakeCleaner(**kw),
        host_name="StudioHost",
    )
    engine = _DummyEngine(block_transcribe=True)
    host, _eng, pairing, token, pool = _start_host(
        tmp_path,
        ai_services=ai,
        engine=engine,
        max_active_jobs=1,
        max_queued_jobs=0,
        max_active_ai_jobs=2,
        max_queued_ai_jobs=2,
    )
    speech_conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        speech_conn.connect()
        speech_out: dict = {}
        t = threading.Thread(
            target=lambda: speech_out.setdefault(
                "res",
                speech_conn.request("transcribe", audio=np.zeros(1600, dtype=np.float32), timeout=5),
            )
        )
        t.start()
        assert engine.transcribe_entered.wait(3)

        # Even while speech decode is saturated, AI cleanup runs immediately.
        reply = remote_text_cleanup("hello world", pool=pool)
        assert reply["text"] == "CLEANED[ollama:qwen3]: hello world"
        assert reply["provider"] == "ollama"
        assert reply["model"] == "qwen3"
        assert isinstance(reply["host_ms"], (int, float))

        engine.transcribe_release.set()
        t.join(5)
        assert speech_out["res"]["text"] == "speech-1"
    finally:
        engine.transcribe_release.set()
        speech_conn.close()
        pool.close_all()
        host.stop()


# ---- 4. End-to-end TranscriptCleanup & text_rewrite via remote_host ----


def test_transcript_cleanup_and_text_rewrite_via_remote_host(tmp_path, monkeypatch):
    seen: list = []
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2",
    }

    def transform(text, system_prompt, reasoning):
        seen.append((text, system_prompt, reasoning))
        if "Instruction:" in (system_prompt or ""):
            return text.upper()
        return f"Fixed: {text.strip()}"

    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        cleaner_factory=lambda **kw: _FakeCleaner(**kw, transform=transform),
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    monkeypatch.setattr(ai_client, "ai_connection_pool", lambda: pool)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: token)
    try:
        cleaner = TranscriptCleanup(provider=TranscriptCleanupProvider.REMOTE_HOST, reasoning="medium")
        assert cleaner.is_available() is True
        assert cleaner.cleanup("hello um world", system_prompt="Clean this.") == "Fixed: hello um world"
        assert cleaner.last_error is None
        assert seen[-1] == ("hello um world", "Clean this.", "medium")

        # Large transcript (> 64 KiB MAX_HEADER_BYTES) travels in binary JSON payload cleanly.
        long_text = "word " * 16_000  # 80,000 chars
        assert len(long_text) > protocol.MAX_HEADER_BYTES
        cleaned_long = cleaner.cleanup(long_text, system_prompt="Clean long.")
        assert cleaned_long.startswith("Fixed: word")
        assert cleaner.last_error is None

        # text_rewrite automatically routes through TranscriptCleanup!
        client_settings = {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: TranscriptCleanupProvider.REMOTE_HOST}
        assert text_rewrite.provider_ready(client_settings) is True
        rewritten, err = text_rewrite.rewrite_standalone(
            "make me uppercase", "Make this uppercase", client_settings,
        )
        assert err is None
        assert rewritten == "MAKE ME UPPERCASE"
    finally:
        pool.close_all()
        host.stop()


def test_transcript_cleanup_remote_cancel_stops_host_and_returns_raw(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    host_settings = {
        SettingsKey.REMOTE_HOST_TEXT_CLEANUP: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "qwen3",
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        cleaner_factory=lambda **kw: _FakeCleaner(**kw, block_event=release, entered_event=entered),
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    monkeypatch.setattr(ai_client, "ai_connection_pool", lambda: pool)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: token)
    try:
        cancel = threading.Event()
        cleaner = TranscriptCleanup(
            provider=TranscriptCleanupProvider.REMOTE_HOST, cancel_event=cancel,
        )
        result_box: dict = {}
        t = threading.Thread(
            target=lambda: result_box.setdefault("text", cleaner.cleanup("original words")),
        )
        t.start()
        assert entered.wait(3)
        cancel.set()
        t.join(5)
        assert not t.is_alive()
        assert result_box["text"] == "original words"
        assert cleaner.last_error == CANCELED_REASON
    finally:
        release.set()
        pool.close_all()
        host.stop()


def test_paired_host_cleanup_is_a_route_beside_the_installed_agents(monkeypatch):
    """The class asks its route two things, can it run and run it, whoever
    cleans up: nothing in it is specific to the paired host."""
    paired = {"value": True}
    replies = iter(["  Tidy.  ", "   "])
    calls: list = []

    def fake_cleanup(text, **kwargs):
        calls.append((text, kwargs))
        return {"text": next(replies)}

    monkeypatch.setattr(ai_client, "has_remote_host", lambda settings=None: paired["value"])
    monkeypatch.setattr(ai_client, "remote_text_cleanup", fake_cleanup)
    cancel = threading.Event()
    cleaner = TranscriptCleanup(
        provider=TranscriptCleanupProvider.REMOTE_HOST, reasoning="low", cancel_event=cancel,
    )

    assert cleaner.client is None and cleaner.api_key is None
    assert cleaner.attempt_timeout_s == 120.0
    assert cleaner.cleanup("um tidy", system_prompt="Tidy this.") == "Tidy."
    assert cleaner.last_error is None
    text, sent = calls[0]
    assert text == "um tidy"
    assert sent == {
        "system_prompt": "Tidy this.", "reasoning": "low", "timeout_s": 120.0,
        "deadline_s": None, "cancel_event": cancel,
    }
    # A blank reply keeps the words, as it does from an endpoint or an agent.
    assert cleaner.cleanup("keep me") == "keep me"
    assert cleaner.last_error == "empty response"

    paired["value"] = False
    assert cleaner.is_available() is False
    assert cleaner.cleanup("raw") == "raw"
    assert cleaner.last_error == "cleanup unavailable"
    assert len(calls) == 2


def test_host_write_up_reports_a_missing_agent_under_its_own_setting(tmp_path, monkeypatch):
    """The host's job is the local one's call, so its failures read the same:
    a missing agent names the page that chooses the write-up's agent."""
    from meeting.agent.installed.core import MEETING_AGENT_SETTING
    from meeting.agent.installed.drivers import AgentUnavailable

    def missing(agent_id, **kwargs):
        raise AgentUnavailable(f"Codex is not installed. Choose another in {MEETING_AGENT_SETTING}.")

    host_settings = {
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.CODEX,
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings), write_up_runner=missing,
        host_name="StudioHost",
    )
    host, _eng, _pairing, _token, pool = _start_host(tmp_path, ai_services=ai)
    try:
        shot = _make_png(tmp_path / "shot-1.png")
        with pytest.raises(RemoteEngineError) as failed:
            remote_walkthrough_writeup(
                "Words\n\n![Screenshot 1](shot-1.png)\n", [shot],
                poll_interval_s=0.05, pool=pool,
            )
        assert write_up.WRITE_UP_AGENT_SETTING in str(failed.value)
        assert MEETING_AGENT_SETTING not in str(failed.value)
    finally:
        pool.close_all()
        host.stop()


# ---- 5. End-to-end fast_judge & typesafe wiring ----


def test_typesafe_remote_host_route_judges_and_verifies_without_client_keys(tmp_path, monkeypatch):
    fake_judge = _FakeJudge()
    host_settings = {
        SettingsKey.REMOTE_HOST_FAST_JUDGMENTS: True,
        SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.TYPESAFE,
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        judge_factory=lambda _s: fake_judge,
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    monkeypatch.setattr(ai_client, "ai_connection_pool", lambda: pool)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: token)
    monkeypatch.setattr(typesafe, "resolve_credential", lambda _name: None)
    try:
        client_settings = {
            SettingsKey.TYPESAFE_ENABLED: True,
            SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.REMOTE_HOST,
        }
        assert typesafe.key_present(client_settings) is True
        assert typesafe.is_configured(client_settings) is True
        route = typesafe.route_from_settings(client_settings)
        assert route.id == TypeSafeProvider.REMOTE_HOST
        assert route.name == "StudioHost"

        judge = typesafe.judge_from_settings(client_settings)
        assert isinstance(judge, typesafe.RemoteFastJudge)
        prob = judge.noul({"text": "should we stop?"}, "Is the speaker asking to stop?")
        assert prob == 0.85
        choice = judge.choice({"text": "pick A"}, "Which option?", {"a": "Option A", "b": "Option B"})
        assert choice is not None and choice.choice == "a" and choice.confidence == 0.9
        assert judge.usage.requests == 2 and judge.usage.failures == 0

        ok, detail = typesafe.verify_key("", route=route)
        assert ok is True
        assert "StudioHost" in detail and "jev-host-test" in detail

        # When unpaired, judge_from_settings returns None.
        monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: None)
        assert typesafe.key_present(client_settings) is False
        assert typesafe.judge_from_settings(client_settings) is None
    finally:
        pool.close_all()
        host.stop()


# ---- 6. End-to-end walkthrough write-up (inline, chunked, cancel, isolation, cleanup) ----


def test_walkthrough_writeup_remote_inline_stages_renders_and_cleans_temp_dir(
    tmp_path, monkeypatch,
):
    from meeting.agent.installed.drivers import PassOutcome

    captured: dict = {}

    def fake_runner(agent_id, **kwargs):
        folder = kwargs.get("folder")
        captured["agent_id"] = agent_id
        captured["folder"] = folder
        captured["system_prompt"] = kwargs.get("system_prompt")
        captured["user_prompt"] = kwargs.get("user_prompt")
        assert folder and os.path.isdir(folder)
        assert os.path.isfile(os.path.join(folder, "walkthrough.md"))
        assert os.path.isfile(os.path.join(folder, "shot-1.png"))
        return PassOutcome(
            ok=True,
            text=(
                "---\ntext color: purple\nheading color: #5b2a86\nfont: Georgia\n---\n\n"
                "# Polished Guide\n\nStep one.\n\n![Screen](shot-1.png)\n"
            ),
        )

    host_settings = {
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.CLAUDE_CODE,
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        write_up_runner=fake_runner,
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    monkeypatch.setattr(ai_client, "ai_connection_pool", lambda: pool)
    try:
        folder = tmp_path / "client_wt"
        folder.mkdir()
        shot1 = _make_png(folder / "shot-1.png")
        (folder / "walkthrough.md").write_text("First click settings.\n\n![Screenshot 1](shot-1.png)\n", encoding="utf-8")
        (folder / "walkthrough.pdf").write_bytes(b"%PDF spoken")
        result = WalkthroughResult(
            folder=folder,
            pdf=folder / "walkthrough.pdf",
            markdown=folder / "walkthrough.md",
            clipboard_text="First click settings.",
            history_text="First click settings. [Screenshot 1]",
            history_raw=None,
            shots=1,
            duration_s=45.0,
            first_shot=shot1,
        )

        done = write_up.write_up(
            result,
            [shot1],
            agent=MeetingAgentCore.REMOTE_HOST,
            instructions="Make it concise.",
            follow_notes=True,
            notes=[Note("use purple text", 0)],
        )
        assert done.written_up is True
        assert done.markdown == folder / "write-up.md"
        assert (folder / "write-up.pdf").is_file()
        assert (folder / "write-up.docx").is_file()
        assert "# Polished Guide" in done.markdown.read_text(encoding="utf-8")
        assert "![Screen](shot-1.png)" in done.markdown.read_text(encoding="utf-8")
        assert "Make it concise." in captured["user_prompt"]
        assert "use purple text" in captured["user_prompt"]
        assert "note for the agent" in captured["system_prompt"]

        # Host sandboxed temp folder MUST be cleaned up!
        assert _wait_for(lambda: not os.path.exists(captured["folder"]))
    finally:
        pool.close_all()
        host.stop()


def test_walkthrough_writeup_chunked_upload_validation_cancel_and_device_isolation(
    tmp_path, monkeypatch,
):
    from meeting.agent.installed.drivers import PassOutcome

    stage_dirs: list[str] = []
    started_run = threading.Event()
    release_run = threading.Event()

    def slow_runner(agent_id, **kwargs):
        images = kwargs.get("images") or []
        if images:
            stage_dirs.append(os.path.dirname(images[0]))
        started_run.set()
        cancel = kwargs.get("cancel_event")
        deadline = time.monotonic() + 5.0
        while not release_run.is_set() and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return PassOutcome(ok=False, canceled=True, error="canceled")
            time.sleep(0.02)
        return PassOutcome(ok=True, text="# Chunked Done\n\n![Shot](shot-1.png)\n")

    host_settings = {
        SettingsKey.REMOTE_HOST_WALKTHROUGH_WRITEUP: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.CODEX,
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        write_up_runner=slow_runner,
        host_name="StudioHost",
    )
    host, _eng, pairing_a, token_a, pool_a = _start_host(tmp_path, ai_services=ai)
    # Pair a second device B to verify job ownership isolation!
    pairing_b = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "LaptopB")
    conn_b = RemoteConnection("127.0.0.1", host.port, pairing_b.token, pairing_b.fingerprint)
    # Force chunked upload path by lowering inline threshold in ai_client.
    monkeypatch.setattr(ai_client, "_INLINE_SHOT_BYTES_LIMIT", 10)
    try:
        conn_b.connect()
        shot_path = _make_png(tmp_path / "shot-1.png")

        # Reject path traversal and non-PNG data on host.
        with pytest.raises(RemoteRequestError) as bad_name:
            pool_a.request(
                "walkthrough_writeup_start",
                json_body={
                    "markdown": "# Bad",
                    "shots": [{"name": "../evil.png", "data_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n1234").decode()}],
                },
            )
        assert bad_name.value.code == "bad_request"

        with pytest.raises(RemoteRequestError) as bad_png:
            pool_a.request(
                "walkthrough_writeup_start",
                json_body={
                    "markdown": "# Bad",
                    "shots": [{"name": "shot-1.png", "data_b64": base64.b64encode(b"NOT_A_PNG_DATA").decode()}],
                },
            )
        assert bad_png.value.code == "bad_request"

        # Chunked upload succeeds end-to-end when released!
        release_run.set()
        md, look, label = remote_walkthrough_writeup(
            "Words\n\n![Screenshot 1](shot-1.png)\n",
            [shot_path],
            poll_interval_s=0.05,
            pool=pool_a,
        )
        assert "# Chunked Done" in md
        assert label == "Codex"
        assert stage_dirs and _wait_for(lambda: not os.path.exists(stage_dirs[-1]))

        # Now test cancellation AND verify Device B cannot touch Device A's job!
        release_run.clear()
        started_run.clear()
        cancel_ev = threading.Event()
        err_box: dict = {}

        def run_cancelable():
            try:
                remote_walkthrough_writeup(
                    "Cancel me\n\n![Screenshot 1](shot-1.png)\n",
                    [shot_path],
                    cancel_event=cancel_ev,
                    poll_interval_s=0.05,
                    pool=pool_a,
                )
            except Exception as exc:
                err_box["exc"] = exc

        t = threading.Thread(target=run_cancelable)
        t.start()
        assert started_run.wait(3)

        # Grab active job_id from host to test cross-device access denial.
        active_job_id = next(
            jid for jid, j in ai._writeup_jobs.items() if j.status == "running"
        )
        with pytest.raises(RemoteRequestError) as denied_status:
            conn_b.request("walkthrough_writeup_status", job_id=active_job_id)
        assert denied_status.value.code == "not_found"
        with pytest.raises(RemoteRequestError) as denied_cancel:
            conn_b.request("walkthrough_writeup_cancel", job_id=active_job_id)
        assert denied_cancel.value.code == "not_found"

        cancel_ev.set()
        t.join(5)
        assert isinstance(err_box.get("exc"), RemoteAICanceled)
        assert _wait_for(lambda: not os.path.exists(stage_dirs[-1]))
    finally:
        release_run.set()
        conn_b.close()
        pool_a.close_all()
        host.stop()


# ---- 7. End-to-end agent_ask & actions/runtime.py wiring ----


def test_agent_ask_remote_and_cancel_and_action_runtime_wiring(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def fake_ask(agent_id, text, *, model="", timeout_s=120.0, cancel=None):
        if text == "slow question":
            entered.set()
            deadline = time.monotonic() + 5.0
            while not release.is_set() and time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    return ActionResult(False, "Stopped asking Claude Code.", stopped=True)
                time.sleep(0.02)
        return ActionResult(True, "Claude Code answered", output=f"Answer({agent_id},{model}): {text}")

    host_settings = {
        SettingsKey.REMOTE_HOST_AGENT_ASK: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.CLAUDE_CODE,
    }
    ai = HostAIServices(
        settings_loader=lambda: dict(host_settings),
        agent_ask_runner=fake_ask,
        host_name="StudioHost",
    )
    host, _eng, pairing, token, pool = _start_host(tmp_path, ai_services=ai)
    monkeypatch.setattr(ai_client, "ai_connection_pool", lambda: pool)
    try:
        # ActionRuntime._agent with agent="remote_host", mode=AgentMode.ASK
        params = {
            "agent": MeetingAgentCore.REMOTE_HOST,
            "mode": AgentMode.ASK,
            "surface": "auto",
            "folder": "",
            "new_folder": False,
            "include_selection": False,
            "model": "sonnet",
        }
        res = _agent(params, "What is 6 times 7?", None, threading.Event())
        assert res.ok is True
        assert res.output == "Answer(claude_code,sonnet): What is 6 times 7?"

        # Handoff mode is refused cleanly on remote_host.
        handoff_res = _agent({**params, "mode": AgentMode.HANDOFF}, "hello", None, threading.Event())
        assert handoff_res.ok is False
        assert "Quick answer" in handoff_res.message

        # Cancellation stops the running host ask.
        cancel = threading.Event()
        box: dict = {}
        t = threading.Thread(
            target=lambda: box.setdefault(
                "res", remote_agent_ask("slow question", cancel_event=cancel, pool=pool),
            ),
        )
        t.start()
        assert entered.wait(3)
        cancel.set()
        t.join(5)
        assert not t.is_alive()
        assert box["res"]["ok"] is False
        assert box["res"]["stopped"] is True
    finally:
        release.set()
        pool.close_all()
        host.stop()


# ---- 8. Pluggable Phase 2 slots (meeting_ai, file_decode, walkthrough_records) ----


def test_pluggable_phase2_slots_advertise_and_dispatch(tmp_path):
    meeting_enabled = [False]
    meeting_calls: list = []
    decode_calls: list = []

    def meeting_handler(op, header, payload, device):
        meeting_calls.append((op, len(payload), device["id"]))
        if op == "speaker_embed":
            audio = protocol.payload_audio(payload)
            return {"embedding": [1.0, 0.0], "samples": len(audio)}
        return {"op": op, "ok": True}

    def file_decode_handler(op, header, payload, device):
        decode_calls.append((op, len(payload), device["id"]))
        return {"op": op, "bytes": len(payload)}

    host, _eng, pairing, token, pool = _start_host(
        tmp_path,
        meeting_ai_enabled=lambda: meeting_enabled[0],
        meeting_ai_capabilities=lambda: {"meeting_ai": True, "speaker_diarization": True},
        meeting_ai_handler=meeting_handler,
        file_decode_handler=file_decode_handler,
        records=lambda op, header, payload, device: {"op": op},
        records_enabled=lambda: True,
        record_kinds=lambda: ("dictation", "meeting", "walkthrough"),
    )
    conn = RemoteConnection("127.0.0.1", host.port, token, pairing.fingerprint)
    try:
        ready = conn.connect()
        assert ready["capabilities"]["meeting_ai"] is False
        assert ready["capabilities"]["speaker_diarization"] is False
        assert ready["capabilities"]["file_decode"] is True
        assert ready["capabilities"]["walkthrough_records"] is True

        # Disabled meeting_ai refuses speaker_embed and meeting_agent_turn per request.
        with pytest.raises(RemoteRequestError) as forbidden:
            conn.request("speaker_embed", audio=np.zeros(1600, dtype=np.float32))
        assert forbidden.value.code == "forbidden"

        # Turn meeting_ai ON dynamically — rechecked immediately without reconnect!
        meeting_enabled[0] = True
        emb = conn.request("speaker_embed", audio=np.zeros(1600, dtype=np.float32))
        assert emb == {"embedding": [1.0, 0.0], "samples": 1600}
        turn = conn.request("meeting_agent_turn", payload=protocol.pack_json_payload({"x": 1}))
        assert turn == {"op": "meeting_agent_turn", "ok": True}

        # File decode binary chunks work cleanly.
        dec = conn.request("file_decode_put", payload=b"\x01\x02\x03\x04", upload_id="u1")
        assert dec == {"op": "file_decode_put", "bytes": 4}
        assert len(meeting_calls) == 2
        assert len(decode_calls) == 1
    finally:
        conn.close()
        pool.close_all()
        host.stop()


def test_remote_engine_service_pluggable_registrations(tmp_path, monkeypatch):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    monkeypatch.setattr("services.settings.settings_manager", manager)
    service = RemoteEngineService(lambda: _DummyEngine(), identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    try:
        service.register_record_kind("walkthrough", object())
        assert "walkthrough" in service.record_kinds()
        service.register_meeting_ai_handler(lambda *a: {"ok": True}, capabilities=lambda: {"meeting_ai": True, "speaker_diarization": True})
        service.register_file_decode_handler(lambda *a: {"ok": True})
        host = service._ensure_host()
        assert host._meeting_ai_handler is not None
        assert host._file_decode_handler is not None
        assert "walkthrough" in host._advertised_record_kinds()
    finally:
        service.shutdown()
