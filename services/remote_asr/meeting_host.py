"""Host-side services for Meeting Mode AI, speaker diarization, and file decode.

Plugged into ``SpeechHost`` and ``RemoteEngineService`` (see ``register_on_service``
and ``register_on_host``):

* **Speaker diarization** (``speaker_embed``): runs the host's WeSpeaker ONNX
  ``SpeakerEmbedder`` lazily under a lock on 16 kHz mono PCM and returns an
  L2-normalized float32 vector.
* **Meeting intelligence** (``meeting_agent_turn``, ``meeting_agent_status``,
  ``meeting_agent_cancel``, ``meeting_custom_report``): runs the host's
  configured ``AgentCore`` or custom-report generator asynchronously on a
  background thread against a client-supplied meeting snapshot using a
  host-side ``RecordingToolHost`` (ephemeral in-memory ``MeetingStateStore``),
  recording validated state-patch operations for the client to replay locally.
  Host API keys, host database rows, and host knowledge folders are never
  exposed to paired clients.
* **Host file decode** (``file_decode_begin``, ``file_decode_put``,
  ``file_decode_finish``, ``file_decode_abort``): accepts chunked, hash-verified
  uploads of compressed media files (MP3/M4A/MP4/OGG/FLAC/etc.) into a
  sandboxed per-device staging directory, decodes them with the host's PyAV,
  and returns 16 kHz PCM windows, audio header metadata, or a transcript.

Every meeting AI operation rechecks the host's ``remote_host_meeting_ai``
opt-in on every request.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from meeting.diarize.embedder import EmbedderUnavailable, SpeakerEmbedder
from meeting.interfaces import AgentConfig, CheckpointPayload, OpResult
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from services.remote_asr import protocol

logger = logging.getLogger(__name__)

#: Provider/core id used by clients that delegate Meeting AI / Speaker ID to the host.
REMOTE_HOST_PROVIDER = "remote_host"

#: Meeting AI RPC operations handled by ``HostMeetingServices.handle_meeting_ai``.
MEETING_AI_OPS = frozenset({
    "speaker_embed",
    "meeting_agent_turn",
    "meeting_agent_status",
    "meeting_agent_cancel",
    "meeting_custom_report",
})

#: File decode RPC operations handled by ``HostMeetingServices.handle_file_decode``.
FILE_DECODE_OPS = frozenset({
    "file_decode_begin",
    "file_decode_put",
    "file_decode_finish",
    "file_decode_abort",
})

#: Bounds on requests and staging resources.
MAX_SPEAKER_AUDIO_SECONDS = 60.0
MAX_MEETING_JSON_PAYLOAD_BYTES = 3 * 1024 * 1024
MAX_FILE_DECODE_BYTES = 512 * 1024 * 1024
FILE_DECODE_CHUNK_BYTES = protocol.RECORD_CHUNK_BYTES
#: Windows per paged ``file_decode_finish`` reply (~3.8 MB base64, well inside 8 MB).
MAX_WINDOWS_PER_PAGE = 3
MAX_ACTIVE_JOBS_PER_DEVICE = 4
MAX_FILE_DECODE_SESSIONS_PER_DEVICE = 4
MAX_ACTIVITY_TICKS = 100
JOB_TTL_S = 15 * 60.0
FILE_DECODE_TTL_S = 30 * 60.0
_FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024

#: Extensions permitted for host-side compressed file decode.
ALLOWED_MEDIA_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".flac", ".aac",
    ".wma", ".webm", ".mkv", ".mov", ".m4v", ".amr", ".aiff", ".aif",
    ".caf", ".mp2", ".m4b",
})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")


class MeetingServiceError(RuntimeError):
    """Domain error carrying a protocol error ``code`` for ``SpeechHost``."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


def _default_meeting_ai_enabled() -> bool:
    """Read the host's ``remote_host_meeting_ai`` opt-in (fail-closed)."""
    try:
        from services.remote_asr import settings as remote_settings

        getter = getattr(remote_settings, "host_meeting_ai", None)
        if callable(getter):
            return getter() is True
    except Exception:
        logger.debug("Could not read host_meeting_ai helper", exc_info=True)
    try:
        from services.settings import SettingsKey, settings_manager

        key = getattr(SettingsKey, "REMOTE_HOST_MEETING_AI", None)
        if key is not None:
            return key.read(settings_manager.load_settings((key,))) is True
        raw = settings_manager.load_all_settings().get("remote_host_meeting_ai")
        return raw is True
    except Exception:
        logger.warning("Could not read remote_host_meeting_ai setting", exc_info=True)
        return False


def _default_speaker_model_path() -> str | None:
    try:
        from services.components import speaker_model_path

        return speaker_model_path()
    except Exception:
        return None


def _has_onnxruntime() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _has_pyav() -> bool:
    from services.local_asr.audio import has_pyav

    return has_pyav()


def _safe_device_id(device: dict | None) -> str:
    device_id = (device or {}).get("id")
    if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
        raise ValueError("Unknown or invalid device id.")
    return device_id


def _merge_json_request(header: dict, payload: bytes) -> dict:
    """Merge a large UTF-8 JSON ``payload`` with ``header`` (``header`` wins on ``id``/``op``)."""
    merge_fn = getattr(protocol, "merge_json_payload", None)
    if callable(merge_fn):
        merged = merge_fn(header, payload)
        if isinstance(merged, dict):
            return merged
    if not payload:
        return dict(header)
    if len(payload) > MAX_MEETING_JSON_PAYLOAD_BYTES:
        raise ValueError("Meeting request payload is too large.")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Meeting request payload must be UTF-8 JSON.") from exc
    if not isinstance(body, dict):
        raise ValueError("Meeting request payload must be a JSON object.")
    return {**body, **header}


def _sanitize_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "requests"):
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            out[key] = val
    return out


def _sanitize_sources(sources: Any) -> dict[str, Any]:
    if not isinstance(sources, dict):
        return {}
    allowed = (
        "transcript_lines", "duration_s", "transcript_complete", "tools_used",
        "past_meetings", "knowledge_folder", "model", "provider",
    )
    out: dict[str, Any] = {}
    for key in allowed:
        if key in sources:
            out[key] = sources[key]
    return out


def _validate_media_filename(filename: Any) -> str:
    """Return a validated lowercase media extension, or raise ValueError."""
    if not isinstance(filename, str) or not filename or len(filename) > 200:
        raise ValueError("Invalid media filename.")
    if any(ch in filename for ch in ("/", "\\", "\x00")) or ".." in filename:
        raise ValueError("Invalid media filename.")
    if any(ord(ch) < 32 for ch in filename):
        raise ValueError("Invalid media filename.")
    if os.path.basename(filename) != filename:
        raise ValueError("Invalid media filename.")
    _, ext = os.path.splitext(filename)
    ext_lower = ext.lower()
    if ext_lower not in ALLOWED_MEDIA_EXTENSIONS:
        raise ValueError(f"Unsupported media file type: {ext_lower or '(none)'}")
    return ext_lower


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ embedder


class _HostSpeakerEmbedder:
    """Thread-safe lazy loader for the host's ONNX ``SpeakerEmbedder``."""

    def __init__(
        self,
        model_path_fn: Callable[[], str | None] | None = None,
        embedder_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._model_path_fn = model_path_fn or _default_speaker_model_path
        self._custom_factory = embedder_factory is not None
        self._embedder_factory = embedder_factory or SpeakerEmbedder
        self._lock = threading.Lock()
        self._embedder: Any | None = None
        self._loaded_path: str | None = None

    def is_ready(self) -> bool:
        try:
            path = self._model_path_fn()
        except Exception:
            return False
        if not path or not os.path.isfile(path):
            return False
        if not self._custom_factory and not _has_onnxruntime():
            return False
        return True

    def embed(self, audio_f32_16k: np.ndarray) -> list[float] | None:
        with self._lock:
            path = self._model_path_fn()
            if not path or not os.path.isfile(path):
                raise EmbedderUnavailable(
                    "No speaker embedding model is installed on the host."
                )
            if (
                self._embedder is None
                or self._loaded_path != path
                or not getattr(self._embedder, "available", False)
            ):
                self._embedder = self._embedder_factory(path)
                self._loaded_path = path
            vec = self._embedder.embed(audio_f32_16k)
        if vec is None:
            return None
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(arr))
        if not np.isfinite(norm) or norm < 1e-8:
            return None
        normalized = (arr / norm).astype(np.float32)
        return [float(x) for x in normalized.tolist()]


# ------------------------------------------------------- proxy AgentToolHost


def _collect_snapshot_segment_ids(snapshot: dict[str, Any]) -> set[str]:
    """Collect segment ids already referenced in ``snapshot`` so updates stay valid."""
    ids: set[str] = set()
    for seg in snapshot.get("recent_transcript_context") or ():
        if isinstance(seg, dict) and isinstance(seg.get("id"), str) and seg["id"]:
            ids.add(seg["id"])
    cards = snapshot.get("cards") or {}
    if isinstance(cards, dict):
        for items in cards.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                for sid in item.get("evidence") or ():
                    if isinstance(sid, str) and sid:
                        ids.add(sid)
    for question in snapshot.get("questions") or ():
        if not isinstance(question, dict):
            continue
        for sid in question.get("evidence") or ():
            if isinstance(sid, str) and sid:
                ids.add(sid)
    return ids


class RecordingToolHost:
    """Host-side proxy ``AgentToolHost`` over an ephemeral in-memory state store.

    Validates every op the host's ``AgentCore`` emits against a reconstructed
    ``MeetingStateStore`` (without writing anything to the host's database) and
    records every accepted op so the client can replay it through its own real
    ``StoreToolHost``.

    Read-only recall tools (``search_past_meetings``, ``search_context_files``)
    are deliberately disabled here so a paired client can never read the host
    owner's private meetings or knowledge folder.
    """

    def __init__(
        self,
        state_snapshot: dict[str, Any],
        segments: list[dict[str, Any]] | None = None,
        *,
        known_segment_ids: Iterable[str] = (),
    ) -> None:
        self._segments: dict[str, dict[str, Any]] = {}
        for seg in segments or ():
            if isinstance(seg, dict) and isinstance(seg.get("id"), str) and seg["id"]:
                self._segments[seg["id"]] = dict(seg)
        for seg in (state_snapshot or {}).get("recent_transcript_context") or ():
            if isinstance(seg, dict) and isinstance(seg.get("id"), str) and seg["id"]:
                self._segments.setdefault(seg["id"], dict(seg))

        self._known_ids: set[str] = (
            set(self._segments)
            | _collect_snapshot_segment_ids(state_snapshot or {})
            | {str(sid) for sid in known_segment_ids if isinstance(sid, str) and sid}
        )
        state = MeetingState.from_dict(copy.deepcopy(state_snapshot or {}))
        self._store = MeetingStateStore(
            state,
            repository=None,
            segment_handler=self._handle_segment_op,
            segment_exists=self.segment_exists,
        )
        self._lock = threading.Lock()
        self.recorded_ops: list[dict[str, Any]] = []

    def _handle_segment_op(self, result: OpResult) -> dict[str, Any] | None:
        """In-memory segment handler so polish passes (``revise_segment_text``) succeed."""
        effect = result.effect or {}
        op_name = (result.op or {}).get("op")
        segment_id = effect.get("segment_id")
        if op_name != "revise_segment_text" or not isinstance(segment_id, str):
            return None
        prior = self._segments.get(segment_id)
        new_text = str(effect.get("text") or "")
        if prior is not None:
            old_text = str(prior.get("text") or "")
            updated = dict(prior)
            updated["text"] = new_text
            updated["original_text"] = new_text
            self._segments[segment_id] = updated
            result.effect = {
                "entity": "segment_text",
                "segment_id": segment_id,
                "text": new_text,
                "segment": updated,
            }
            return {
                "op": "revise_segment_text",
                "segment_id": segment_id,
                "text": old_text,
                "evidence": [segment_id],
            }
        return None

    def segment_exists(self, segment_id: str) -> bool:
        return isinstance(segment_id, str) and segment_id in self._known_ids

    def apply_agent_ops(self, ops: list[dict[str, Any]]) -> list[OpResult]:
        with self._lock:
            results = self._store.apply("agent", "agent", list(ops))
            for result in results:
                if result.ok:
                    self.recorded_ops.append({
                        "kind": "patch",
                        "op": copy.deepcopy(result.op),
                        "host_target_id": result.target_id,
                    })
            return results

    def ask_question(self, text: str, evidence: list[str]) -> OpResult:
        ev = list(evidence or [])
        with self._lock:
            result = self._store.apply(
                "agent", "agent", [{"op": "ask_question", "text": text, "evidence": ev}]
            )[0]
            if result.ok:
                self.recorded_ops.append({
                    "kind": "ask_question",
                    "text": text,
                    "evidence": ev,
                    "host_target_id": result.target_id,
                })
            return result

    def resolve_question(
        self,
        question_id: str,
        answer_text: str,
        confidence: float,
        evidence: list[str],
    ) -> OpResult:
        ev = list(evidence or [])
        with self._lock:
            result = self._store.apply(
                "agent",
                "agent",
                [{
                    "op": "resolve_question",
                    "question_id": question_id,
                    "answer_text": answer_text,
                    "confidence": float(confidence),
                    "evidence": ev,
                }],
            )[0]
            if result.ok:
                self.recorded_ops.append({
                    "kind": "resolve_question",
                    "question_id": question_id,
                    "answer_text": answer_text,
                    "confidence": float(confidence),
                    "evidence": ev,
                    "host_target_id": result.target_id,
                })
            return result

    def search_past_meetings(
        self,
        query: str = "",
        meeting_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "disabled": True,
            "text": "Past-meeting recall is not available on the remote host.",
            "hits": [],
        }

    def search_context_files(
        self,
        query: str = "",
        relative_path: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "disabled": True,
            "text": "Knowledge-folder search is not available on the remote host.",
            "hits": [],
        }


# ------------------------------------------------------------- async job state


@dataclass
class _MeetingJob:
    job_id: str
    device_id: str
    kind: str  # "agent_turn" | "custom_report"
    status: str = "running"  # "running" | "completed" | "failed" | "canceled"
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    core: Any | None = None
    activity: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None


# ----------------------------------------------------------- file decode state


@dataclass
class _FileDecodeSession:
    upload_id: str
    device_id: str
    mode: str  # "windows" | "transcribe" | "probe"
    ext: str
    size: int
    sha256: str
    session_dir: str
    staged_path: str
    language: str | None = None
    phrases: tuple[str, ...] = ()
    received: int = 0
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    decoded_windows: list[tuple[float, str]] | None = None  # [(offset_s, pcm_path)]
    lock: threading.Lock = field(default_factory=threading.Lock)


class _FileDecodeManager:
    """Chunked, hash-verified host-side media file decode for PyAV-free clients."""

    def __init__(
        self,
        *,
        staging_root: str | None = None,
        engine_provider: Callable[[], Any] | None = None,
        windows_fn: Callable[[str], Iterable[tuple[float, np.ndarray]]] | None = None,
        probe_fn: Callable[[str], tuple[float, int, int]] | None = None,
        max_file_bytes: int = MAX_FILE_DECODE_BYTES,
    ) -> None:
        self._owns_root = staging_root is None
        self._root = (
            os.path.abspath(staging_root)
            if staging_root is not None
            else tempfile.mkdtemp(prefix="openwhisper_file_decode_")
        )
        self._engine_provider = engine_provider
        self._custom_decoder = windows_fn is not None or probe_fn is not None
        self._windows_fn = windows_fn
        self._probe_fn = probe_fn
        self._max_file_bytes = max(1, int(max_file_bytes))
        self._lock = threading.Lock()
        self._sessions: dict[str, _FileDecodeSession] = {}

    def available(self) -> bool:
        return self._custom_decoder or _has_pyav()

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            shutil.rmtree(session.session_dir, ignore_errors=True)
        if self._owns_root:
            shutil.rmtree(self._root, ignore_errors=True)

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - FILE_DECODE_TTL_S
        stale = [
            uid for uid, sess in self._sessions.items() if sess.updated_at < cutoff
        ]
        for uid in stale:
            sess = self._sessions.pop(uid, None)
            if sess is not None:
                shutil.rmtree(sess.session_dir, ignore_errors=True)

    def _get_session(self, device_id: str, upload_id: Any) -> _FileDecodeSession:
        if not isinstance(upload_id, str) or not _JOB_ID_RE.match(upload_id):
            raise ValueError("Invalid file decode upload id.")
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(upload_id)
        if session is None or session.device_id != device_id:
            raise LookupError("Unknown file decode session.")
        return session

    def _drop_session(self, upload_id: str, session_dir: str) -> None:
        with self._lock:
            self._sessions.pop(upload_id, None)
        shutil.rmtree(session_dir, ignore_errors=True)

    def handle(self, op: str, header: dict, payload: bytes, device: dict) -> dict:
        device_id = _safe_device_id(device)
        if op == "file_decode_begin":
            return self._begin(device_id, header)
        if op == "file_decode_put":
            return self._put(device_id, header, payload)
        if op == "file_decode_finish":
            return self._finish(device_id, header)
        if op == "file_decode_abort":
            return self._abort(device_id, header)
        raise ValueError(f"Unknown file decode operation: {op!r}")

    def _begin(self, device_id: str, header: dict) -> dict:
        if not self.available():
            raise NotImplementedError("This host does not have PyAV available for file decode.")
        ext = _validate_media_filename(header.get("filename"))
        size = header.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > self._max_file_bytes
        ):
            raise ValueError("Invalid media file size.")
        sha = header.get("sha256")
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            raise ValueError("Invalid media file SHA-256 hash.")
        mode = header.get("mode", "windows")
        if mode not in ("windows", "transcribe", "probe"):
            raise ValueError("Invalid file decode mode.")
        if mode == "transcribe" and self._engine_provider is None:
            raise NotImplementedError("Host engine is unavailable for file transcription.")
        language = header.get("language")
        if language is not None and not isinstance(language, str):
            raise ValueError("Invalid language.")
        phrases = protocol.header_phrases(header)

        os.makedirs(self._root, exist_ok=True)
        free = shutil.disk_usage(self._root).free
        if size + _FREE_SPACE_MARGIN_BYTES > free:
            raise RuntimeError("Not enough free disk space on the host to decode this file.")

        with self._lock:
            self._prune_locked()
            active = sum(
                1 for sess in self._sessions.values() if sess.device_id == device_id
            )
            if active >= MAX_FILE_DECODE_SESSIONS_PER_DEVICE:
                raise MeetingServiceError(
                    "Too many active file decode uploads for this device.", code="busy"
                )
            upload_id = uuid.uuid4().hex
            device_dir = os.path.join(self._root, device_id)
            session_dir = os.path.join(device_dir, upload_id)
            os.makedirs(session_dir, exist_ok=True)
            # Client filename is never placed on disk; use sanitized extension only.
            staged_path = os.path.join(session_dir, f"input{ext}")
            session = _FileDecodeSession(
                upload_id=upload_id,
                device_id=device_id,
                mode=mode,
                ext=ext,
                size=size,
                sha256=sha,
                session_dir=session_dir,
                staged_path=staged_path,
                language=language,
                phrases=phrases,
            )
            self._sessions[upload_id] = session

        return {
            "upload_id": upload_id,
            "chunk_bytes": FILE_DECODE_CHUNK_BYTES,
        }

    def _put(self, device_id: str, header: dict, payload: bytes) -> dict:
        session = self._get_session(device_id, header.get("upload_id"))
        offset = header.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid chunk offset.")
        if len(payload) > FILE_DECODE_CHUNK_BYTES:
            raise ValueError("File decode chunk is too large.")
        with session.lock:
            if session.decoded_windows is not None:
                raise ValueError("Upload has already finished.")
            if offset != session.received:
                return {
                    "upload_id": session.upload_id,
                    "received": session.received,
                    "complete": session.received == session.size,
                    "resume": True,
                }
            if session.received + len(payload) > session.size:
                raise ValueError("Uploaded data exceeds declared file size.")
            with open(session.staged_path, "ab") as handle:
                handle.write(payload)
            session.received += len(payload)
            session.updated_at = time.monotonic()
            return {
                "upload_id": session.upload_id,
                "received": session.received,
                "complete": session.received == session.size,
            }

    def _finish(self, device_id: str, header: dict) -> dict:
        session = self._get_session(device_id, header.get("upload_id"))
        with session.lock:
            session.updated_at = time.monotonic()
            if session.decoded_windows is None:
                if session.received != session.size or not os.path.isfile(session.staged_path):
                    raise ValueError("File upload is incomplete.")
                if _file_sha256(session.staged_path) != session.sha256:
                    self._drop_session(session.upload_id, session.session_dir)
                    raise ValueError("Uploaded file hash mismatch.")

            if session.mode == "probe":
                try:
                    duration_s, rate, channels = self._probe_file(session.staged_path)
                    return {
                        "duration_seconds": float(duration_s),
                        "sample_rate": int(rate),
                        "channels": int(channels),
                    }
                finally:
                    self._drop_session(session.upload_id, session.session_dir)

            if session.mode == "transcribe":
                try:
                    return self._transcribe_file(session)
                finally:
                    self._drop_session(session.upload_id, session.session_dir)

            # mode == "windows" (supports paged reads without exceeding MAX_REPLY_BYTES)
            return self._finish_windows_page(session, header)

    def _probe_file(self, path: str) -> tuple[float, int, int]:
        if self._probe_fn is not None:
            return self._probe_fn(path)
        from services.audio_processor import AudioProcessor

        return AudioProcessor()._probe_audio_header(path)

    def _decode_windows(self, path: str) -> Iterable[tuple[float, np.ndarray]]:
        if self._windows_fn is not None:
            return self._windows_fn(path)
        from services.local_asr.audio import windows

        return windows(path)

    def _transcribe_file(self, session: _FileDecodeSession) -> dict:
        engine = self._engine_provider() if self._engine_provider is not None else None
        if engine is None:
            raise RuntimeError("No transcription engine is available on the host.")
        texts: list[str] = []
        all_segments: list[dict[str, Any]] = []
        count = 0
        for offset_s, audio_f32 in self._decode_windows(session.staged_path):
            count += 1
            if session.phrases and getattr(engine, "accepts_phrases", False) is True:
                reply = engine.transcribe(
                    audio_f32, session.language, phrases=session.phrases
                )
            else:
                reply = engine.transcribe(audio_f32, session.language)
            text = str((reply or {}).get("text") or "").strip()
            if text:
                texts.append(text)
            for seg in (reply or {}).get("segments") or ():
                if isinstance(seg, dict):
                    shifted = dict(seg)
                    if isinstance(shifted.get("start"), (int, float)):
                        shifted["start"] = float(shifted["start"]) + float(offset_s)
                    if isinstance(shifted.get("end"), (int, float)):
                        shifted["end"] = float(shifted["end"]) + float(offset_s)
                    all_segments.append(shifted)
        return {
            "text": " ".join(texts).strip(),
            "segments": all_segments,
            "window_count": count,
        }

    def _finish_windows_page(self, session: _FileDecodeSession, header: dict) -> dict:
        if session.decoded_windows is None:
            win_dir = os.path.join(session.session_dir, "windows")
            os.makedirs(win_dir, exist_ok=True)
            decoded: list[tuple[float, str]] = []
            try:
                for index, (offset_s, audio_f32) in enumerate(
                    self._decode_windows(session.staged_path)
                ):
                    pcm_bytes = protocol.encode_audio(audio_f32)
                    pcm_path = os.path.join(win_dir, f"w_{index:06d}.pcm")
                    with open(pcm_path, "wb") as handle:
                        handle.write(pcm_bytes)
                    decoded.append((float(offset_s), pcm_path))
            except Exception:
                self._drop_session(session.upload_id, session.session_dir)
                raise
            # Raw uploaded file is no longer needed once decoded into PCM windows.
            try:
                os.remove(session.staged_path)
            except OSError:
                pass
            session.decoded_windows = decoded

        window_offset = header.get("window_offset", 0)
        if isinstance(window_offset, bool) or not isinstance(window_offset, int) or window_offset < 0:
            raise ValueError("Invalid window_offset.")
        max_windows = header.get("max_windows", MAX_WINDOWS_PER_PAGE)
        if isinstance(max_windows, bool) or not isinstance(max_windows, int):
            max_windows = MAX_WINDOWS_PER_PAGE
        page_size = max(1, min(max_windows, MAX_WINDOWS_PER_PAGE))

        total = len(session.decoded_windows)
        if window_offset > total:
            raise ValueError("window_offset is past the end of decoded windows.")
        end = min(total, window_offset + page_size)
        items = []
        for idx in range(window_offset, end):
            offset_s, pcm_path = session.decoded_windows[idx]
            with open(pcm_path, "rb") as handle:
                raw_pcm = handle.read()
            items.append({
                "index": idx,
                "offset_s": offset_s,
                "pcm_b64": base64.b64encode(raw_pcm).decode("ascii"),
            })
        eof = end >= total
        if eof:
            self._drop_session(session.upload_id, session.session_dir)
        return {
            "sample_rate": protocol.SAMPLE_RATE,
            "total_windows": total,
            "windows": items,
            "next_index": end,
            "eof": eof,
        }

    def _abort(self, device_id: str, header: dict) -> dict:
        upload_id = header.get("upload_id")
        if not isinstance(upload_id, str) or not _JOB_ID_RE.match(upload_id):
            return {"aborted": False}
        with self._lock:
            session = self._sessions.get(upload_id)
            if session is None or session.device_id != device_id:
                return {"aborted": False}
            self._sessions.pop(upload_id, None)
        shutil.rmtree(session.session_dir, ignore_errors=True)
        return {"aborted": True}


# ------------------------------------------------------- HostMeetingServices


def _default_route_resolver():
    from services.meeting_agent_route import resolve_meeting_agent_route

    return resolve_meeting_agent_route()


def _default_core_factory(kind: str, payload_dir: str | None):
    from meeting.agent.base import create_agent_core

    return create_agent_core(kind, payload_dir)


def _default_report_generator(
    meeting: dict[str, Any],
    state: dict[str, Any],
    segments: list[dict[str, Any]],
    request: str,
    *,
    provider: str,
    model: str,
    endpoint: dict[str, Any] | None,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    from meeting.custom_report import generate_report

    # Never pass host's MeetingRepository so paired clients cannot search
    # the host owner's private past meetings.
    return generate_report(
        meeting,
        state,
        segments,
        request,
        provider=provider,
        model=model,
        endpoint=endpoint,
        repository=None,
        cancel_event=cancel_event,
    )


class HostMeetingServices:
    """Unified host handler for Phase 2 Meeting AI, Speaker ID, and File Decode.

    Integration into ``SpeechHost`` / ``RemoteEngineService``::

        meeting_services = HostMeetingServices(engine_provider=service._engine)
        meeting_services.register_on_service(service)

    Or directly onto ``SpeechHost``::

        host.set_meeting_ai_handler(
            meeting_services.handle_meeting_ai,
            capabilities=meeting_services.meeting_ai_capabilities,
        )
        host.set_file_decode_handler(meeting_services.handle_file_decode)
    """

    def __init__(
        self,
        *,
        meeting_ai_enabled: Callable[[], bool] | None = None,
        speaker_model_path_fn: Callable[[], str | None] | None = None,
        embedder_factory: Callable[[str], Any] | None = None,
        route_resolver: Callable[[], Any] | None = None,
        core_factory: Callable[[str, str | None], Any] | None = None,
        report_generator: Callable[..., dict[str, Any]] | None = None,
        engine_provider: Callable[[], Any] | None = None,
        file_decode_root: str | None = None,
        windows_fn: Callable[[str], Iterable[tuple[float, np.ndarray]]] | None = None,
        probe_fn: Callable[[str], tuple[float, int, int]] | None = None,
        max_file_decode_bytes: int = MAX_FILE_DECODE_BYTES,
    ) -> None:
        self._meeting_ai_enabled = meeting_ai_enabled or _default_meeting_ai_enabled
        self._embedder = _HostSpeakerEmbedder(
            model_path_fn=speaker_model_path_fn,
            embedder_factory=embedder_factory,
        )
        self._route_resolver = route_resolver or _default_route_resolver
        self._core_factory = core_factory or _default_core_factory
        self._report_generator = report_generator or _default_report_generator
        self._file_decode = _FileDecodeManager(
            staging_root=file_decode_root,
            engine_provider=engine_provider,
            windows_fn=windows_fn,
            probe_fn=probe_fn,
            max_file_bytes=max_file_decode_bytes,
        )
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, _MeetingJob] = {}

    # ---- registration helpers ----

    def register_on_service(self, service: Any) -> None:
        """Wire meeting AI, file decode, and walkthrough records into ``RemoteEngineService``."""
        from services.remote_records.walkthrough_kind import WalkthroughRecords

        if hasattr(service, "_attach_meeting_services"):
            service._attach_meeting_services(self)
        service.register_meeting_ai_handler(
            self.handle_meeting_ai,
            capabilities=self.meeting_ai_capabilities,
        )
        service.register_file_decode_handler(self.handle_file_decode)
        if hasattr(service, "register_record_kind"):
            service.register_record_kind("walkthrough", WalkthroughRecords())

    def register_on_host(self, host: Any) -> None:
        """Wire meeting AI and file decode handlers directly onto a ``SpeechHost``."""
        host.set_meeting_ai_handler(
            self.handle_meeting_ai,
            capabilities=self.meeting_ai_capabilities,
        )
        host.set_file_decode_handler(self.handle_file_decode)

    # ---- capabilities ----

    def _is_meeting_ai_enabled(self) -> bool:
        try:
            return self._meeting_ai_enabled() is True
        except Exception:
            logger.warning("Could not read host meeting AI opt-in", exc_info=True)
            return False

    def _agent_available(self) -> bool:
        try:
            route = self._route_resolver()
        except Exception:
            return False
        kind = getattr(route, "kind", "")
        return bool(kind) and kind != REMOTE_HOST_PROVIDER

    def file_decode_available(self) -> bool:
        return self._file_decode.available()

    def meeting_ai_capabilities(self) -> dict[str, bool]:
        """Capability flags for ``ready["capabilities"]`` (no secrets)."""
        enabled = self._is_meeting_ai_enabled()
        return {
            "meeting_ai": enabled and self._agent_available(),
            "speaker_diarization": enabled and self._embedder.is_ready(),
            "file_decode": self.file_decode_available(),
        }

    # ---- lifecycle ----

    def shutdown(self) -> None:
        """Cancel all running jobs and clean up temporary file decode storage."""
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel_event.set()
            core = job.core
            if core is not None:
                try:
                    core.cancel()
                except Exception:
                    pass
        self._file_decode.shutdown()

    # ---- dispatchers ----

    def handle_meeting_ai(
        self, op: str, header: dict, payload: bytes, device: dict
    ) -> dict:
        """Dispatch ``MEETING_AI_OPS`` with per-request opt-in and device scoping."""
        device_id = _safe_device_id(device)
        if op == "speaker_embed":
            self._require_meeting_ai_enabled()
            return self._handle_speaker_embed(payload)
        if op == "meeting_agent_turn":
            self._require_meeting_ai_enabled()
            req = _merge_json_request(header, payload)
            return self._start_agent_turn(device_id, req)
        if op == "meeting_custom_report":
            self._require_meeting_ai_enabled()
            req = _merge_json_request(header, payload)
            return self._start_custom_report(device_id, req)
        if op == "meeting_agent_status":
            req = _merge_json_request(header, payload)
            return self._get_job_status(device_id, req)
        if op == "meeting_agent_cancel":
            req = _merge_json_request(header, payload)
            return self._cancel_job(device_id, req)
        raise ValueError(f"Unknown meeting AI operation: {op!r}")

    def handle_file_decode(
        self, op: str, header: dict, payload: bytes, device: dict
    ) -> dict:
        """Dispatch ``FILE_DECODE_OPS`` for PyAV-free clients."""
        return self._file_decode.handle(op, header, payload, device)

    # ---- speaker_embed ----

    def _require_meeting_ai_enabled(self) -> None:
        if not self._is_meeting_ai_enabled():
            raise PermissionError(
                "Meeting AI & Speaker ID sharing is disabled on the host. "
                "Enable it in Settings → Remote engine on the host."
            )

    def _handle_speaker_embed(self, payload: bytes) -> dict:
        try:
            audio = protocol.payload_audio(payload)
        except protocol.ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        max_samples = int(MAX_SPEAKER_AUDIO_SECONDS * protocol.SAMPLE_RATE)
        if len(audio) > max_samples:
            raise ValueError("Speaker embedding audio segment is too long.")
        try:
            embedding = self._embedder.embed(audio)
        except EmbedderUnavailable as exc:
            raise MeetingServiceError(str(exc), code="unavailable") from exc
        return {
            "embedding": embedding,
            "dim": len(embedding) if embedding is not None else 0,
        }

    # ---- meeting_agent_turn / custom_report / status / cancel ----

    def _prune_jobs_locked(self) -> None:
        cutoff = time.monotonic() - JOB_TTL_S
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def _register_job(self, device_id: str, kind: str) -> _MeetingJob:
        with self._jobs_lock:
            self._prune_jobs_locked()
            running = sum(
                1
                for job in self._jobs.values()
                if job.device_id == device_id and job.status == "running"
            )
            if running >= MAX_ACTIVE_JOBS_PER_DEVICE:
                raise MeetingServiceError(
                    "Too many meeting AI jobs are already running for this device.",
                    code="busy",
                )
            job = _MeetingJob(job_id=uuid.uuid4().hex, device_id=device_id, kind=kind)
            self._jobs[job.job_id] = job
            return job

    def _resolve_host_route(self):
        route = self._route_resolver()
        kind = getattr(route, "kind", "")
        if not kind:
            raise RuntimeError("No meeting intelligence agent is configured on the host.")
        if kind == REMOTE_HOST_PROVIDER:
            raise ValueError(
                "The host cannot forward meeting intelligence to another remote host."
            )
        return route

    def _start_agent_turn(self, device_id: str, req: dict) -> dict:
        meeting_id = req.get("meeting_id")
        if not isinstance(meeting_id, str) or not meeting_id.strip() or len(meeting_id) > 128:
            raise ValueError("A valid meeting_id is required.")
        state_snapshot = req.get("state_snapshot")
        if not isinstance(state_snapshot, dict):
            raise ValueError("state_snapshot must be an object.")
        new_segments = req.get("new_segments")
        if not isinstance(new_segments, list) or not all(
            isinstance(seg, dict) for seg in new_segments
        ):
            raise ValueError("new_segments must be a list of segment objects.")
        known_ids = req.get("known_segment_ids") or []
        if not isinstance(known_ids, list):
            raise ValueError("known_segment_ids must be a list.")

        is_consolidation = bool(req.get("is_consolidation"))
        is_polish = bool(req.get("is_polish"))
        is_notes = bool(req.get("is_notes"))
        request_id = str(req.get("request_id") or uuid.uuid4().hex)[:128]
        system_prompt = req.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be a string.")
        extra = req.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("extra must be an object.")

        route = self._resolve_host_route()
        job = self._register_job(device_id, "agent_turn")

        worker = threading.Thread(
            target=self._run_agent_turn_job,
            args=(
                job,
                route,
                meeting_id,
                request_id,
                state_snapshot,
                new_segments,
                known_ids,
                is_consolidation,
                is_polish,
                is_notes,
                system_prompt,
                extra or {},
            ),
            name=f"remote-meeting-turn-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
        }

    def _run_agent_turn_job(
        self,
        job: _MeetingJob,
        route: Any,
        meeting_id: str,
        request_id: str,
        state_snapshot: dict[str, Any],
        new_segments: list[dict[str, Any]],
        known_ids: list[Any],
        is_consolidation: bool,
        is_polish: bool,
        is_notes: bool,
        system_prompt: str | None,
        extra: dict[str, Any],
    ) -> None:
        core = None
        try:
            if system_prompt is None:
                from meeting.agent.prompts import build_system_prompt

                system_prompt = build_system_prompt()
            core = self._core_factory(route.kind, getattr(route, "payload_dir", None))
            with self._jobs_lock:
                job.core = core
                if job.cancel_event.is_set():
                    job.status = "canceled"
                    job.error = "Canceled."
                    job.finished_at = time.monotonic()
                    return

            tool_host = RecordingToolHost(
                state_snapshot,
                new_segments,
                known_segment_ids=known_ids,
            )
            # Never accept client API keys; the host's core resolves its own credentials.
            cfg = AgentConfig(
                meeting_id=meeting_id,
                provider=str(getattr(route, "provider", "") or ""),
                model=str(getattr(route, "model", "") or ""),
                api_key=None,
                system_prompt=system_prompt,
                extra=dict(extra),
                endpoint=getattr(route, "endpoint", None),
            )
            core.initialize(cfg, tool_host)

            setter = getattr(core, "set_activity_callback", None)
            if callable(setter):
                setter(lambda act: self._record_activity(job, act))

            if job.cancel_event.is_set():
                with self._jobs_lock:
                    job.status = "canceled"
                    job.error = "Canceled."
                    job.finished_at = time.monotonic()
                return

            payload = CheckpointPayload(
                request_id=request_id,
                state_snapshot=state_snapshot,
                new_segments=new_segments,
                is_consolidation=is_consolidation,
                is_polish=is_polish,
                is_notes=is_notes,
            )
            result = (
                core.consolidate(payload)
                if is_consolidation
                else core.checkpoint(payload)
            )
            with self._jobs_lock:
                if job.cancel_event.is_set():
                    job.status = "canceled"
                    job.error = "Canceled."
                else:
                    job.status = "completed"
                    job.result = {
                        "ok": bool(getattr(result, "ok", False)),
                        "recorded_ops": list(tool_host.recorded_ops),
                        "error": getattr(result, "error", None),
                        "usage": _sanitize_usage(getattr(result, "usage", None)),
                    }
                job.finished_at = time.monotonic()
        except Exception as exc:
            logger.warning("Remote meeting agent turn %s failed: %s", job.job_id, exc, exc_info=True)
            with self._jobs_lock:
                if job.cancel_event.is_set():
                    job.status = "canceled"
                    job.error = "Canceled."
                else:
                    job.status = "failed"
                    job.error = str(exc) or type(exc).__name__
                job.finished_at = time.monotonic()
        finally:
            with self._jobs_lock:
                job.core = None
            if core is not None:
                try:
                    core.shutdown()
                except Exception:
                    logger.debug("Host meeting agent shutdown failed", exc_info=True)

    def _record_activity(self, job: _MeetingJob, activity: Any) -> None:
        tick = {
            "kind": str(getattr(activity, "kind", "") or "update"),
            "label": str(getattr(activity, "label", "") or ""),
            "tool": str(getattr(activity, "tool", "") or ""),
            "pass_kind": str(getattr(activity, "pass_kind", "") or ""),
            "ts": str(getattr(activity, "ts", "") or ""),
        }
        with self._jobs_lock:
            if len(job.activity) < MAX_ACTIVITY_TICKS:
                job.activity.append(tick)

    def _start_custom_report(self, device_id: str, req: dict) -> dict:
        prompt = req.get("request")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Describe the report you want.")
        state_snapshot = req.get("state_snapshot")
        if not isinstance(state_snapshot, dict):
            raise ValueError("state_snapshot must be an object.")
        segments = req.get("segments")
        if not isinstance(segments, list) or not all(
            isinstance(seg, dict) for seg in segments
        ):
            raise ValueError("segments must be a list of segment objects.")
        meeting = req.get("meeting")
        if meeting is None:
            meeting = {}
        if not isinstance(meeting, dict):
            raise ValueError("meeting must be an object.")

        route = self._resolve_host_route()
        job = self._register_job(device_id, "custom_report")

        worker = threading.Thread(
            target=self._run_custom_report_job,
            args=(job, route, meeting, state_snapshot, segments, prompt.strip()),
            name=f"remote-meeting-report-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
        }

    def _run_custom_report_job(
        self,
        job: _MeetingJob,
        route: Any,
        meeting: dict[str, Any],
        state_snapshot: dict[str, Any],
        segments: list[dict[str, Any]],
        request: str,
    ) -> None:
        try:
            report = self._report_generator(
                meeting,
                state_snapshot,
                segments,
                request,
                provider=str(getattr(route, "provider", "") or ""),
                model=str(getattr(route, "model", "") or ""),
                endpoint=getattr(route, "endpoint", None),
                cancel_event=job.cancel_event,
            )
            with self._jobs_lock:
                if job.cancel_event.is_set():
                    job.status = "canceled"
                    job.error = "Canceled."
                else:
                    job.status = "completed"
                    job.result = {
                        "markdown": str(report.get("markdown") or ""),
                        "title": str(report.get("title") or ""),
                        "sources": _sanitize_sources(report.get("sources")),
                        "usage": _sanitize_usage(report.get("usage")),
                    }
                job.finished_at = time.monotonic()
        except Exception as exc:
            logger.warning("Remote custom report %s failed: %s", job.job_id, exc, exc_info=True)
            with self._jobs_lock:
                if job.cancel_event.is_set():
                    job.status = "canceled"
                    job.error = "Canceled."
                else:
                    job.status = "failed"
                    job.error = str(exc) or type(exc).__name__
                job.finished_at = time.monotonic()

    def _get_job_status(self, device_id: str, req: dict) -> dict:
        job_id = req.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            raise ValueError("Invalid job_id.")
        cursor = req.get("activity_cursor", 0)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        with self._jobs_lock:
            self._prune_jobs_locked()
            job = self._jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise LookupError("Unknown meeting AI job.")
            new_activity = list(job.activity[cursor:])
            next_cursor = len(job.activity)
            return {
                "job_id": job.job_id,
                "kind": job.kind,
                "status": job.status,
                "activity": new_activity,
                "next_activity_cursor": next_cursor,
                "result": copy.deepcopy(job.result) if job.result is not None else None,
                "error": job.error,
            }

    def _cancel_job(self, device_id: str, req: dict) -> dict:
        job_id = req.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            raise ValueError("Invalid job_id.")
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise LookupError("Unknown meeting AI job.")
            job.cancel_event.set()
            core = job.core
            if job.status == "running":
                job.status = "canceled"
                job.error = "Canceled."
                job.finished_at = time.monotonic()
            status = job.status
        if core is not None:
            try:
                core.cancel()
            except Exception:
                logger.debug("Host meeting agent cancel raised", exc_info=True)
        return {
            "job_id": job_id,
            "canceled": True,
            "status": status,
        }
