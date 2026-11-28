"""Linux audio activity adapter using PipeWire ``pw-dump --monitor``.

Maintains a lightweight in-memory graph of PipeWire ``Stream/Input/Audio`` and
``Stream/Output/Audio`` nodes and ``PipeWire:Interface:Client`` records.
Streams capturing a sink monitor (``stream.capture.sink == true``), such as
OpenWhisper's own Meeting Mode system-audio capture, are excluded from
microphone users. Only stream metadata is read; no audio is captured.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from meeting.call_detection.adapters import AudioActivityAdapter
from meeting.call_detection.catalog import deduplicate_processes, is_process_of
from meeting.call_detection.models import ActiveAudioProcess

logger = logging.getLogger(__name__)

INPUT_STREAM_CLASS: Final[str] = "Stream/Input/Audio"
OUTPUT_STREAM_CLASS: Final[str] = "Stream/Output/Audio"
_RESTART_BACKOFF_S: Final[float] = 2.0


@dataclass(frozen=True)
class _PwProcess:
    pid: Optional[int] = None
    binary: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def from_props(cls, props: dict[str, Any]) -> "_PwProcess":
        raw_pid = props.get("application.process.id")
        pid: Optional[int] = None
        if isinstance(raw_pid, int):
            pid = raw_pid
        elif isinstance(raw_pid, str) and raw_pid.strip().lstrip("-").isdigit():
            pid = int(raw_pid.strip())

        binary = props.get("application.process.binary")
        name = props.get("application.name")
        return cls(
            pid=pid,
            binary=str(binary) if isinstance(binary, str) and binary else None,
            name=str(name) if isinstance(name, str) and name else None,
        )

    def merged_with(self, fallback: "_PwProcess") -> "_PwProcess":
        return _PwProcess(
            pid=self.pid if self.pid is not None else fallback.pid,
            binary=self.binary or fallback.binary,
            name=self.name or fallback.name,
        )


@dataclass(frozen=True)
class _PwNode:
    is_input: bool = False
    is_output: bool = False
    running: bool = False
    client_id: Optional[int] = None
    process: _PwProcess = field(default_factory=_PwProcess)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _is_truthy_prop(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


class PipeWireGraph:
    """Pure, testable in-memory projection of ``pw-dump --monitor`` objects."""

    def __init__(self) -> None:
        self._nodes: dict[int, _PwNode] = {}
        self._clients: dict[int, _PwProcess] = {}
        self._ready: bool = False

    @property
    def ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        self._nodes.clear()
        self._clients.clear()
        self._ready = False

    def apply_payload(self, payload: Any) -> None:
        """Apply one decoded JSON value from ``pw-dump --monitor`` (list or object)."""
        if isinstance(payload, list):
            self._ready = True
            for item in payload:
                if isinstance(item, dict):
                    self.apply_object(item)
        elif isinstance(payload, dict):
            self.apply_object(payload)

    def apply_object(self, obj: dict[str, Any]) -> None:
        obj_id = _as_int(obj.get("id"))
        if obj_id is None:
            return
        self._ready = True
        info = obj.get("info")
        if info is None or not isinstance(info, dict):
            self._nodes.pop(obj_id, None)
            self._clients.pop(obj_id, None)
            return

        props = info.get("props")
        if not isinstance(props, dict):
            props = {}

        obj_type = obj.get("type")
        if obj_type == "PipeWire:Interface:Node":
            media_class = props.get("media.class")
            is_monitor = _is_truthy_prop(props.get("stream.capture.sink"))
            state = info.get("state")
            self._nodes[obj_id] = _PwNode(
                is_input=(media_class == INPUT_STREAM_CLASS and not is_monitor),
                is_output=(media_class == OUTPUT_STREAM_CLASS),
                running=(state == "running"),
                client_id=_as_int(props.get("client.id")),
                process=_PwProcess.from_props(props),
            )
        elif obj_type == "PipeWire:Interface:Client":
            self._clients[obj_id] = _PwProcess.from_props(props)

    def _resolved_process(self, node: _PwNode) -> _PwProcess:
        if node.client_id is not None and node.client_id in self._clients:
            return node.process.merged_with(self._clients[node.client_id])
        return node.process

    def microphone_processes(self, own_pid: int) -> list[ActiveAudioProcess]:
        """Return deduplicated active microphone processes, excluding ``own_pid``."""
        found: list[ActiveAudioProcess] = []
        for node in self._nodes.values():
            if not (node.is_input and node.running):
                continue
            proc = self._resolved_process(node)
            if proc.pid is None or proc.pid == own_pid or not proc.binary:
                continue
            found.append(
                ActiveAudioProcess(
                    pid=proc.pid,
                    bundle_id=proc.binary,
                    name=proc.name or proc.binary,
                )
            )
        return deduplicate_processes(found)

    def is_app_playing(self, canonical_id: str, own_pid: int) -> bool:
        """Return True if any running output node belongs to ``canonical_id``."""
        for node in self._nodes.values():
            if not (node.is_output and node.running):
                continue
            proc = self._resolved_process(node)
            if proc.pid == own_pid:
                continue
            if is_process_of(proc.binary, canonical_id, proc.name):
                return True
        return False


class IncrementalJsonParser:
    """Incrementally parses concatenated JSON documents from a text stream."""

    def __init__(self) -> None:
        self._buffer: str = ""
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: str) -> list[Any]:
        if not chunk:
            return []
        self._buffer += chunk
        results: list[Any] = []
        idx = 0
        length = len(self._buffer)
        while idx < length:
            while idx < length and self._buffer[idx].isspace():
                idx += 1
            if idx >= length:
                break
            try:
                value, end = self._decoder.raw_decode(self._buffer, idx)
            except json.JSONDecodeError:
                break
            results.append(value)
            idx = end
        if idx > 0:
            self._buffer = self._buffer[idx:]
        return results

    def reset(self) -> None:
        self._buffer = ""


class LinuxPipeWireAdapter(AudioActivityAdapter):
    """Watches PipeWire via a long-running ``pw-dump --monitor`` subprocess."""

    def __init__(self, *, pw_dump_path: Optional[str] = None) -> None:
        self._pw_dump = pw_dump_path or shutil.which("pw-dump")
        self._own_pid = os.getpid()
        self._lock = threading.Lock()
        self._graph = PipeWireGraph()
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def _ensure_started(self) -> None:
        if self._started or not self._pw_dump or self._stop_event.is_set():
            return
        with self._lock:
            if self._started or self._stop_event.is_set():
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="pipewire-call-monitor",
                daemon=True,
            )
            self._thread.start()

    def _monitor_loop(self) -> None:
        parser = IncrementalJsonParser()
        while not self._stop_event.is_set():
            proc: Optional[subprocess.Popen[str]] = None
            try:
                proc = subprocess.Popen(
                    [self._pw_dump, "--monitor"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                with self._lock:
                    self._proc = proc
                parser.reset()
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self._stop_event.is_set():
                        break
                    for payload in parser.feed(line):
                        with self._lock:
                            self._graph.apply_payload(payload)
            except OSError as exc:
                logger.debug("pw-dump --monitor unavailable: %s", exc)
            except Exception:
                logger.debug("pw-dump --monitor reader error", exc_info=True)
            finally:
                with self._lock:
                    self._proc = None
                    self._graph.reset()
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            if not self._stop_event.is_set():
                self._stop_event.wait(_RESTART_BACKOFF_S)

    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        if not self._pw_dump:
            return []
        self._ensure_started()
        with self._lock:
            if not self._graph.ready:
                return []
            return self._graph.microphone_processes(self._own_pid)

    def is_app_playing_audio(self, canonical_id: str) -> bool:
        if not self._pw_dump:
            return False
        self._ensure_started()
        with self._lock:
            if not self._graph.ready:
                return False
            return self._graph.is_app_playing(canonical_id, self._own_pid)

    def close(self) -> None:
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
