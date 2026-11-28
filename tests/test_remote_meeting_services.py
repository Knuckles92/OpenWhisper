"""Tests for Host-Facilitated Meeting Mode (Speaker Diarization, Meeting AI,
Custom Reports) and Host File Decode RPC.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import threading
import time
import wave
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from meeting.agent.base import create_agent_core
from meeting.diarize.clustering import OnlineDiarizer
from meeting.diarize.embedder import EmbedderUnavailable, RemoteSpeakerEmbedder
from meeting.engine import (
    DIARIZATION_REMOTE_UNAVAILABLE,
    MeetingEngine,
    MeetingEngineOptions,
)
from meeting.interfaces import (
    AgentConfig,
    AgentCore,
    AgentResult,
    CheckpointPayload,
    TranscriptSegment,
)
from meeting.reinsight import StoreToolHost
from meeting.state.schema import MeetingState
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore
from services.remote_asr import protocol
from services.remote_asr.client import RemoteRequestError
from services.remote_asr.meeting_client import (
    RemoteAgentCore,
    decode_audio_windows_remote,
    probe_audio_remote,
    request_remote_custom_report,
    transcribe_audio_file_remote,
)
from services.remote_asr.meeting_host import (
    FILE_DECODE_OPS,
    MEETING_AI_OPS,
    HostMeetingServices,
    MeetingServiceError,
    RecordingToolHost,
)


class _WireConnection:
    """In-process connection exercising real ``protocol.pack_request`` / ``unpack_frame``."""

    def __init__(
        self,
        services: HostMeetingServices,
        device_id: str = "dev-laptop-1",
        device_name: str = "Laptop",
    ) -> None:
        self._services = services
        self._device = {"id": device_id, "name": device_name}
        self.closed = False
        self.alive = True
        self.ready: dict[str, Any] = {}
        self._serial = 0

    def connect(self) -> dict[str, Any]:
        self.ready = {
            "type": "ready",
            "host": {"name": "devbox"},
            "device": dict(self._device),
            "capabilities": self._services.meeting_ai_capabilities(),
        }
        return self.ready

    def close(self) -> None:
        self.closed = True
        self.alive = False

    def request(
        self,
        op: str,
        *,
        timeout: float = 60.0,
        audio=None,
        payload: bytes | None = None,
        **fields,
    ) -> dict:
        if self.closed:
            raise RuntimeError("Connection closed")
        self._serial += 1
        frame = protocol.pack_request(
            {"id": self._serial, "op": op, **fields},
            audio=audio,
            payload=payload,
        )
        header, raw_payload = protocol.unpack_frame(frame)
        try:
            if op in MEETING_AI_OPS:
                return self._services.handle_meeting_ai(
                    op, header, raw_payload, self._device
                )
            if op in FILE_DECODE_OPS:
                return self._services.handle_file_decode(
                    op, header, raw_payload, self._device
                )
            raise ValueError(f"Unknown op: {op}")
        except PermissionError as exc:
            raise RemoteRequestError(str(exc), code="forbidden") from exc
        except LookupError as exc:
            raise RemoteRequestError(str(exc), code="not_found") from exc
        except ValueError as exc:
            raise RemoteRequestError(str(exc), code="bad_request") from exc
        except NotImplementedError as exc:
            raise RemoteRequestError(str(exc), code="unsupported") from exc
        except MeetingServiceError as exc:
            raise RemoteRequestError(str(exc), code=exc.code) from exc


def _block_module_import(monkeypatch, module_name: str) -> None:
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module_name or name.startswith(module_name + "."):
            raise ModuleNotFoundError(f"No module named '{module_name}'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)


def _write_wav(path, seconds: float = 1.5, rate: int = 16000, freq: float = 440.0) -> str:
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float64) / rate
    pcm = np.clip(np.sin(2 * np.pi * freq * t) * 16000.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return str(path)


# ------------------------------------------------ Speaker Diarization Tests


class _FakeHostOnnxEmbedder:
    """Deterministic fake embedder: distinguishes audio by mean sign / energy."""

    instances = 0

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.available = True
        self.calls = 0
        type(self).instances += 1

    def embed(self, audio_f32_16k: np.ndarray) -> np.ndarray | None:
        self.calls += 1
        arr = np.asarray(audio_f32_16k, dtype=np.float32).reshape(-1)
        if arr.size < int(0.8 * 16000):
            return None
        # Positive DC -> speaker A ([1, 0, 0, 0]), negative DC -> speaker B ([0, 1, 0, 0])
        if float(np.mean(arr)) >= 0.0:
            vec = np.array([2.0, 0.1, 0.0, 0.0], dtype=np.float32)
        else:
            vec = np.array([0.1, 2.0, 0.0, 0.0], dtype=np.float32)
        return (vec / np.linalg.norm(vec)).astype(np.float32)


def test_speaker_embed_host_and_remote_diarizer_without_onnxruntime(tmp_path, monkeypatch):
    model_file = tmp_path / "wespeaker.onnx"
    model_file.write_bytes(b"fake-onnx")
    _FakeHostOnnxEmbedder.instances = 0

    enabled = {"value": True}
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: enabled["value"],
        speaker_model_path_fn=lambda: str(model_file),
        embedder_factory=_FakeHostOnnxEmbedder,
    )
    assert services.meeting_ai_capabilities()["speaker_diarization"] is True

    # Simulate a Client-Only build with no onnxruntime installed.
    _block_module_import(monkeypatch, "onnxruntime")

    embedder = RemoteSpeakerEmbedder(
        connect_fn=lambda: _WireConnection(services)
    )
    assert embedder.available is True

    # Short audio returns None without even hitting the host.
    short = np.zeros(4000, dtype=np.float32)
    assert embedder.embed(short) is None

    # Full segments get L2-normalized float32 embeddings and separate two speakers.
    audio_a = np.full(16000, 0.25, dtype=np.float32)
    audio_b = np.full(16000, -0.25, dtype=np.float32)
    emb_a = embedder.embed(audio_a)
    emb_b = embedder.embed(audio_b)
    assert emb_a is not None and emb_b is not None
    assert emb_a.dtype == np.float32
    assert np.isclose(np.linalg.norm(emb_a), 1.0, atol=1e-5)
    assert float(np.dot(emb_a, emb_b)) < 0.2
    # Host embedder was lazily constructed once and reused.
    assert _FakeHostOnnxEmbedder.instances == 1

    # Plug directly into OnlineDiarizer.
    store = MeetingStateStore(MeetingState(meeting_id="m_remote_diar"))
    diarizer = OnlineDiarizer(embedder, store, None, "m_remote_diar")
    assert diarizer.is_available() is True
    seg1 = TranscriptSegment("sg_1", "m_remote_diar", 1, "loopback", 0.0, 1.0, "hello")
    seg2 = TranscriptSegment("sg_2", "m_remote_diar", 1, "loopback", 1.0, 2.0, "world")
    pid1 = diarizer.assign(seg1, audio_a, 16000)
    pid2 = diarizer.assign(seg2, audio_b, 16000)
    assert pid1 is not None and pid2 is not None and pid1 != pid2

    # Rechecking opt-in mid-meeting: turning off on host immediately denies and degrades cleanly!
    enabled["value"] = False
    with pytest.raises(EmbedderUnavailable):
        embedder.embed(audio_a)
    assert embedder.available is False
    assert diarizer.is_available() is False
    embedder.close()
    services.shutdown()


def test_speaker_embed_denied_when_host_opt_in_off(tmp_path):
    model_file = tmp_path / "wespeaker.onnx"
    model_file.write_bytes(b"fake-onnx")
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: False,
        speaker_model_path_fn=lambda: str(model_file),
        embedder_factory=_FakeHostOnnxEmbedder,
    )
    caps = services.meeting_ai_capabilities()
    assert caps["speaker_diarization"] is False
    assert caps["meeting_ai"] is False
    with pytest.raises(PermissionError):
        services.handle_meeting_ai(
            "speaker_embed",
            {"op": "speaker_embed"},
            protocol.encode_audio(np.zeros(16000, dtype=np.float32)),
            {"id": "dev-1", "name": "Laptop"},
        )
    with pytest.raises(EmbedderUnavailable, match="not enabled"):
        RemoteSpeakerEmbedder(connect_fn=lambda: _WireConnection(services))
    services.shutdown()


class _MinimalRepo:
    def __init__(self) -> None:
        self.segments: dict[str, dict[str, Any]] = {}

    def get_segment(self, meeting_id: str, segment_id: str) -> dict[str, Any] | None:
        return self.segments.get(segment_id)

    def segment_exists(self, meeting_id: str, segment_id: str) -> bool:
        return segment_id in self.segments

    def on_ops_applied(self, meeting_id, state, results, actor_type, actor_id):
        for r in results:
            if r.ok and (r.op or {}).get("op") == "revise_segment_text":
                sid = r.op.get("segment_id")
                if sid in self.segments:
                    self.segments[sid]["text"] = r.op.get("text")
        return None

    def persist_state(self, meeting_id, state):
        return None


def test_meeting_engine_remote_host_and_auto_fallback_diarization(tmp_path, monkeypatch):
    model_file = tmp_path / "wespeaker.onnx"
    model_file.write_bytes(b"fake-onnx")
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        speaker_model_path_fn=lambda: str(model_file),
        embedder_factory=_FakeHostOnnxEmbedder,
    )

    # 1. Explicit speaker_id_backend="remote_host" skips local model download/resolver.
    resolver_called = []
    engine = MeetingEngine(
        MeetingEngineOptions(
            speaker_id_backend="remote_host",
            diarization_model_resolver=lambda: resolver_called.append(True) or None,
        ),
        repository=_MinimalRepo(),
    )
    engine.meeting_id = "m_eng_1"
    engine.store = MeetingStateStore(MeetingState(meeting_id="m_eng_1"))

    monkeypatch.setattr(
        "services.remote_asr.meeting_client._default_connect",
        lambda: _WireConnection(services),
    )
    engine._start_diarizer()
    assert resolver_called == []
    assert engine._diarizer is not None
    assert engine.store.with_state(lambda s: s.diarization_available) is True
    engine._close_diarizer_embedder(engine._diarizer)

    # 2. Automatic fallback: speaker_id_backend="local" when onnxruntime is missing
    # and paired host advertises speaker_diarization.
    _block_module_import(monkeypatch, "onnxruntime")
    monkeypatch.setattr("services.remote_asr.meeting_client.has_paired_host", lambda: True)
    engine2 = MeetingEngine(
        MeetingEngineOptions(
            speaker_id_backend="local",
            diarization_model_resolver=lambda: resolver_called.append(True) or None,
        ),
        repository=_MinimalRepo(),
    )
    engine2.meeting_id = "m_eng_2"
    engine2.store = MeetingStateStore(MeetingState(meeting_id="m_eng_2"))
    engine2._start_diarizer()
    assert resolver_called == []
    assert engine2._diarizer is not None
    assert isinstance(engine2._diarizer._embedder, RemoteSpeakerEmbedder)
    assert engine2.store.with_state(lambda s: s.diarization_available) is True
    engine2._close_diarizer_embedder(engine2._diarizer)

    # 3. Remote unavailable sets clear reason without crashing.
    disabled_services = HostMeetingServices(meeting_ai_enabled=lambda: False)
    monkeypatch.setattr(
        "services.remote_asr.meeting_client._default_connect",
        lambda: _WireConnection(disabled_services),
    )
    engine3 = MeetingEngine(
        MeetingEngineOptions(speaker_id_backend="remote_host"),
        repository=_MinimalRepo(),
    )
    engine3.meeting_id = "m_eng_3"
    engine3.store = MeetingStateStore(MeetingState(meeting_id="m_eng_3"))
    engine3._start_diarizer()
    assert engine3._diarizer is None
    assert engine3.store.with_state(lambda s: s.diarization_available) is False
    reason = engine3.store.with_state(lambda s: s.diarization_reason)
    assert reason and (
        "not enabled" in reason or reason == DIARIZATION_REMOTE_UNAVAILABLE
    )
    services.shutdown()
    disabled_services.shutdown()


# -------------------------------------------- Meeting Intelligence Agent Tests


class _ScriptedHostAgentCore:
    """Fake host AgentCore that exercises multi-tool patch/question/activity flows."""

    def __init__(self, *, slow_event: threading.Event | None = None) -> None:
        self.cfg: AgentConfig | None = None
        self.tools: Any = None
        self._activity_cb = None
        self._slow_event = slow_event
        self.canceled = False
        self.shutdown_called = False

    def set_activity_callback(self, cb) -> None:
        self._activity_cb = cb

    def initialize(self, cfg: AgentConfig, tools) -> None:
        self.cfg = cfg
        self.tools = tools

    def _emit(self, kind: str, label: str, tool: str = "") -> None:
        if self._activity_cb is not None:
            self._activity_cb(SimpleNamespace(kind=kind, label=label, tool=tool, pass_kind="cards", ts=""))

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        if self._slow_event is not None:
            self._slow_event.wait(timeout=5.0)
            if self.canceled:
                return AgentResult(ok=False, error="Canceled")

        self._emit("thinking", "Analyzing transcript")
        # Verify host read tools do NOT leak host DB or knowledge folder.
        recall = self.tools.search_past_meetings(query="secret")
        assert recall["ok"] is False and recall["disabled"] is True
        ctx = self.tools.search_context_files(query="secret")
        assert ctx["ok"] is False and ctx["disabled"] is True

        seg_id = payload.new_segments[0]["id"]
        if payload.is_polish:
            res = self.tools.apply_agent_ops([{
                "op": "revise_segment_text",
                "segment_id": seg_id,
                "text": "Polished transcript text.",
                "evidence": [seg_id],
            }])
            return AgentResult(ok=True, op_results=res, usage={"total_tokens": 42})

        # 1. Create a participant and reference its id in a subsequent action item!
        p_res = self.tools.apply_agent_ops([{
            "op": "upsert_participant",
            "display_name": "Priya",
            "kind": "others_cluster",
            "is_provisional": True,
            "evidence": [seg_id],
        }])
        assert p_res[0].ok, p_res[0].reason
        pid = p_res[0].target_id
        self._emit("tool", "Adding items", tool="patch_state")
        # 2. Add action item owned by the new participant + update it.
        a_res = self.tools.apply_agent_ops([{
            "op": "add_item",
            "card": "action_items",
            "text": "Send the updated proposal",
            "data": {"owner_participant_id": pid, "deadline": "Friday"},
            "evidence": [seg_id],
        }])
        assert a_res[0].ok, a_res[0].reason
        item_id = a_res[0].target_id
        u_res = self.tools.apply_agent_ops([{
            "op": "update_item",
            "id": item_id,
            "base_revision": 1,
            "set": {"text": "Send the final updated proposal"},
            "evidence": [seg_id],
        }])
        assert u_res[0].ok, u_res[0].reason
        q_res = self.tools.ask_question("When is the launch date?", [seg_id])
        assert q_res.ok, q_res.reason
        r_res = self.tools.resolve_question(
            q_res.target_id, "Next Tuesday", 0.95, [seg_id]
        )
        assert r_res.ok, r_res.reason
        return AgentResult(
            ok=True,
            op_results=[*p_res, *a_res, *u_res, q_res, r_res],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "requests": 1},
        )

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        seg_id = payload.new_segments[0]["id"]
        res = self.tools.apply_agent_ops([{
            "op": "set_rolling_summary",
            "text": "Final consolidated meeting summary.",
            "evidence": [seg_id],
        }])
        return AgentResult(ok=True, op_results=res, usage={"total_tokens": 200})

    def cancel(self) -> None:
        self.canceled = True
        if self._slow_event is not None:
            self._slow_event.set()

    def is_healthy(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_create_agent_core_remote_host_registration():
    core = create_agent_core("remote_host")
    assert isinstance(core, RemoteAgentCore)
    assert isinstance(core, AgentCore)
    core.shutdown()


def test_remote_agent_core_turn_id_remapping_polish_consolidation_and_activity():
    created_cores: list[_ScriptedHostAgentCore] = []

    def core_factory(kind: str, payload_dir: str | None):
        core = _ScriptedHostAgentCore()
        created_cores.append(core)
        return core

    route = SimpleNamespace(
        kind="pi", provider="openrouter", model="openai/gpt-4o-mini",
        endpoint=None, payload_dir=None,
    )
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        route_resolver=lambda: route,
        core_factory=core_factory,
    )

    repo = _MinimalRepo()
    repo.segments["sg_1"] = {
        "id": "sg_1",
        "start_s": 1.0,
        "end_s": 3.0,
        "text": "Priya will send the updated proposal by Friday. Launch is next Tuesday.",
        "channel": "loopback",
    }
    store = MeetingStateStore(
        MeetingState(meeting_id="m_ai_1", cloud_enabled=True),
        repository=repo,
        segment_handler=make_segment_handler(repo, "m_ai_1"),
        segment_exists=lambda sid: repo.segment_exists("m_ai_1", sid),
    )
    tool_host = StoreToolHost(store, repo)

    client_core = RemoteAgentCore(
        connect_fn=lambda: _WireConnection(services),
        poll_interval_s=0.02,
    )
    activities: list[Any] = []
    client_core.set_activity_callback(activities.append)
    client_core.initialize(
        AgentConfig(
            meeting_id="m_ai_1",
            provider="remote_host",
            model="",
            api_key=None,
            system_prompt="System prompt",
        ),
        tool_host,
    )
    assert client_core.is_healthy() is True

    # 1. Rolling checkpoint: creates participant, action item referencing remapped pid,
    # updates that item, asks & resolves a question, and forwards activity ticks!
    cp = CheckpointPayload(
        request_id="req_1",
        state_snapshot=store.snapshot(),
        new_segments=[dict(repo.segments["sg_1"])],
    )
    res = client_core.checkpoint(cp)
    assert res.ok is True
    assert res.usage["total_tokens"] == 150
    assert len(res.op_results) == 5
    assert all(r.ok for r in res.op_results)
    assert len(activities) >= 2

    snap = store.snapshot()
    priya = next(
        p for p in snap["participants"].values() if p["display_name"] == "Priya"
    )
    action = snap["cards"]["action_items"][0]
    assert action["text"] == "Send the final updated proposal"
    # Remapped participant id matches client's real participant id!
    assert action["data"]["owner_participant_id"] == priya["id"]
    assert snap["questions"][0]["status"] == "resolved"
    assert snap["questions"][0]["answer"] == "Next Tuesday"

    # 2. Polish pass (revise_segment_text)
    polish_cp = CheckpointPayload(
        request_id="req_2",
        state_snapshot=store.snapshot(),
        new_segments=[dict(repo.segments["sg_1"])],
        is_polish=True,
    )
    polish_res = client_core.checkpoint(polish_cp)
    assert polish_res.ok is True
    assert repo.segments["sg_1"]["text"] == "Polished transcript text."

    # 3. Consolidation pass (including large snapshot > 70 KiB to test binary JSON payload!)
    big_snap = store.snapshot()
    big_snap["rolling_summary"] = "x" * 75000
    cons_cp = CheckpointPayload(
        request_id="req_3",
        state_snapshot=big_snap,
        new_segments=[dict(repo.segments["sg_1"])],
        is_consolidation=True,
    )
    cons_res = client_core.consolidate(cons_cp)
    assert cons_res.ok is True
    assert store.snapshot()["rolling_summary"] == "Final consolidated meeting summary."
    assert all(c.shutdown_called for c in created_cores)
    # Host never received a client api_key.
    assert all(c.cfg.api_key is None for c in created_cores)

    client_core.shutdown()
    services.shutdown()


def test_remote_agent_client_writes_revoked_and_human_override_preserved():
    route = SimpleNamespace(
        kind="pi", provider="openrouter", model="m", endpoint=None, payload_dir=None,
    )
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        route_resolver=lambda: route,
        core_factory=lambda _k, _d: _ScriptedHostAgentCore(),
    )
    repo = _MinimalRepo()
    repo.segments["sg_1"] = {"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "hi"}
    store = MeetingStateStore(
        MeetingState(meeting_id="m_revoke", cloud_enabled=True),
        repository=repo,
        segment_exists=lambda sid: sid == "sg_1",
    )
    # Client revokes writes (e.g. meeting ended while turn ran on host).
    tool_host = StoreToolHost(store, repo, writes_allowed=lambda: False)
    core = RemoteAgentCore(connect_fn=lambda: _WireConnection(services), poll_interval_s=0.02)
    core.initialize(AgentConfig("m_revoke", "remote_host", "", None, ""), tool_host)
    res = core.checkpoint(
        CheckpointPayload("r1", store.snapshot(), [dict(repo.segments["sg_1"])])
    )
    assert res.op_results and all(not r.ok and r.reason == "agent_writes_revoked" for r in res.op_results)
    assert store.snapshot()["cards"]["action_items"] == []
    core.shutdown()
    services.shutdown()


def test_remote_agent_human_edited_item_protected_on_client_replay():
    """If the user edits an item while a host turn is in flight, client StoreToolHost rejects overwrite."""
    repo = _MinimalRepo()
    repo.segments["sg_1"] = {"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "ship Friday"}
    store = MeetingStateStore(
        MeetingState(meeting_id="m_human", cloud_enabled=True),
        repository=repo,
        segment_exists=lambda sid: sid == "sg_1",
    )
    added = store.apply("agent", "agent", [{
        "op": "add_item", "card": "key_points", "text": "Draft point", "evidence": ["sg_1"],
    }])
    item_id = added[0].target_id
    # Snapshot taken BEFORE human edit (simulates in-flight host turn).
    stale_snapshot = store.snapshot()
    # Human edits the item on client while turn runs on host.
    store.apply("host", "me", [{
        "op": "update_item", "id": item_id, "set": {"text": "Human-authored truth"},
    }])

    class _OverwriteAgent:
        def initialize(self, cfg, tools):
            self.tools = tools

        def checkpoint(self, payload):
            res = self.tools.apply_agent_ops([{
                "op": "update_item",
                "id": item_id,
                "base_revision": 1,
                "set": {"text": "Agent overwrite attempt"},
                "evidence": ["sg_1"],
            }])
            return AgentResult(ok=True, op_results=res)

        def consolidate(self, payload):
            return self.checkpoint(payload)

        def cancel(self):
            pass

        def is_healthy(self):
            return True

        def shutdown(self):
            pass

    route = SimpleNamespace(
        kind="pi", provider="openrouter", model="m", endpoint=None, payload_dir=None,
    )
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        route_resolver=lambda: route,
        core_factory=lambda _k, _d: _OverwriteAgent(),
    )
    core = RemoteAgentCore(connect_fn=lambda: _WireConnection(services), poll_interval_s=0.02)
    core.initialize(AgentConfig("m_human", "remote_host", "", None, ""), StoreToolHost(store, repo))
    res = core.checkpoint(CheckpointPayload("r_hum", stale_snapshot, [dict(repo.segments["sg_1"])]))
    assert len(res.op_results) == 1
    assert res.op_results[0].ok is False
    assert res.op_results[0].reason == "human_edited"
    assert store.snapshot()["cards"]["key_points"][0]["text"] == "Human-authored truth"
    core.shutdown()
    services.shutdown()


def test_remote_agent_cancel_and_device_isolation_and_recursion_guard():
    slow_event = threading.Event()
    host_core = _ScriptedHostAgentCore(slow_event=slow_event)
    route = SimpleNamespace(
        kind="pi", provider="openrouter", model="m", endpoint=None, payload_dir=None,
    )
    services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        route_resolver=lambda: route,
        core_factory=lambda _k, _d: host_core,
    )
    conn_a = _WireConnection(services, device_id="dev-A", device_name="Laptop A")
    conn_b = _WireConnection(services, device_id="dev-B", device_name="Laptop B")
    conn_a.connect()
    conn_b.connect()

    started = conn_a.request(
        "meeting_agent_turn",
        meeting_id="m_cancel",
        request_id="r_cancel",
        state_snapshot=MeetingState(meeting_id="m_cancel").to_dict(),
        new_segments=[{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "hi"}],
    )
    job_id = started["job_id"]
    assert started["status"] == "running"

    # Device B cannot read status or cancel Device A's job!
    with pytest.raises(RemoteRequestError) as exc_status:
        conn_b.request("meeting_agent_status", job_id=job_id)
    assert exc_status.value.code == "not_found"
    with pytest.raises(RemoteRequestError) as exc_cancel:
        conn_b.request("meeting_agent_cancel", job_id=job_id)
    assert exc_cancel.value.code == "not_found"

    # Device A cancels cleanly.
    canceled = conn_a.request("meeting_agent_cancel", job_id=job_id)
    assert canceled["canceled"] is True
    assert canceled["status"] == "canceled"
    # Give worker thread a moment to see cancel and shut down.
    deadline = time.monotonic() + 2.0
    while not host_core.shutdown_called and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host_core.canceled is True
    assert host_core.shutdown_called is True

    # Host refuses recursive remote_host routing.
    recursive_services = HostMeetingServices(
        meeting_ai_enabled=lambda: True,
        route_resolver=lambda: SimpleNamespace(
            kind="remote_host", provider="remote_host", model="", endpoint=None, payload_dir=None
        ),
    )
    assert recursive_services.meeting_ai_capabilities()["meeting_ai"] is False
    with pytest.raises(ValueError, match="cannot forward"):
        recursive_services.handle_meeting_ai(
            "meeting_agent_turn",
            {
                "op": "meeting_agent_turn",
                "meeting_id": "m_rec",
                "state_snapshot": {},
                "new_segments": [],
            },
            b"",
            {"id": "dev-A", "name": "A"},
        )
    services.shutdown()
    recursive_services.shutdown()


def test_remote_custom_report_round_trip_and_opt_in_recheck():
    enabled = {"value": True}
    route = SimpleNamespace(
        kind="pi", provider="openrouter", model="gpt-4o", endpoint=None, payload_dir=None,
    )

    def fake_report(meeting, state, segments, request, *, provider, model, endpoint, cancel_event):
        assert provider == "openrouter"
        assert model == "gpt-4o"
        return {
            "markdown": f"# Pricing Summary\n\nAnswer to: {request}",
            "title": "Pricing Summary",
            "sources": {
                "transcript_lines": len(segments),
                "provider": provider,
                "model": model,
                "secret_local_path": "/should/be/stripped",
            },
            "usage": {"total_tokens": 321},
        }

    services = HostMeetingServices(
        meeting_ai_enabled=lambda: enabled["value"],
        route_resolver=lambda: route,
        report_generator=fake_report,
    )
    report = request_remote_custom_report(
        {"id": "m_rep", "title": "Sales Call"},
        MeetingState(meeting_id="m_rep").to_dict(),
        [{"id": "sg_1", "start_s": 0.0, "end_s": 2.0, "text": "Discount is 10%"}],
        "Summarize pricing objections",
        connect_fn=lambda: _WireConnection(services),
        poll_interval_s=0.02,
    )
    assert report["title"] == "Pricing Summary"
    assert "Summarize pricing objections" in report["markdown"]
    assert "secret_local_path" not in report["sources"]
    assert report["usage"] == {"total_tokens": 321}

    # Turning off opt-in blocks subsequent custom reports.
    enabled["value"] = False
    with pytest.raises(RuntimeError, match="not enabled"):
        request_remote_custom_report(
            {"id": "m_rep"},
            MeetingState(meeting_id="m_rep").to_dict(),
            [],
            "Another report",
            connect_fn=lambda: _WireConnection(services),
            poll_interval_s=0.02,
        )
    services.shutdown()


def test_recording_tool_host_validates_and_records():
    state = MeetingState(meeting_id="m_rec_host").to_dict()
    host_tools = RecordingToolHost(
        state,
        [{"id": "sg_1", "start_s": 0.0, "end_s": 2.0, "text": "hello"}],
    )
    # Unknown evidence rejected and NOT recorded.
    bad = host_tools.apply_agent_ops([{
        "op": "add_item",
        "card": "key_points",
        "text": "Hallucinated point",
        "evidence": ["sg_missing"],
    }])
    assert bad[0].ok is False and bad[0].reason == "unknown_evidence"
    assert host_tools.recorded_ops == []

    # Valid evidence accepted and recorded.
    good = host_tools.apply_agent_ops([{
        "op": "add_item",
        "card": "key_points",
        "text": "Grounded point",
        "evidence": ["sg_1"],
    }])
    assert good[0].ok is True
    assert len(host_tools.recorded_ops) == 1
    assert host_tools.recorded_ops[0]["kind"] == "patch"
    assert host_tools.recorded_ops[0]["host_target_id"] == good[0].target_id


# ------------------------------------------------------- Host File Decode RPC


def test_file_decode_windows_pagination_transcribe_probe_and_cleanup(tmp_path):
    staging = tmp_path / "staging"
    wav_path = _write_wav(tmp_path / "meeting.mp3", seconds=1.0, rate=16000)

    # Simulate 5 windows to exercise multi-page retrieval (MAX_WINDOWS_PER_PAGE=3).
    fake_windows = [
        (float(i * 10), np.full(1600, 0.1 * (i + 1), dtype=np.float32))
        for i in range(5)
    ]

    class _FakeEngine:
        accepts_phrases = True

        def transcribe(self, audio, language, *, phrases=()):
            return {
                "text": f"win-{len(audio)}-{language or 'auto'}",
                "segments": [{"text": "seg", "start": 0.5, "end": 1.0}],
            }

    services = HostMeetingServices(
        file_decode_root=str(staging),
        engine_provider=lambda: _FakeEngine(),
        windows_fn=lambda _path: list(fake_windows),
        probe_fn=lambda _path: (50.0, 44100, 2),
    )

    # 1. Chunked decode into 16 kHz windows across multiple pages
    got = list(
        decode_audio_windows_remote(
            wav_path, connect_fn=lambda: _WireConnection(services)
        )
    )
    assert len(got) == 5
    for idx, (offset_s, samples) in enumerate(got):
        assert offset_s == float(idx * 10)
        assert samples.dtype == np.float32
        assert len(samples) == 1600
        assert np.allclose(samples, 0.1 * (idx + 1), atol=1 / 32768)
    # Staging folder was cleaned up after EOF.
    assert services._file_decode._sessions == {}

    # 2. Direct host transcribe mode
    tx = transcribe_audio_file_remote(
        wav_path,
        language="en",
        phrases=("OpenWhisper",),
        connect_fn=lambda: _WireConnection(services),
    )
    assert tx["window_count"] == 5
    assert "win-1600-en" in tx["text"]
    assert tx["segments"][1]["start"] == pytest.approx(10.5)
    assert services._file_decode._sessions == {}

    # 3. Probe mode
    dur, rate, ch = probe_audio_remote(
        wav_path, connect_fn=lambda: _WireConnection(services)
    )
    assert (dur, rate, ch) == (50.0, 44100, 2)
    assert services._file_decode._sessions == {}
    services.shutdown()


def test_file_decode_security_checks_hash_mismatch_and_abort(tmp_path):
    staging = tmp_path / "staging"
    services = HostMeetingServices(
        file_decode_root=str(staging),
        windows_fn=lambda _p: [(0.0, np.zeros(1600, dtype=np.float32))],
        max_file_decode_bytes=1024,
    )
    dev_a = {"id": "dev-A", "name": "A"}
    dev_b = {"id": "dev-B", "name": "B"}
    good_data = b"ID3fake-mp3-data"
    good_sha = hashlib.sha256(good_data).hexdigest()

    # Path traversal & disallowed extensions rejected
    for bad_name in ("../evil.mp3", "sub/a.mp3", "C:\\x.mp3", "clip.exe", "clip.py", "a\x00.mp3", ""):
        with pytest.raises(ValueError):
            services.handle_file_decode(
                "file_decode_begin",
                {"filename": bad_name, "size": len(good_data), "sha256": good_sha},
                b"",
                dev_a,
            )

    # Oversized file rejected
    with pytest.raises(ValueError, match="size"):
        services.handle_file_decode(
            "file_decode_begin",
            {"filename": "big.mp3", "size": 2048, "sha256": good_sha},
            b"",
            dev_a,
        )

    # Hash mismatch detected & staged file removed
    wrong_sha = hashlib.sha256(b"different").hexdigest()
    beg = services.handle_file_decode(
        "file_decode_begin",
        {"filename": "clip.mp3", "size": len(good_data), "sha256": wrong_sha},
        b"",
        dev_a,
    )
    uid = beg["upload_id"]
    session_dir = services._file_decode._sessions[uid].session_dir
    services.handle_file_decode(
        "file_decode_put", {"upload_id": uid, "offset": 0}, good_data, dev_a
    )
    # Device B cannot touch Device A's upload!
    with pytest.raises(LookupError):
        services.handle_file_decode(
            "file_decode_finish", {"upload_id": uid}, b"", dev_b
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        services.handle_file_decode(
            "file_decode_finish", {"upload_id": uid}, b"", dev_a
        )
    assert not os.path.exists(session_dir)

    # Abort cleans up staging directory immediately
    beg2 = services.handle_file_decode(
        "file_decode_begin",
        {"filename": "clip.m4a", "size": len(good_data), "sha256": good_sha},
        b"",
        dev_a,
    )
    uid2 = beg2["upload_id"]
    session_dir2 = services._file_decode._sessions[uid2].session_dir
    assert os.path.isdir(session_dir2)
    aborted = services.handle_file_decode(
        "file_decode_abort", {"upload_id": uid2}, b"", dev_a
    )
    assert aborted == {"aborted": True}
    assert not os.path.exists(session_dir2)
    services.shutdown()


def test_host_meeting_services_registration_hooks():
    registered: dict[str, Any] = {}

    class _FakeService:
        def register_meeting_ai_handler(self, handler, *, capabilities=None):
            registered["meeting_ai"] = (handler, capabilities)

        def register_file_decode_handler(self, handler):
            registered["file_decode"] = handler

        def register_record_kind(self, kind, handler):
            registered["record_kind"] = (kind, handler)

    class _FakeHost:
        def set_meeting_ai_handler(self, handler, *, capabilities=None):
            registered["host_meeting_ai"] = (handler, capabilities)

        def set_file_decode_handler(self, handler):
            registered["host_file_decode"] = handler

    services = HostMeetingServices(meeting_ai_enabled=lambda: False)
    services.register_on_service(_FakeService())
    services.register_on_host(_FakeHost())
    assert registered["meeting_ai"] == (
        services.handle_meeting_ai,
        services.meeting_ai_capabilities,
    )
    assert registered["file_decode"] == services.handle_file_decode
    assert registered["record_kind"][0] == "walkthrough"
    assert registered["host_meeting_ai"] == (
        services.handle_meeting_ai,
        services.meeting_ai_capabilities,
    )
    assert registered["host_file_decode"] == services.handle_file_decode
    services.shutdown()
