"""Client-side transport and helpers for Phase 1 host-facilitated AI services.

Uses a dedicated :class:`RemoteAIConnectionPool` of paired
:class:`services.remote_asr.client.RemoteConnection` sockets so LLM cleanup,
fast judgments, walkthrough write-ups, and coding-agent Q&A never contend
with the live speech audio WebSocket.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from services.remote_asr import protocol
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import (
    RemoteConnection,
    RemoteConnectionLost,
    RemoteEngineError,
    RemoteRequestError,
)

logger = logging.getLogger(__name__)

#: Idle dedicated AI connections retained in the pool.
_POOL_MAX_IDLE = 4
#: Drop pooled idle connections older than this so NAT/firewall timeouts don't stall.
_POOL_IDLE_S = 115.0
#: Inline base64 threshold for walkthrough screenshots before switching to chunked put.
_INLINE_SHOT_BYTES_LIMIT = 2 * 1024 * 1024
_CANCEL_POLL_S = 0.05
DEFAULT_WRITEUP_POLL_S = 0.5
DEFAULT_WRITEUP_TIMEOUT_S = 360.0
DEFAULT_CLEANUP_TIMEOUT_S = 60.0
DEFAULT_ASK_TIMEOUT_S = 120.0

_CapabilityLabels: dict[str, str] = {
    "text_cleanup": "AI cleanup and text rewrites",
    "fast_judgments": "fast judgments",
    "walkthrough_writeup": "walkthrough AI write-ups",
    "agent_ask": "coding-agent quick answers",
}


class RemoteAICanceled(RuntimeError):
    """Raised when a remote AI call was stopped by its caller's cancel event."""


def has_remote_host(settings: Mapping[str, Any] | None = None) -> bool:
    """True when this computer is paired with a host and has its device token."""
    mapping = dict(settings) if settings is not None else None
    return remote_settings.has_paired_host(mapping)


def check_capability(ready: Mapping[str, Any], capability: str, host_name: str) -> None:
    """Verify that ``ready`` advertises ``capability`` as enabled."""
    capabilities = ready.get("capabilities")
    label = _CapabilityLabels.get(capability, capability)
    if not isinstance(capabilities, Mapping) or capability not in capabilities:
        raise RemoteRequestError(
            f"Update OpenWhisper on {host_name} to use {label} remotely.",
            code="unsupported",
        )
    if capabilities.get(capability) is not True:
        raise RemoteRequestError(
            f"{host_name} isn't sharing {label}. "
            "Turn it on in Settings → Remote engine there.",
            code="forbidden",
        )


class RemoteAIConnectionPool:
    """Thread-safe pool of dedicated paired connections for non-ASR AI calls.

    Never shares a socket with the speech dictation or meeting audio backend.
    Each in-flight call holds its own connection and returns it when finished.
    """

    def __init__(
        self,
        *,
        pairing_loader: Callable[[], remote_settings.ClientPairing | None] | None = None,
        token_loader: Callable[[], str | None] | None = None,
        max_idle: int = _POOL_MAX_IDLE,
        idle_s: float = _POOL_IDLE_S,
    ) -> None:
        self._pairing_loader = pairing_loader or remote_settings.load_client_pairing
        self._token_loader = token_loader or remote_settings.load_client_token
        self._max_idle = max(1, int(max_idle))
        self._idle_s = float(idle_s)
        self._lock = threading.Lock()
        self._idle: list[tuple[tuple[str, str, int, str], RemoteConnection, float]] = []

    def _credentials(self) -> tuple[remote_settings.ClientPairing, str]:
        pairing = self._pairing_loader()
        token = self._token_loader()
        if pairing is None or not token:
            raise RemoteEngineError("Pair with an OpenWhisper host first.")
        return pairing, token

    @staticmethod
    def _key(pairing: remote_settings.ClientPairing, token: str) -> tuple[str, str, int, str]:
        return (pairing.fingerprint.upper(), pairing.host, pairing.port, token)

    def acquire(
        self, *, connect_timeout: float | None = None,
    ) -> tuple[RemoteConnection, remote_settings.ClientPairing, bool]:
        """Check out a connected ``RemoteConnection``; returns ``(conn, pairing, reused)``."""
        pairing, token = self._credentials()
        wanted = self._key(pairing, token)
        now = time.monotonic()
        stale: list[RemoteConnection] = []
        chosen: RemoteConnection | None = None
        with self._lock:
            kept: list[tuple[tuple[str, str, int, str], RemoteConnection, float]] = []
            for key, conn, released_at in self._idle:
                if (
                    chosen is None
                    and key == wanted
                    and (now - released_at) < self._idle_s
                    and conn.alive
                ):
                    chosen = conn
                elif key == wanted and (now - released_at) < self._idle_s and conn.alive:
                    kept.append((key, conn, released_at))
                else:
                    stale.append(conn)
            self._idle = kept
        for conn in stale:
            conn.close()
        if chosen is not None:
            return chosen, pairing, True
        kwargs: dict[str, Any] = {"alternates": pairing.alternates}
        if connect_timeout is not None:
            kwargs["timeout"] = connect_timeout
        conn = RemoteConnection(
            pairing.host, pairing.port, token, pairing.fingerprint, **kwargs,
        )
        conn.connect()
        return conn, pairing, False

    def release(self, conn: RemoteConnection, pairing: remote_settings.ClientPairing) -> None:
        """Return a healthy connection to the idle pool, or close it."""
        if not conn.alive:
            conn.close()
            return
        current = self._pairing_loader()
        token = self._token_loader()
        if current is None or not token or current != pairing:
            conn.close()
            return
        key = self._key(current, token)
        evicted: RemoteConnection | None = None
        with self._lock:
            if len(self._idle) >= self._max_idle:
                _old_key, evicted, _old_ts = self._idle.pop(0)
            self._idle.append((key, conn, time.monotonic()))
        if evicted is not None:
            evicted.close()

    def close_all(self) -> None:
        """Close every idle connection in the pool."""
        with self._lock:
            idle, self._idle = self._idle, []
        for _key, conn, _ts in idle:
            conn.close()

    def request(
        self,
        op: str,
        *,
        json_body: dict[str, Any] | None = None,
        payload: bytes | None = None,
        header_fields: dict[str, Any] | None = None,
        timeout: float = 60.0,
        required_capability: str | None = None,
        cancel_event: threading.Event | None = None,
        on_cancel: Callable[[], None] | None = None,
        retry_on_unsent: bool = True,
    ) -> dict:
        """Send one AI request on a dedicated pooled connection.

        When ``json_body`` is supplied, it is encoded as UTF-8 JSON bytes in
        the binary payload so large prompts and transcripts never overflow
        ``MAX_HEADER_BYTES``.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise RemoteAICanceled("Canceled")
        if json_body is not None and payload is not None:
            raise ValueError("Pass either json_body or payload, not both.")
        wire_payload = (
            protocol.pack_json_payload(json_body) if json_body is not None else payload
        )
        fields = dict(header_fields or {})

        for attempt in range(2):
            conn, pairing, reused = self.acquire(connect_timeout=min(6.0, max(1.0, timeout)))
            keep = False
            stop_watch = threading.Event()
            canceled_midflight = threading.Event()

            def _watch_cancel(
                active_conn: RemoteConnection = conn,
                stop_ev: threading.Event = stop_watch,
                midflight_ev: threading.Event = canceled_midflight,
            ) -> None:
                assert cancel_event is not None
                while not stop_ev.wait(_CANCEL_POLL_S):
                    if cancel_event.is_set():
                        midflight_ev.set()
                        if on_cancel is not None:
                            try:
                                on_cancel()
                            except Exception:
                                logger.debug("Remote AI cancel hook raised", exc_info=True)
                        # Closing wakes recv immediately and notifies the host.
                        active_conn.close()
                        return

            try:
                if required_capability:
                    check_capability(conn.ready, required_capability, pairing.host_name)
                if cancel_event is not None:
                    if cancel_event.is_set():
                        raise RemoteAICanceled("Canceled")
                    threading.Thread(
                        target=_watch_cancel,
                        name=f"remote-ai-cancel-{op}",
                        daemon=True,
                    ).start()
                result = conn.request(op, timeout=timeout, payload=wire_payload, **fields)
                keep = True
                return result
            except RemoteConnectionLost as exc:
                if canceled_midflight.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise RemoteAICanceled("Canceled") from exc
                if reused and retry_on_unsent and not exc.sent and attempt == 0:
                    continue
                raise
            except RuntimeError as exc:
                if canceled_midflight.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise RemoteAICanceled("Canceled") from exc
                raise
            finally:
                stop_watch.set()
                if keep and not canceled_midflight.is_set():
                    self.release(conn, pairing)
                else:
                    conn.close()
        raise RemoteEngineError("Could not complete request to the paired host.")


_POOL_LOCK = threading.Lock()
_DEFAULT_POOL: RemoteAIConnectionPool | None = None


def ai_connection_pool() -> RemoteAIConnectionPool:
    """Process-wide dedicated connection pool for host-facilitated AI calls."""
    global _DEFAULT_POOL
    with _POOL_LOCK:
        if _DEFAULT_POOL is None:
            _DEFAULT_POOL = RemoteAIConnectionPool()
        return _DEFAULT_POOL


def reset_ai_connection_pool() -> None:
    """Close and reset the default AI connection pool (e.g. after pairing changes)."""
    global _DEFAULT_POOL
    with _POOL_LOCK:
        pool, _DEFAULT_POOL = _DEFAULT_POOL, None
    if pool is not None:
        pool.close_all()


# ---- Phase 1 high-level client calls ----


def remote_text_cleanup(
    text: str,
    *,
    system_prompt: str | None = None,
    reasoning: str | None = None,
    timeout_s: float | None = None,
    deadline_s: float | None = None,
    cancel_event: threading.Event | None = None,
    pool: RemoteAIConnectionPool | None = None,
) -> dict[str, Any]:
    """Run transcript cleanup / rewrite / transform / rule polish on the paired host."""
    active_pool = pool or ai_connection_pool()
    budget = (
        float(deadline_s)
        if deadline_s is not None
        else (float(timeout_s) if timeout_s is not None else DEFAULT_CLEANUP_TIMEOUT_S)
    )
    body: dict[str, Any] = {"text": text}
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    if reasoning is not None:
        body["reasoning"] = reasoning
    if timeout_s is not None:
        body["timeout_s"] = float(timeout_s)
    if deadline_s is not None:
        body["deadline_s"] = float(deadline_s)
    return active_pool.request(
        "text_cleanup",
        json_body=body,
        timeout=max(1.0, budget + 2.0),
        required_capability="text_cleanup",
        cancel_event=cancel_event,
    )


def remote_fast_judge(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    timeout_s: float = 30.0,
    pool: RemoteAIConnectionPool | None = None,
) -> dict[str, Any]:
    """Run TypeSafe / text-model fast judgments on the paired host."""
    active_pool = pool or ai_connection_pool()
    budget = max(0.5, float(timeout_s))
    body: dict[str, Any] = {
        "state": state,
        "questions": dict(questions),
        "timeout_s": budget,
    }
    return active_pool.request(
        "fast_judge",
        json_body=body,
        timeout=budget + 2.0,
        required_capability="fast_judgments",
    )


def _read_shot_info(shots: Sequence[Path]) -> list[tuple[str, Path, int, str]]:
    info: list[tuple[str, Path, int, str]] = []
    for shot in shots:
        path = Path(shot)
        data = path.read_bytes()
        info.append((path.name, path, len(data), hashlib.sha256(data).hexdigest()))
    return info


def _cancel_remote_writeup(pool: RemoteAIConnectionPool, job_id: str) -> None:
    try:
        pool.request(
            "walkthrough_writeup_cancel",
            header_fields={"job_id": job_id},
            timeout=5.0,
            retry_on_unsent=True,
        )
    except Exception:
        logger.debug("Could not cancel remote walkthrough write-up %s", job_id, exc_info=True)


def remote_walkthrough_writeup(
    markdown: str,
    shots: Sequence[Path],
    *,
    instructions: str = "",
    follow_notes: bool = False,
    notes: Sequence[Any] = (),
    duration_s: float = 0.0,
    cancel_event: threading.Event | None = None,
    poll_interval_s: float = DEFAULT_WRITEUP_POLL_S,
    timeout_s: float = DEFAULT_WRITEUP_TIMEOUT_S,
    pool: RemoteAIConnectionPool | None = None,
) -> tuple[str, dict[str, str], str]:
    """Run a walkthrough AI write-up on the paired host and return ``(markdown, look, label)``."""
    active_pool = pool or ai_connection_pool()
    if cancel_event is not None and cancel_event.is_set():
        raise RemoteAICanceled("canceled")

    serialized_notes = [
        {
            "text": str(getattr(note, "text", note["text"] if isinstance(note, dict) else "")),
            "section": int(getattr(note, "section", note["section"] if isinstance(note, dict) else 0)),
        }
        for note in notes
    ]
    shot_info = _read_shot_info(shots)
    total_shot_bytes = sum(size for _name, _path, size, _sha in shot_info)
    use_inline = total_shot_bytes <= _INLINE_SHOT_BYTES_LIMIT

    if use_inline:
        shot_entries = [
            {
                "name": name,
                "data_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
            for name, path, _size, _sha in shot_info
        ]
        started = active_pool.request(
            "walkthrough_writeup_start",
            json_body={
                "markdown": markdown,
                "instructions": instructions,
                "follow_notes": bool(follow_notes),
                "notes": serialized_notes,
                "duration_s": float(duration_s),
                "shots": shot_entries,
            },
            timeout=30.0,
            required_capability="walkthrough_writeup",
            cancel_event=cancel_event,
        )
    else:
        descriptors = [
            {"name": name, "size": size, "sha256": sha}
            for name, _path, size, sha in shot_info
        ]
        started = active_pool.request(
            "walkthrough_writeup_start",
            json_body={
                "markdown": markdown,
                "instructions": instructions,
                "follow_notes": bool(follow_notes),
                "notes": serialized_notes,
                "duration_s": float(duration_s),
                "shots": descriptors,
                "stage_only": True,
            },
            timeout=30.0,
            required_capability="walkthrough_writeup",
            cancel_event=cancel_event,
        )

    job_id = str(started.get("job_id") or "")
    if not job_id:
        raise RemoteEngineError("The paired host did not return a walkthrough write-up job id.")
    agent_label = str(started.get("agent_name") or "Paired host")

    completed = False
    try:
        if not use_inline:
            for name, path, size, _sha in shot_info:
                offset = 0
                with open(os.fspath(path), "rb") as handle:
                    while offset < size:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RemoteAICanceled("canceled")
                        chunk = handle.read(protocol.WALKTHROUGH_CHUNK_BYTES)
                        if not chunk:
                            break
                        active_pool.request(
                            "walkthrough_writeup_put",
                            payload=chunk,
                            header_fields={
                                "job_id": job_id,
                                "name": name,
                                "offset": offset,
                            },
                            timeout=30.0,
                            required_capability="walkthrough_writeup",
                            cancel_event=cancel_event,
                            retry_on_unsent=False,
                        )
                        offset += len(chunk)
            started = active_pool.request(
                "walkthrough_writeup_start",
                header_fields={"job_id": job_id},
                timeout=30.0,
                required_capability="walkthrough_writeup",
                cancel_event=cancel_event,
            )

        deadline = time.monotonic() + max(5.0, float(timeout_s))
        status_reply = started
        while True:
            state = str(status_reply.get("status") or "")
            if state == "done":
                completed = True
                raw_look = status_reply.get("look")
                look = (
                    {str(k): str(v) for k, v in raw_look.items() if isinstance(v, str)}
                    if isinstance(raw_look, dict)
                    else {}
                )
                return str(status_reply.get("markdown") or ""), look, agent_label
            if state == "canceled":
                completed = True
                raise RemoteAICanceled("canceled")
            if state == "failed":
                completed = True
                raise RemoteEngineError(
                    str(status_reply.get("error") or f"{agent_label} failed"),
                )
            if time.monotonic() >= deadline:
                raise RemoteEngineError("The paired host walkthrough write-up timed out.")
            wait_until = min(deadline, time.monotonic() + max(0.05, float(poll_interval_s)))
            while time.monotonic() < wait_until:
                if cancel_event is not None and cancel_event.is_set():
                    raise RemoteAICanceled("canceled")
                time.sleep(min(_CANCEL_POLL_S, max(0.0, wait_until - time.monotonic())))
            status_reply = active_pool.request(
                "walkthrough_writeup_status",
                header_fields={"job_id": job_id},
                timeout=15.0,
                cancel_event=cancel_event,
            )
    except BaseException:
        if not completed:
            _cancel_remote_writeup(active_pool, job_id)
        raise


def _send_agent_ask_cancel(pool: RemoteAIConnectionPool, ask_id: str) -> None:
    try:
        pool.request(
            "agent_ask_cancel",
            header_fields={"ask_id": ask_id},
            timeout=5.0,
            retry_on_unsent=True,
        )
    except Exception:
        logger.debug("Could not send remote agent_ask_cancel for %s", ask_id, exc_info=True)


def remote_agent_ask(
    text: str,
    *,
    agent: str = "",
    model: str = "",
    timeout_s: float = DEFAULT_ASK_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    pool: RemoteAIConnectionPool | None = None,
) -> dict[str, Any]:
    """Run an opt-in quick answer (``AgentMode.ASK``) on the paired host."""
    active_pool = pool or ai_connection_pool()
    if cancel_event is not None and cancel_event.is_set():
        return {"ok": False, "output": "", "message": "Canceled", "stopped": True}
    ask_id = uuid.uuid4().hex[:16]
    budget = max(1.0, float(timeout_s))
    try:
        return active_pool.request(
            "agent_ask",
            json_body={
                "text": text,
                "agent": agent,
                "model": model,
                "timeout_s": budget,
                "ask_id": ask_id,
            },
            timeout=budget + 5.0,
            required_capability="agent_ask",
            cancel_event=cancel_event,
            on_cancel=lambda: _send_agent_ask_cancel(active_pool, ask_id),
        )
    except RemoteAICanceled:
        return {"ok": False, "output": "", "message": "Stopped asking paired host.", "stopped": True}
