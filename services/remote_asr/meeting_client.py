"""Client-side adapters for Host-Facilitated Meeting Mode and File Decode.

Uses a dedicated paired ``RemoteConnection`` so slow meeting AI turns, speaker
embedding calls, and compressed file decode uploads never contend with the
live speech dictation WebSocket.

Exports:
* ``RemoteMeetingClient`` — paired connection manager for meeting AI / speaker
  embeddings / custom reports.
* ``RemoteAgentCore`` — ``meeting.interfaces.AgentCore`` implementation that
  delegates turns to the host and replays validated ops through the client's
  real ``StoreToolHost``.
* ``RemoteSpeakerEmbedder`` — re-exported from ``meeting.diarize.embedder``.
* ``decode_audio_windows_remote`` / ``transcribe_audio_file_remote`` /
  ``probe_audio_remote`` — chunked host-side decode helpers for PyAV-free
  clients.
* ``request_remote_custom_report`` — async custom-report helper over the
  paired host.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np

from meeting.interfaces import (
    AgentConfig,
    AgentResult,
    AgentToolHost,
    CheckpointPayload,
    OpResult,
)
from services.remote_asr import protocol
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import (
    RemoteConnection,
    RemoteConnectionLost,
    RemoteEngineError,
    RemoteRequestError,
)

logger = logging.getLogger(__name__)

#: Default polling interval for long-running meeting AI jobs on the host.
DEFAULT_POLL_INTERVAL_S = 0.35
#: Default timeouts for remote operations.
SPEAKER_EMBED_TIMEOUT_S = 30.0
AGENT_TURN_TIMEOUT_S = 900.0
CUSTOM_REPORT_TIMEOUT_S = 420.0
FILE_DECODE_TIMEOUT_S = 300.0
BUSY_RETRY_LIMIT = 4


def has_paired_host() -> bool:
    """True when this computer has a saved paired host and device token."""
    helper = getattr(remote_settings, "has_paired_host", None)
    if callable(helper):
        try:
            if not helper():
                return False
        except Exception:
            pass
    pairing = remote_settings.load_client_pairing()
    if pairing is None:
        return False
    return bool(remote_settings.load_client_token())


def _default_connect() -> RemoteConnection:
    """Open a fresh authenticated ``RemoteConnection`` to the paired host."""
    pairing = remote_settings.load_client_pairing()
    if pairing is None:
        raise RemoteEngineError("Pair with a host in Settings → Remote engine first.")
    token = remote_settings.load_client_token()
    if not token:
        raise RemoteEngineError(
            f"This computer's pairing with {pairing.host_name} is missing its token. "
            "Pair again in Settings → Remote engine."
        )
    conn = RemoteConnection(
        pairing.host,
        pairing.port,
        token,
        pairing.fingerprint,
        alternates=pairing.alternates,
    )
    conn.connect()
    return conn


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_payload(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class RemoteMeetingClient:
    """Dedicated paired connection wrapper for host meeting AI and speaker ID."""

    def __init__(
        self,
        connect_fn: Callable[[], Any] | None = None,
    ) -> None:
        self._connect_fn = connect_fn or _default_connect
        self._lock = threading.Lock()
        self._conn: Any | None = None
        self._closed = False
        self._active_job_id: str | None = None
        self._job_lock = threading.Lock()

    def close(self) -> None:
        self._closed = True
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.debug("Closing RemoteMeetingClient connection raised", exc_info=True)

    def _ensure_connected(self) -> Any:
        if self._closed:
            raise RemoteEngineError("Remote meeting client is closed.")
        conn = self._conn
        if conn is not None and not getattr(conn, "closed", False) and getattr(conn, "alive", True):
            return conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._conn = None
        conn = self._connect_fn()
        if not getattr(conn, "ready", None):
            conn.connect()
        self._conn = conn
        return conn

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            conn = self._ensure_connected()
            ready = getattr(conn, "ready", None) or {}
            caps = ready.get("capabilities")
            return dict(caps) if isinstance(caps, dict) else {}

    def supports(self, capability: str) -> bool:
        try:
            return self.capabilities().get(capability) is True
        except Exception:
            return False

    def request(
        self,
        op: str,
        *,
        timeout: float = 60.0,
        audio=None,
        payload: bytes | None = None,
        **fields,
    ) -> dict:
        """Send one request over the dedicated connection, reconnecting once if idle-dead."""
        delay = 0.5
        for busy_try in range(BUSY_RETRY_LIMIT + 1):
            try:
                return self._request_once(
                    op, timeout=timeout, audio=audio, payload=payload, **fields
                )
            except RemoteRequestError as exc:
                if not exc.retryable or busy_try >= BUSY_RETRY_LIMIT:
                    raise
                wait = exc.retry_after_s if exc.retry_after_s is not None else delay
                time.sleep(min(max(wait, 0.1), 3.0))
                delay = min(delay * 1.5, 3.0)
        raise AssertionError("unreachable")

    def _request_once(
        self,
        op: str,
        *,
        timeout: float,
        audio,
        payload: bytes | None,
        **fields,
    ) -> dict:
        with self._lock:
            for attempt in (0, 1):
                conn = self._ensure_connected()
                try:
                    return conn.request(
                        op, timeout=timeout, audio=audio, payload=payload, **fields
                    )
                except RemoteConnectionLost as exc:
                    self._conn = None
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if exc.sent or attempt:
                        raise
            raise RemoteEngineError("Could not reach the paired host.")

    def speaker_embed(
        self,
        audio_f32_16k: np.ndarray,
        *,
        timeout_s: float = SPEAKER_EMBED_TIMEOUT_S,
    ) -> np.ndarray | None:
        """Request one L2-normalized float32 speaker embedding from the paired host."""
        reply = self.request("speaker_embed", audio=audio_f32_16k, timeout=timeout_s)
        raw = reply.get("embedding")
        if raw is None:
            return None
        if not isinstance(raw, list) or not raw:
            raise RemoteEngineError("Host returned an invalid speaker embedding.")
        vec = np.asarray(raw, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm < 1e-8:
            return None
        return (vec / norm).astype(np.float32)

    def cancel_active_job(self) -> None:
        """Best-effort cancel of any currently active meeting AI job on the host."""
        with self._job_lock:
            job_id = self._active_job_id
        if not job_id:
            return
        try:
            # Open a quick cancel connection if the main lock is busy, or reuse directly.
            if self._lock.acquire(timeout=0.5):
                try:
                    conn = self._ensure_connected()
                    conn.request("meeting_agent_cancel", job_id=job_id, timeout=10.0)
                finally:
                    self._lock.release()
            else:
                aux = self._connect_fn()
                try:
                    if not getattr(aux, "ready", None):
                        aux.connect()
                    aux.request("meeting_agent_cancel", job_id=job_id, timeout=10.0)
                finally:
                    aux.close()
        except Exception:
            logger.debug("Remote meeting_agent_cancel failed", exc_info=True)

    def _poll_job(
        self,
        job_id: str,
        *,
        cancel_event: threading.Event | None = None,
        on_activity: Callable[[Any], None] | None = None,
        timeout_s: float = AGENT_TURN_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        cursor = 0
        with self._job_lock:
            self._active_job_id = job_id
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self.cancel_active_job()
                    return {"status": "canceled", "error": "Canceled.", "result": None}
                if time.monotonic() >= deadline:
                    self.cancel_active_job()
                    return {
                        "status": "failed",
                        "error": f"Remote meeting AI timed out after {int(timeout_s)}s.",
                        "result": None,
                    }
                status_reply = self.request(
                    "meeting_agent_status",
                    job_id=job_id,
                    activity_cursor=cursor,
                    timeout=30.0,
                )
                ticks = status_reply.get("activity") or []
                if isinstance(ticks, list) and on_activity is not None:
                    for tick in ticks:
                        if isinstance(tick, dict):
                            try:
                                on_activity(SimpleNamespace(**tick))
                            except Exception:
                                logger.debug("Activity callback raised", exc_info=True)
                next_cursor = status_reply.get("next_activity_cursor")
                if isinstance(next_cursor, int) and next_cursor >= cursor:
                    cursor = next_cursor
                state = str(status_reply.get("status") or "")
                if state in ("completed", "failed", "canceled"):
                    return status_reply
                if cancel_event is not None:
                    cancel_event.wait(poll_interval_s)
                else:
                    time.sleep(poll_interval_s)
        finally:
            with self._job_lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def run_agent_turn(
        self,
        meeting_id: str,
        payload: CheckpointPayload,
        *,
        system_prompt: str | None = None,
        known_segment_ids: Sequence[str] = (),
        extra: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        on_activity: Callable[[Any], None] | None = None,
        timeout_s: float = AGENT_TURN_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> dict[str, Any]:
        """Start ``meeting_agent_turn`` on the host and poll until completion."""
        body: dict[str, Any] = {
            "state_snapshot": payload.state_snapshot,
            "new_segments": list(payload.new_segments or []),
            "known_segment_ids": list(known_segment_ids or ()),
            "is_consolidation": bool(payload.is_consolidation),
            "is_polish": bool(payload.is_polish),
            "is_notes": bool(payload.is_notes),
        }
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if extra:
            body["extra"] = dict(extra)
        started = self.request(
            "meeting_agent_turn",
            meeting_id=meeting_id,
            request_id=payload.request_id,
            payload=_json_payload(body),
            timeout=30.0,
        )
        job_id = str(started.get("job_id") or "")
        if not job_id:
            raise RemoteEngineError("Host did not return a meeting job id.")
        outcome = self._poll_job(
            job_id,
            cancel_event=cancel_event,
            on_activity=on_activity,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        if outcome.get("status") == "completed" and isinstance(outcome.get("result"), dict):
            return dict(outcome["result"])
        return {
            "ok": False,
            "recorded_ops": [],
            "error": str(outcome.get("error") or outcome.get("status") or "Meeting AI failed."),
            "usage": {},
        }

    def generate_custom_report(
        self,
        meeting: dict[str, Any],
        state_snapshot: dict[str, Any],
        segments: list[dict[str, Any]],
        request_text: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_s: float = CUSTOM_REPORT_TIMEOUT_S,
        poll_interval_s: float = 0.5,
    ) -> dict[str, Any]:
        """Run ``meeting_custom_report`` asynchronously on the host and return the report."""
        body = {
            "meeting": dict(meeting or {}),
            "state_snapshot": dict(state_snapshot or {}),
            "segments": list(segments or []),
            "request": str(request_text or ""),
        }
        started = self.request(
            "meeting_custom_report",
            payload=_json_payload(body),
            timeout=30.0,
        )
        job_id = str(started.get("job_id") or "")
        if not job_id:
            raise RemoteEngineError("Host did not return a custom report job id.")
        outcome = self._poll_job(
            job_id,
            cancel_event=cancel_event,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        if outcome.get("status") == "completed" and isinstance(outcome.get("result"), dict):
            return dict(outcome["result"])
        raise RuntimeError(
            str(outcome.get("error") or "The paired host could not generate the report.")
        )


# ----------------------------------------------------------- RemoteAgentCore


def _remap_op_ids(op: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """Remap entity ids minted during a host turn to the client's local ids."""
    if not id_map:
        return op
    remapped = copy.deepcopy(op)
    for key in ("id", "question_id", "participant_id"):
        val = remapped.get(key)
        if isinstance(val, str) and val in id_map:
            remapped[key] = id_map[val]
    data = remapped.get("data")
    if isinstance(data, dict):
        owner = data.get("owner_participant_id")
        if isinstance(owner, str) and owner in id_map:
            data["owner_participant_id"] = id_map[owner]
    return remapped


def replay_recorded_ops(
    tools: AgentToolHost,
    recorded_ops: Sequence[dict[str, Any]],
) -> list[OpResult]:
    """Replay host-recorded validated operations through the client's real ``AgentToolHost``."""
    op_results: list[OpResult] = []
    id_map: dict[str, str] = {}
    for entry in recorded_ops or ():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        host_target_id = entry.get("host_target_id")
        if kind == "patch":
            raw_op = entry.get("op")
            if not isinstance(raw_op, dict):
                continue
            op = _remap_op_ids(raw_op, id_map)
            results = list(tools.apply_agent_ops([op]))
            op_results.extend(results)
            if (
                results
                and results[0].ok
                and isinstance(host_target_id, str)
                and host_target_id
                and results[0].target_id
            ):
                id_map[host_target_id] = results[0].target_id
        elif kind == "ask_question":
            result = tools.ask_question(
                str(entry.get("text") or ""),
                list(entry.get("evidence") or []),
            )
            op_results.append(result)
            if (
                result.ok
                and isinstance(host_target_id, str)
                and host_target_id
                and result.target_id
            ):
                id_map[host_target_id] = result.target_id
        elif kind == "resolve_question":
            qid = str(entry.get("question_id") or "")
            qid = id_map.get(qid, qid)
            result = tools.resolve_question(
                qid,
                str(entry.get("answer_text") or ""),
                float(entry.get("confidence") or 0.0),
                list(entry.get("evidence") or []),
            )
            op_results.append(result)
    return op_results


class RemoteAgentCore:
    """``meeting.interfaces.AgentCore`` backed by a paired OpenWhisper host.

    The client needs no Node/Bun/Pi/OpenCode installation and no API keys.
    Every turn runs on the host and returns validated state-patch ops, which
    are replayed locally through the client's ``StoreToolHost`` so client-side
    persistence, audit trails, and human-override rules remain authoritative.
    """

    def __init__(
        self,
        *,
        client: RemoteMeetingClient | None = None,
        connect_fn: Callable[[], Any] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        timeout_s: float = AGENT_TURN_TIMEOUT_S,
    ) -> None:
        self._owns_client = client is None
        self._client = client or RemoteMeetingClient(connect_fn=connect_fn)
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._cfg: AgentConfig | None = None
        self._tools: AgentToolHost | None = None
        self._healthy = False
        self._activity_cb: Callable[[Any], None] | None = None
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    def set_activity_callback(self, cb: Callable[[Any], None] | None) -> None:
        self._activity_cb = cb

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        self._cfg = cfg
        self._tools = tools
        self._cancel_event.clear()
        caps = self._client.capabilities()
        if caps.get("meeting_ai") is not True:
            self._healthy = False
            raise RuntimeError(
                "Meeting AI is not enabled on the paired host. Enable "
                "\"Meeting AI & Speaker ID\" in Settings → Remote engine on the host."
            )
        self._healthy = True

    def _known_segment_ids(self, payload: CheckpointPayload) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for seg in list(payload.new_segments or []) + list(
            (payload.state_snapshot or {}).get("recent_transcript_context") or []
        ):
            if isinstance(seg, dict) and isinstance(seg.get("id"), str) and seg["id"] not in seen:
                seen.add(seg["id"])
                ids.append(seg["id"])
        return ids

    def _run_turn(self, payload: CheckpointPayload) -> AgentResult:
        if self._cfg is None or self._tools is None:
            return AgentResult(ok=False, error="Remote agent core is not initialized.")
        self._cancel_event.clear()
        try:
            turn = self._client.run_agent_turn(
                self._cfg.meeting_id,
                payload,
                system_prompt=self._cfg.system_prompt or None,
                known_segment_ids=self._known_segment_ids(payload),
                extra=self._cfg.extra or None,
                cancel_event=self._cancel_event,
                on_activity=self._activity_cb,
                timeout_s=self._timeout_s,
                poll_interval_s=self._poll_interval_s,
            )
        except (RemoteEngineError, RemoteRequestError) as exc:
            code = getattr(exc, "code", None)
            if code in ("forbidden", "unsupported") or isinstance(exc, RemoteEngineError):
                self._healthy = False
            return AgentResult(ok=False, error=str(exc))
        except Exception as exc:
            logger.warning("Remote meeting agent turn failed: %s", exc, exc_info=True)
            return AgentResult(ok=False, error=str(exc))

        recorded = turn.get("recorded_ops") or []
        op_results = replay_recorded_ops(self._tools, recorded)
        return AgentResult(
            ok=bool(turn.get("ok")),
            op_results=op_results,
            error=turn.get("error"),
            usage=dict(turn.get("usage") or {}),
        )

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        return self._run_turn(payload)

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        if not payload.is_consolidation:
            payload = CheckpointPayload(
                request_id=payload.request_id,
                state_snapshot=payload.state_snapshot,
                new_segments=payload.new_segments,
                is_consolidation=True,
                is_polish=False,
                is_notes=False,
            )
        return self._run_turn(payload)

    def cancel(self) -> None:
        self._cancel_event.set()
        self._client.cancel_active_job()

    def is_healthy(self) -> bool:
        return self._healthy

    def shutdown(self) -> None:
        self._healthy = False
        self._cancel_event.set()
        self._client.cancel_active_job()
        if self._owns_client:
            self._client.close()


# ---------------------------------------------- probe / custom report helpers


def remote_speaker_diarization_available(
    *,
    connect_fn: Callable[[], Any] | None = None,
) -> bool:
    """True when a paired host is reachable and advertises ``speaker_diarization``."""
    if connect_fn is None and not has_paired_host():
        return False
    client = RemoteMeetingClient(connect_fn=connect_fn)
    try:
        return client.supports("speaker_diarization")
    except Exception:
        return False
    finally:
        client.close()


def request_remote_custom_report(
    meeting: dict[str, Any],
    state_snapshot: dict[str, Any],
    segments: list[dict[str, Any]],
    request_text: str,
    *,
    connect_fn: Callable[[], Any] | None = None,
    cancel_event: threading.Event | None = None,
    timeout_s: float = CUSTOM_REPORT_TIMEOUT_S,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    """Generate a tailored meeting report on the paired host."""
    client = RemoteMeetingClient(connect_fn=connect_fn)
    try:
        caps = client.capabilities()
        if caps.get("meeting_ai") is not True:
            raise RuntimeError(
                "Meeting AI is not enabled on the paired host. Enable "
                "\"Meeting AI & Speaker ID\" in Settings → Remote engine on the host."
            )
        return client.generate_custom_report(
            meeting,
            state_snapshot,
            segments,
            request_text,
            cancel_event=cancel_event,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
    finally:
        client.close()


# ------------------------------------------------ chunked host file decode RPC


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise RuntimeError("Transcription canceled")


def _upload_for_file_decode(
    conn: Any,
    audio_path: str,
    *,
    mode: str,
    language: str | None = None,
    phrases: Sequence[str] = (),
    should_cancel: Callable[[], bool] | None = None,
    timeout_s: float = FILE_DECODE_TIMEOUT_S,
) -> str:
    """Chunk-upload ``audio_path`` to the host and return its ``upload_id``."""
    caps = (getattr(conn, "ready", None) or {}).get("capabilities") or {}
    if isinstance(caps, dict) and caps.get("file_decode") is False:
        raise RuntimeError("The paired host does not support remote file decoding.")

    size = os.path.getsize(audio_path)
    if size <= 0:
        raise ValueError("Audio file is empty.")
    sha = _file_sha256(audio_path)
    filename = os.path.basename(audio_path)

    begin_fields: dict[str, Any] = {
        "filename": filename,
        "size": size,
        "sha256": sha,
        "mode": mode,
    }
    if language is not None:
        begin_fields["language"] = language
    if phrases:
        begin_fields["phrases"] = list(phrases)

    started = conn.request("file_decode_begin", timeout=30.0, **begin_fields)
    upload_id = str(started.get("upload_id") or "")
    if not upload_id:
        raise RemoteEngineError("Host did not return a file decode upload id.")
    chunk_bytes = int(started.get("chunk_bytes") or protocol.RECORD_CHUNK_BYTES)
    chunk_bytes = max(1024, min(chunk_bytes, protocol.RECORD_CHUNK_BYTES))

    try:
        offset = 0
        with open(audio_path, "rb") as handle:
            while offset < size:
                _check_cancel(should_cancel)
                handle.seek(offset)
                chunk = handle.read(chunk_bytes)
                if not chunk:
                    break
                reply = conn.request(
                    "file_decode_put",
                    upload_id=upload_id,
                    offset=offset,
                    payload=chunk,
                    timeout=min(timeout_s, 60.0),
                )
                received = int(reply.get("received", offset + len(chunk)))
                if reply.get("resume"):
                    offset = received
                else:
                    offset = received
    except BaseException:
        try:
            conn.request("file_decode_abort", upload_id=upload_id, timeout=5.0)
        except Exception:
            pass
        raise
    return upload_id


def decode_audio_windows_remote(
    audio_path: str,
    *,
    connect_fn: Callable[[], Any] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout_s: float = FILE_DECODE_TIMEOUT_S,
) -> Iterator[tuple[float, np.ndarray]]:
    """Decode a compressed media file on the paired host and yield 16 kHz float32 windows."""
    if connect_fn is None and not has_paired_host():
        raise RuntimeError(
            "Decoding compressed audio files requires PyAV/FFmpeg locally or a "
            "paired OpenWhisper host."
        )
    conn = (connect_fn or _default_connect)()
    upload_id = ""
    finished = False
    try:
        if not getattr(conn, "ready", None):
            conn.connect()
        upload_id = _upload_for_file_decode(
            conn,
            audio_path,
            mode="windows",
            should_cancel=should_cancel,
            timeout_s=timeout_s,
        )
        window_offset = 0
        while True:
            _check_cancel(should_cancel)
            page = conn.request(
                "file_decode_finish",
                upload_id=upload_id,
                window_offset=window_offset,
                timeout=timeout_s,
            )
            for item in page.get("windows") or ():
                _check_cancel(should_cancel)
                raw_pcm = base64.b64decode(item["pcm_b64"])
                samples = protocol.decode_audio(raw_pcm)
                yield float(item.get("offset_s") or 0.0), samples
            if page.get("eof", True):
                finished = True
                break
            next_index = int(page.get("next_index", window_offset))
            if next_index <= window_offset:
                finished = True
                break
            window_offset = next_index
    finally:
        if upload_id and not finished:
            try:
                conn.request("file_decode_abort", upload_id=upload_id, timeout=5.0)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def transcribe_audio_file_remote(
    audio_path: str,
    *,
    language: str | None = None,
    phrases: Sequence[str] = (),
    connect_fn: Callable[[], Any] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout_s: float = FILE_DECODE_TIMEOUT_S,
) -> dict[str, Any]:
    """Upload a compressed file to the paired host for direct PyAV decode + transcription."""
    if connect_fn is None and not has_paired_host():
        raise RuntimeError(
            "Transcribing compressed audio files without PyAV requires a paired OpenWhisper host."
        )
    conn = (connect_fn or _default_connect)()
    upload_id = ""
    finished = False
    try:
        if not getattr(conn, "ready", None):
            conn.connect()
        upload_id = _upload_for_file_decode(
            conn,
            audio_path,
            mode="transcribe",
            language=language,
            phrases=phrases,
            should_cancel=should_cancel,
            timeout_s=timeout_s,
        )
        _check_cancel(should_cancel)
        result = conn.request(
            "file_decode_finish",
            upload_id=upload_id,
            timeout=timeout_s,
        )
        finished = True
        return dict(result)
    finally:
        if upload_id and not finished:
            try:
                conn.request("file_decode_abort", upload_id=upload_id, timeout=5.0)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def probe_audio_remote(
    audio_path: str,
    *,
    connect_fn: Callable[[], Any] | None = None,
    timeout_s: float = 60.0,
) -> tuple[float, int, int]:
    """Probe ``(duration_seconds, sample_rate, channels)`` of a compressed file on the host."""
    if connect_fn is None and not has_paired_host():
        raise RuntimeError(
            "Probing compressed audio files without PyAV requires a paired OpenWhisper host."
        )
    conn = (connect_fn or _default_connect)()
    upload_id = ""
    finished = False
    try:
        if not getattr(conn, "ready", None):
            conn.connect()
        upload_id = _upload_for_file_decode(
            conn,
            audio_path,
            mode="probe",
            timeout_s=timeout_s,
        )
        reply = conn.request(
            "file_decode_finish",
            upload_id=upload_id,
            timeout=timeout_s,
        )
        finished = True
        return (
            float(reply["duration_seconds"]),
            int(reply["sample_rate"]),
            int(reply["channels"]),
        )
    finally:
        if upload_id and not finished:
            try:
                conn.request("file_decode_abort", upload_id=upload_id, timeout=5.0)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


from meeting.diarize.embedder import RemoteSpeakerEmbedder  # noqa: E402, F401
