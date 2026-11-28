"""End-to-end integration tests across Phases 1-4 on real loopback sockets.

Exercises the full integrated stack through ``RemoteEngineService``:
- ``SpeechHost`` (TLS WebSocket server on 127.0.0.1)
- ``HostInviteServer`` (HTTP ``/join/<token>`` + ``/invite/<token>.openwhisper-invite``)
- Fingerprint-pinned pairing (``pair_with_host(..., expected_fingerprint=...)`` and
  ``pair_with_invite`` closing the probe->pair TOCTOU window)
- ``HostAIServices`` (text cleanup, fast judgments, walkthrough write-up, agent ask)
- ``HostMeetingServices`` (speaker embeddings, meeting agent turn with op replay,
  chunked compressed audio file decode / probe / transcribe)
- ``WalkthroughRecords`` via ``HostRecordStore`` (``RECORD_KINDS`` includes ``walkthrough``)
- ``main.py --self-test`` under both ``OPENWHISPER_EDITION=full`` and ``client``
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from meeting.agent.installed.drivers import PassOutcome
from meeting.interfaces import AgentConfig, AgentResult, CheckpointPayload
from meeting.reinsight import StoreToolHost
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from services.actions.runtime import ActionResult
from services.remote_asr import discovery, invite, protocol
from services.remote_asr import settings as remote_settings
from services.remote_asr.ai_client import (
    RemoteAIConnectionPool,
    remote_agent_ask,
    remote_fast_judge,
    remote_text_cleanup,
    remote_walkthrough_writeup,
)
from services.remote_asr.ai_host import HostAIServices
from services.remote_asr.client import (
    RemoteConnection,
    RemoteEngineError,
    pair_with_host,
)
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.meeting_client import (
    RemoteAgentCore,
    RemoteMeetingClient,
    decode_audio_windows_remote,
    probe_audio_remote,
    transcribe_audio_file_remote,
)
from services.remote_asr.meeting_host import HostMeetingServices
from services.remote_asr.service import RemoteEngineService
from services.remote_asr.tls import ensure_host_identity
from services.remote_records.host_store import (
    MANIFEST,
    RECORD_KINDS,
    HostRecordStore,
    file_sha256,
    safe_kind,
)
from services.remote_records.walkthrough_kind import WalkthroughRecords
from services.settings import (
    MeetingAgentCore,
    SettingsKey,
    SettingsManager,
    TranscriptCleanupProvider,
    TypeSafeProvider,
)
from tests.fakes.remote import no_real_tailscale  # noqa: F401

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float64) / rate
    pcm = np.clip(np.sin(2.0 * np.pi * 440.0 * t) * 12000.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return path


class _DummySpeechEngine:
    identity = ("e2e", "parakeet-v3")
    accepts_phrases = False

    def describe(self) -> dict[str, Any]:
        return {
            "family": "parakeet",
            "model": "v3",
            "label": "Parakeet V3",
            "device": "cuda",
            "available": True,
            "streaming": False,
        }

    def transcribe(self, audio, language=None) -> dict[str, Any]:
        dur = round(len(audio) / float(protocol.SAMPLE_RATE), 2)
        return {
            "text": f"transcribed {dur}s",
            "segments": [{"start": 0.0, "end": dur, "text": f"transcribed {dur}s"}],
        }

    def stream(self, session, audio, language, finish) -> dict[str, Any]:
        return {"events": []}

    def cancel_stream(self, session) -> None:
        pass


class _FakeCleaner:
    def __init__(self, *, provider: str, model: str, **_kwargs: Any) -> None:
        self.provider = provider
        self.model = model
        self.last_error: str | None = None

    def is_available(self) -> bool:
        return True

    def cleanup(self, text: str, **_kwargs: Any) -> str:
        self.last_error = None
        return f"Polished({self.provider}/{self.model}): {text.strip()}"

    def close(self) -> None:
        pass


class _FakeJudge:
    model = "jev-e2e"
    route = SimpleNamespace(id="typesafe")

    def ask(self, state: Any, questions: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        del state, timeout_s
        out: dict[str, Any] = {}
        for qid, spec in questions.items():
            if spec["type"] == "noul":
                out[qid] = {"type": "noul", "noul": 0.91}
            elif spec["type"] == "choice":
                choice = next(iter(spec["criteria"]))
                out[qid] = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 0.94,
                    "probabilities": {choice: 0.94},
                }
        return out


class _FakeEmbedder:
    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.available = True

    def embed(self, audio_f32_16k: np.ndarray) -> np.ndarray:
        arr = np.asarray(audio_f32_16k, dtype=np.float32).reshape(-1)
        sign = 1.0 if float(np.mean(arr)) >= 0.0 else -1.0
        vec = np.array([3.0 * sign, 4.0, 0.0, 0.0], dtype=np.float32)
        return vec


class _FakeMeetingCore:
    def __init__(self) -> None:
        self.tools: Any = None
        self.shutdown_called = False

    def set_activity_callback(self, cb) -> None:
        self._cb = cb

    def initialize(self, cfg: AgentConfig, tools: Any) -> None:
        self.cfg = cfg
        self.tools = tools

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        seg_id = payload.new_segments[0]["id"]
        # Ensure host knowledge/recall tools stay disabled for paired clients.
        assert self.tools.search_past_meetings(query="private")["disabled"] is True
        assert self.tools.search_context_files(query="private")["disabled"] is True
        p_res = self.tools.apply_agent_ops([{
            "op": "upsert_participant",
            "display_name": "Elena",
            "kind": "others_cluster",
            "is_provisional": True,
            "evidence": [seg_id],
        }])
        assert p_res[0].ok
        a_res = self.tools.apply_agent_ops([{
            "op": "add_item",
            "card": "action_items",
            "text": "Publish the client-only installer",
            "data": {"owner_participant_id": p_res[0].target_id, "deadline": "Monday"},
            "evidence": [seg_id],
        }])
        assert a_res[0].ok
        return AgentResult(
            ok=True,
            op_results=[*p_res, *a_res],
            usage={"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75, "requests": 1},
        )

    def cancel(self) -> None:
        pass

    def is_healthy(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def _upload_walkthrough_over_connection(
    conn: RemoteConnection,
    kind_handler: WalkthroughRecords,
    record_id: str,
) -> dict[str, Any]:
    bundle = kind_handler.bundle(None, record_id)
    manifest_bytes = bundle.manifest_bytes()
    file_entries: list[tuple[str, bytes]] = [(MANIFEST, manifest_bytes)]
    for rel_name, path in sorted(bundle.files.items()):
        file_entries.append((rel_name, Path(path).read_bytes()))

    total_bytes = sum(len(data) for _, data in file_entries)
    conn.request(
        "records_begin",
        kind="walkthrough",
        record_id=record_id,
        files=len(file_entries),
        bytes=total_bytes,
    )
    for rel_name, data in file_entries:
        sha = hashlib.sha256(data).hexdigest()
        conn.request(
            "records_put",
            kind="walkthrough",
            record_id=record_id,
            name=rel_name,
            size=len(data),
            sha256=sha,
            offset=0,
            payload=data,
        )
    return conn.request("records_commit", kind="walkthrough", record_id=record_id)


# ---- Unit checks for integration contracts ----


def test_host_record_store_natively_accepts_walkthrough_kind(tmp_path):
    assert "walkthrough" in RECORD_KINDS
    assert safe_kind("walkthrough") == "walkthrough"
    store = HostRecordStore(str(tmp_path / "records"), {})
    with pytest.raises(ValueError, match="Unknown kind"):
        store.handle("records_list", {"kind": "walkthrough"}, b"", {"id": "dev1"})


def test_speech_host_ready_advertises_file_decode_from_meeting_capabilities(tmp_path):
    store = []
    registry = DeviceRegistry(lambda: list(store), lambda items: (store.clear(), store.extend(items)))
    file_decode_flag = [False]
    host = SpeechHost(
        engine_provider=lambda: _DummySpeechEngine(),
        registry=registry,
        identity=ensure_host_identity(str(tmp_path / "id")),
        host_name="CapHost",
        meeting_ai_enabled=lambda: True,
        meeting_ai_capabilities=lambda: {
            "meeting_ai": True,
            "speaker_diarization": False,
            "file_decode": file_decode_flag[0],
        },
        meeting_ai_handler=lambda *a: {},
        file_decode_handler=lambda *a: {},
        records_enabled=lambda: True,
        records=lambda *a: {},
        record_kinds=lambda: ("dictation", "meeting", "walkthrough"),
    )
    host.start(port=0, bind="127.0.0.1")
    try:
        pairing = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "Laptop")
        conn1 = RemoteConnection("127.0.0.1", host.port, pairing.token, pairing.fingerprint)
        try:
            ready1 = conn1.connect()
            assert ready1["capabilities"]["file_decode"] is False
            assert ready1["capabilities"]["meeting_ai"] is True
            assert ready1["capabilities"]["speaker_diarization"] is False
            assert ready1["capabilities"]["walkthrough_records"] is True
        finally:
            conn1.close()

        file_decode_flag[0] = True
        conn2 = RemoteConnection("127.0.0.1", host.port, pairing.token, pairing.fingerprint)
        try:
            ready2 = conn2.connect()
            assert ready2["capabilities"]["file_decode"] is True
        finally:
            conn2.close()
    finally:
        host.stop()


def test_pair_with_host_expected_fingerprint_blocks_toctou_before_sending_code(tmp_path):
    store = []
    registry = DeviceRegistry(lambda: list(store), lambda items: (store.clear(), store.extend(items)))
    host = SpeechHost(
        engine_provider=lambda: _DummySpeechEngine(),
        registry=registry,
        identity=ensure_host_identity(str(tmp_path / "id")),
        host_name="PinnedHost",
    )
    host.start(port=0, bind="127.0.0.1")
    try:
        code = host.open_pairing()
        real_fp = host.identity.fingerprint
        wrong_fp = ("0" if real_fp[0] != "0" else "1") + real_fp[1:]

        with pytest.raises(RemoteEngineError, match="different security identity"):
            pair_with_host(
                "127.0.0.1",
                host.port,
                code,
                "Laptop",
                expected_fingerprint=wrong_fp,
            )
        # The pairing code was never sent and remains active & unconsumed!
        assert host.pairing_status() is not None
        assert host.pairing_status()[0] == code

        # Matching fingerprint succeeds cleanly.
        paired = pair_with_host(
            "127.0.0.1",
            host.port,
            code,
            "Laptop",
            expected_fingerprint=real_fp.lower(),
        )
        assert paired.fingerprint == real_fp
        assert host.pairing_status() is None
    finally:
        host.stop()


# ---- Full Cross-Phase End-to-End Integration Test ----


def test_full_client_host_e2e_over_real_remote_engine_service(tmp_path, monkeypatch):
    """End-to-end journey: Invite HTTP -> Fingerprint Pair -> AI + Meeting + File Decode + Walkthrough Sync."""
    host_settings_mgr = SettingsManager(str(tmp_path / "host_settings.json"))
    monkeypatch.setattr("services.settings.settings_manager", host_settings_mgr)
    monkeypatch.setattr("services.remote_asr.ai_host.settings_manager", host_settings_mgr)
    monkeypatch.setattr(discovery, "DISCOVERY_PORT", 0)

    # Keep client credentials isolated from real OS keyring.
    cred_store = _FakeCredentialStore()
    monkeypatch.setattr("services.credentials.store", lambda: cred_store)
    monkeypatch.setattr("services.credentials.resolve_credential", cred_store.get)
    # Prevent host secrets from leaking into ready / replies.
    secret_key = "sk-e2e-never-leak-this-secret-9999"
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)

    # Configure host AI providers & enable all host capabilities.
    host_settings_mgr.update_settings({
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: TranscriptCleanupProvider.OPENAI,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-4.1-mini",
        SettingsKey.TYPESAFE_PROVIDER: TypeSafeProvider.TYPESAFE,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.CLAUDE_CODE,
        SettingsKey.REMOTE_HOST_PORT: _free_tcp_port(),
        SettingsKey.REMOTE_HOST_INVITE_PORT: _free_tcp_port(),
    })

    # Host Phase 1 AI services (real dispatcher, deterministic runners).
    writeup_stage_dirs: list[str] = []

    def fake_writeup_runner(agent_id: str, **kwargs: Any) -> PassOutcome:
        folder = str(kwargs.get("folder") or "")
        writeup_stage_dirs.append(folder)
        assert folder and os.path.isdir(folder)
        assert os.path.isfile(os.path.join(folder, "walkthrough.md"))
        assert os.path.isfile(os.path.join(folder, "step-1.png"))
        return PassOutcome(
            ok=True,
            text=(
                "---\ntext color: navy\nfont: Georgia\n---\n\n"
                f"# E2E Guide ({agent_id})\n\nPolished walkthrough.\n\n![Step 1](step-1.png)\n"
            ),
        )

    def fake_agent_ask(agent_id: str, text: str, *, model: str = "", timeout_s: float = 60.0, cancel=None):
        del model, timeout_s, cancel
        return ActionResult(True, "Answered", output=f"HostAsk[{agent_id}]: {text}")

    ai_services = HostAIServices(
        settings_loader=host_settings_mgr.load_all_settings,
        cleaner_factory=_FakeCleaner,
        judge_factory=lambda _s: _FakeJudge(),
        write_up_runner=fake_writeup_runner,
        agent_ask_runner=fake_agent_ask,
        host_name="StudioE2E",
    )

    # Host Phase 2 Meeting AI, Speaker ID, and File Decode (real dispatcher).
    speaker_model = tmp_path / "wespeaker.onnx"
    speaker_model.write_bytes(b"fake-wespeaker-weights")
    meeting_core = _FakeMeetingCore()
    wav_sample = _write_wav(tmp_path / "host_decode_source.wav", seconds=0.5)
    pcm_window = np.linspace(-0.25, 0.25, 8000, dtype=np.float32)

    meeting_services = HostMeetingServices(
        meeting_ai_enabled=remote_settings.host_meeting_ai,
        speaker_model_path_fn=lambda: str(speaker_model),
        embedder_factory=_FakeEmbedder,
        route_resolver=lambda: SimpleNamespace(
            kind="pi", provider="openai", model="gpt-4.1", endpoint=None, payload_dir=None
        ),
        core_factory=lambda _kind, _payload_dir: meeting_core,
        engine_provider=lambda: _DummySpeechEngine(),
        file_decode_root=str(tmp_path / "host_file_decode"),
        windows_fn=lambda _path: [(0.0, pcm_window), (0.5, pcm_window * 0.5)],
        probe_fn=lambda _path: (1.0, 16000, 1),
    )

    # Isolated WalkthroughRecords root on host so real user captures aren't touched.
    host_captures = tmp_path / "host_captures"
    host_walkthroughs = WalkthroughRecords(str(host_captures))

    service = RemoteEngineService(
        lambda: _DummySpeechEngine(),
        identity_dir=str(tmp_path / "host_identity"),
        bind="127.0.0.1",
        records_root=str(tmp_path / "host_remote_records"),
        ai_services=ai_services,
        meeting_services=meeting_services,
        extra_record_kinds={"walkthrough": host_walkthroughs},
    )

    pool: RemoteAIConnectionPool | None = None
    meeting_client: RemoteMeetingClient | None = None
    record_conn: RemoteConnection | None = None

    try:
        # Turn on host sharing & every host opt-in capability.
        service.set_text_cleanup(True)
        service.set_fast_judgments(True)
        service.set_walkthrough_writeup(True)
        service.set_meeting_ai(True)
        service.set_agent_ask(True)
        service.set_keep_records(True)
        service.set_host_enabled(True)

        state = service.host_state()
        assert state["running"] is True
        assert state["error"] == ""
        assert "walkthrough" in service.record_kinds()
        assert "walkthrough" in service.record_store().kinds

        # 1. HostInviteServer via RemoteEngineService.generate_invite -> HTTP /join/<token>.
        inv = service.generate_invite(port=0)
        inv_state = service.invite_state()
        assert inv_state["running"] is True
        assert inv_state["active"] is True
        assert inv_state["url"] == inv["url"]
        assert inv["url"].startswith(f"http://127.0.0.1:{inv['port']}/join/")

        status, headers, raw_html = _http_get(
            inv["url"],
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        assert status == 200
        html_body = raw_html.decode("utf-8")
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in headers["content-security-policy"]
        assert 'style="' not in html_body
        assert secret_key not in html_body

        # Extract openwhisper://pair URI from the join page AND download .openwhisper-invite.
        uri_match = re.search(r"openwhisper://pair\?[^\"'<>\s]+", html_body)
        assert uri_match is not None
        pair_uri = uri_match.group(0).replace("&amp;", "&")
        parsed_uri_invite = invite.parse_invite_uri(pair_uri)

        file_status, _fh, file_raw = _http_get(
            f"http://127.0.0.1:{inv['port']}/invite/{inv['token']}.openwhisper-invite"
        )
        assert file_status == 200
        parsed_file_invite = invite.decode_invite_file(file_raw.decode("utf-8"))
        assert parsed_file_invite == parsed_uri_invite
        assert parsed_file_invite.host == "127.0.0.1"
        assert parsed_file_invite.port == state["port"]
        assert parsed_file_invite.fingerprint == state["fingerprint"]

        # 2. Fingerprint-verified invite pairing (TOCTOU rejection + success).
        wrong_fp = ("F" if state["fingerprint"][0] != "F" else "E") + state["fingerprint"][1:]
        tampered = invite.InviteLink(
            host=parsed_file_invite.host,
            port=parsed_file_invite.port,
            fingerprint=wrong_fp,
            code=parsed_file_invite.code,
            name=parsed_file_invite.name,
        )
        with pytest.raises(RemoteEngineError, match="different security identity"):
            invite.pair_with_invite(service, tampered)
        assert service.host_state()["pairing"] is not None

        pairing = invite.pair_with_invite(service, parsed_file_invite, device_name="E2E-Client")
        token = remote_settings.load_client_token()
        assert token
        assert pairing.host == "127.0.0.1"
        assert pairing.port == state["port"]
        assert pairing.fingerprint == state["fingerprint"]

        # 3. Verify live ready handshake capabilities and zero secret leakage.
        record_conn = RemoteConnection(pairing.host, pairing.port, token, pairing.fingerprint)
        ready = record_conn.connect()
        assert secret_key not in json.dumps(ready)
        caps = ready["capabilities"]
        for expected_cap in (
            "text_cleanup",
            "fast_judgments",
            "walkthrough_writeup",
            "agent_ask",
            "meeting_ai",
            "speaker_diarization",
            "file_decode",
            "records",
            "walkthrough_records",
        ):
            assert caps.get(expected_cap) is True, f"Expected capability {expected_cap}=True, got {caps}"

        # 4. Phase 1 AI services over live TLS WebSocket.
        pool = RemoteAIConnectionPool(
            pairing_loader=lambda: pairing,
            token_loader=lambda: token,
        )
        cleanup_res = remote_text_cleanup("um hello world", pool=pool)
        assert cleanup_res["text"] == "Polished(openai/gpt-4.1-mini): um hello world"

        judge_res = remote_fast_judge(
            {"text": "let us finish"},
            {"stop": {"type": "noul", "prompt": "Should we stop?"}},
            pool=pool,
        )
        assert judge_res["answers"]["stop"]["noul"] == 0.91

        shot_path = tmp_path / "step-1.png"
        shot_path.write_bytes(_PNG_1X1)
        md, look, agent_label = remote_walkthrough_writeup(
            "# Draft\n\nClick save.\n\n![Step 1](step-1.png)\n",
            [shot_path],
            poll_interval_s=0.05,
            pool=pool,
        )
        assert "# E2E Guide (claude_code)" in md
        assert look.get("font") == "Georgia"
        assert agent_label == "Claude Code"
        assert writeup_stage_dirs and _wait_for(lambda: not os.path.exists(writeup_stage_dirs[-1]))

        ask_res = remote_agent_ask("What does this module do?", pool=pool)
        assert ask_res["ok"] is True
        assert "HostAsk[claude_code]: What does this module do?" in ask_res["output"]

        # 5. Phase 2 Speaker Embeddings, Meeting Agent Turn, and File Decode over live TLS.
        def open_meeting_conn() -> RemoteConnection:
            c = RemoteConnection(pairing.host, pairing.port, token, pairing.fingerprint)
            c.connect()
            return c

        meeting_client = RemoteMeetingClient(connect_fn=open_meeting_conn)
        emb = meeting_client.speaker_embed(np.ones(16000, dtype=np.float32) * 0.2)
        assert emb is not None and emb.shape == (4,)
        assert np.isclose(float(np.linalg.norm(emb)), 1.0, atol=1e-4)

        client_state_store = MeetingStateStore(
            MeetingState(meeting_id="mtg_e2e", cloud_enabled=True),
            segment_exists=lambda sid: sid == "seg_001",
        )
        client_tool_host = StoreToolHost(client_state_store)
        remote_core = RemoteAgentCore(client=meeting_client, poll_interval_s=0.05)
        remote_core.initialize(
            AgentConfig(
                meeting_id="mtg_e2e",
                provider="remote_host",
                model="",
                api_key=None,
                system_prompt="System prompt",
            ),
            client_tool_host,
        )
        checkpoint_payload = CheckpointPayload(
            request_id="req_e2e_1",
            state_snapshot=client_state_store.snapshot(),
            new_segments=[{
                "id": "seg_001",
                "text": "Elena will publish the client-only installer by Monday.",
                "channel": "loopback",
                "start_s": 0.0,
                "end_s": 3.5,
            }],
        )
        turn_res = remote_core.checkpoint(checkpoint_payload)
        assert turn_res.ok is True
        snap = client_state_store.snapshot()
        action_items = snap["cards"]["action_items"]
        assert len(action_items) == 1
        assert action_items[0]["text"] == "Publish the client-only installer"
        elena = next(p for p in snap["participants"].values() if p["display_name"] == "Elena")
        assert action_items[0]["data"]["owner_participant_id"] == elena["id"]

        # Compressed file decode: windows + probe + transcribe.
        fake_mp3 = tmp_path / "meeting_clip.mp3"
        fake_mp3.write_bytes(wav_sample.read_bytes())
        windows = list(decode_audio_windows_remote(str(fake_mp3), connect_fn=open_meeting_conn))
        assert len(windows) == 2
        assert windows[0][0] == 0.0 and windows[1][0] == 0.5
        dur_s, sr, ch = probe_audio_remote(str(fake_mp3), connect_fn=open_meeting_conn)
        assert (dur_s, sr, ch) == (1.0, 16000, 1)
        tx = transcribe_audio_file_remote(str(fake_mp3), connect_fn=open_meeting_conn)
        assert tx["window_count"] == 2
        assert "transcribed" in tx["text"]

        # 6. Walkthrough record sync through RemoteEngineService.record_store() + HostRecordStore.
        client_captures = tmp_path / "client_captures"
        wt_id = "20261128_e2e_wt"
        wt_dir = client_captures / wt_id
        wt_dir.mkdir(parents=True)
        (wt_dir / "walkthrough.json").write_text(
            json.dumps({
                "id": wt_id,
                "started_at": "2026-11-28T09:00:00+00:00",
                "duration_s": 12.5,
                "sections": [{"text": "Open Settings and pair.", "shot": "step-1.png"}],
            }),
            encoding="utf-8",
        )
        (wt_dir / "walkthrough.md").write_text("# Client Walkthrough\n\nOpen Settings.\n", encoding="utf-8")
        (wt_dir / "step-1.png").write_bytes(_PNG_1X1)

        client_wt_kind = WalkthroughRecords(str(client_captures))
        commit_res = _upload_walkthrough_over_connection(record_conn, client_wt_kind, wt_id)
        assert commit_res["stored"] is True
        assert commit_res["files"] == 3

        listed = record_conn.request("records_list", kind="walkthrough")
        assert len(listed["records"]) == 1
        assert listed["records"][0]["id"] == wt_id
        assert listed["records"][0]["title"] == "Client Walkthrough"

        opened = record_conn.request("records_open", kind="walkthrough", record_id=wt_id)
        assert any(f["name"] == "files/step-1.png" for f in opened["files"])
        fetched = record_conn.request(
            "records_fetch",
            kind="walkthrough",
            record_id=wt_id,
            name="files/step-1.png",
            offset=0,
        )
        assert base64.b64decode(fetched["data"]) == _PNG_1X1
        assert file_sha256(str(host_captures / "devices" / pairing.device_id / wt_id / "step-1.png")) == (
            hashlib.sha256(_PNG_1X1).hexdigest()
        )
        summary = service.records_summary(pairing.device_id)
        assert summary["walkthrough"]["count"] == 1

        deleted = record_conn.request("records_delete", kind="walkthrough", record_id=wt_id)
        assert deleted["deleted"] is True
        assert record_conn.request("records_list", kind="walkthrough")["records"] == []

        # 7. Invite revocation + auto-stop when host sharing stops.
        assert service.revoke_invite() is True
        assert service.invite_state()["running"] is False
        assert service.invite_state()["enabled"] is False

        # Re-enabling invite while host runs auto-starts HostInviteServer;
        # disabling host sharing auto-stops HostInviteServer.
        service.set_invite_enabled(True)
        assert service.invite_state()["running"] is True
        service.set_host_enabled(False)
        assert service.host_state()["running"] is False
        assert service.invite_state()["running"] is False
    finally:
        if record_conn is not None:
            record_conn.close()
        if meeting_client is not None:
            meeting_client.close()
        if pool is not None:
            pool.close_all()
        service.shutdown()

    assert meeting_core.shutdown_called is True


@pytest.mark.parametrize("edition", ["full", "client"])
def test_main_self_test_succeeds_in_full_and_client_editions(edition):
    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["OPENWHISPER_EDITION"] = edition
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        [sys.executable, str(repo_root / "main.py"), "--self-test"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"main.py --self-test failed for edition={edition!r} (rc={proc.returncode})\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
