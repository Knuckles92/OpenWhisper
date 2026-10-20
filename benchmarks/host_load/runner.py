"""Load the real host/client over pinned TLS; isolate settings and discovery.

Service-time sleeps model inference only. Every request traverses the production
PCM encoder, TLS WebSocket, authentication, admission and dispatch paths.
"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import ExitStack
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
for _proxy_key in ("NO_PROXY", "no_proxy"):
    _hosts = [host for host in os.environ.get(_proxy_key, "").split(",") if host]
    os.environ[_proxy_key] = ",".join(dict.fromkeys([*_hosts, "localhost", "127.0.0.1", "::1"]))

import numpy as np
import psutil

from benchmarks.host_load.profiles import PROFILES


class Measurements:
    def __init__(self, output=None):
        self.rows = []
        self.resources = []
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.file = open(output, "w", encoding="utf-8") if output else None

    def add(self, **row):
        row.setdefault("at_s", time.perf_counter() - self.started)
        with self.lock:
            self.rows.append(row)
            if self.file:
                self.file.write(json.dumps(row, allow_nan=False) + "\n")
                self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


class TimedEngine:
    """Configurable inference simulator with real wall-clock serialization."""
    accepts_phrases = False

    def __init__(self, profile, *, serialized=True, jitter=.1, seed=7):
        self.profile = profile
        self.serialized = serialized
        self.jitter = jitter
        self.lock = threading.Lock()
        self.random = random.Random(seed)
        self.random_lock = threading.Lock()
        self.generation = 0
        self.local = threading.local()

    @property
    def identity(self):
        return ("benchmark", self.profile.family, self.profile.model, self.generation)

    def describe(self):
        return dict(family=self.profile.family, model=self.profile.model,
                    label="Simulated " + self.profile.model, device="simulated",
                    streaming=True, available=True, status="benchmark")

    def transcribe(self, audio, language, **_kwargs):
        timing = self._decode(len(audio) / 16000)
        return {"text": "benchmark speech", "segments": [{"text": "benchmark speech",
                "start": 0., "end": len(audio) / 16000}], "_benchmark": timing}

    def stream(self, session, audio, language, finish):
        timing = self._decode(len(audio) / 16000)
        return {"events": [{"text": "benchmark partial", "final": finish}],
                "_benchmark": timing}

    def cancel_stream(self, session):
        pass

    def _decode(self, seconds):
        started = time.perf_counter()
        if self.serialized:
            self.lock.acquire()
        acquired = time.perf_counter()
        try:
            with self.random_lock:
                multiplier = 1. + self.random.uniform(-self.jitter, self.jitter)
            intended = (self.profile.intercept_s + self.profile.seconds_per_audio_second * seconds) * multiplier
            time.sleep(max(0., intended))
        finally:
            finished = time.perf_counter()
            if self.serialized:
                self.lock.release()
        return {"engine_lock_wait_s": acquired - started,
                "decode_s": finished - acquired, "intended_decode_s": intended}


class BenchmarkHost:
    """Standalone fixture; never opens discovery or uses persistent credentials."""
    def __init__(self, args):
        self.args = args
        self.stack = ExitStack()
        self.connections = []
        self.connection_lock = threading.Lock()
        self.local = threading.local()
        self.credentials = []
        self._stopped = False

    def __enter__(self):
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="ow-host-load-")))
        self.stack.enter_context(patch.dict(os.environ, {"OPENWHISPER_DATA_DIR": str(self.root / "data")}))
        # Imports occur after data isolation and source-root selection.
        from services.remote_asr import discovery, settings, tailscale
        from services.remote_asr.host import DeviceRegistry, SpeechHost
        from services.remote_asr.tls import ensure_host_identity
        from services.settings import settings_manager
        actual_source = Path(sys.modules["services.remote_asr.host"].__file__).resolve().parents[2]
        if self.args.source_root and actual_source != self.args.source_root.resolve():
            raise RuntimeError(f"Production imports came from {actual_source}, not requested source root")

        self.stack.enter_context(patch.object(tailscale, "status", lambda timeout=4.: tailscale.TailscaleStatus("not_installed")))
        self.stack.enter_context(patch.object(tailscale, "whois", lambda address, timeout=4.: None))
        self.stack.enter_context(patch.object(discovery, "find_host", lambda *a, **kw: None))
        self.stack.enter_context(patch.object(settings, "remember_host_addresses", lambda *a, **kw: None))
        self.stack.enter_context(patch.object(settings, "load_client_pairing", lambda *a, **kw: getattr(self.local, "pairing", None)))
        self.stack.enter_context(patch.object(settings, "load_client_token", lambda: getattr(self.local, "token", None)))
        self.stack.enter_context(patch.object(settings_manager, "load_all_settings", lambda: {}))
        self.audio = self._load_audio()
        self.engine = self._make_engine()
        entries = []
        def save(values):
            entries[:] = [dict(value) for value in values]
        registry = DeviceRegistry(lambda: list(entries), save)
        for index in range(max(self.args.clients, 32) + 4):
            device, token = registry.add(f"benchmark-{index}")
            self.credentials.append((device, token))
        kwargs = {}
        if self.args.max_connections is not None:
            kwargs["max_connections"] = self.args.max_connections
        self.host = SpeechHost(engine_provider=lambda: self.engine, registry=registry,
            identity=ensure_host_identity(str(self.root / "tls")), host_name="benchmark-loopback",
            addresses=lambda: [], models=lambda: [{"family": self.engine.describe()["family"],
            "model": self.engine.describe()["model"], "label": "Benchmark"}],
            select_model=self._switch, records_enabled=lambda: True,
            records=self._record, **kwargs)
        self.host.start(port=0, bind="127.0.0.1")
        self.port = self.host.port
        self._instrument()
        return self

    def _load_audio(self):
        if self.args.audio_file is None:
            if self.args.engine == "real":
                raise ValueError("Real inference requires --audio-file with speech (16 kHz mono PCM WAV)")
            return None
        import wave
        with wave.open(str(self.args.audio_file), "rb") as stream:
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
                raise ValueError("--audio-file must be 16 kHz mono 16-bit PCM WAV")
            audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768.
        if len(audio) < 1600 or float(np.max(np.abs(audio))) < .001:
            raise ValueError("--audio-file must contain non-silent speech, not empty/silent PCM")
        return audio

    def audio_for(self, seconds):
        if self.audio is None:
            return tone(seconds)
        samples = round(seconds * 16000)
        return np.resize(self.audio, samples).astype(np.float32)

    def _make_engine(self):
        if self.args.engine == "fake":
            return TimedEngine(PROFILES[self.args.profile], serialized=not self.args.parallel_decode,
                               jitter=self.args.jitter, seed=self.args.seed)
        from services.remote_asr.engines import SpeechWorkerEngine, WhisperEngine
        if self.args.family == "whisper":
            from services.isolated import IsolatedWhisperModel
            model = IsolatedWhisperModel(self.args.model, device=self.args.device,
                compute_type=self.args.compute_type, cpu_threads=self.args.cpu_threads,
                download_root=self.args.model_cache, local_files_only=True)
            model.load()
            self.stack.callback(model.close)
            model_id = self.args.model_id or (Path(self.args.model).name if Path(self.args.model).is_dir() else self.args.model)
            backend = SimpleNamespace(model=model, last_loaded_model=model_id,
                model_name=model_id, device=self.args.device, compute_type=self.args.compute_type,
                is_available=lambda: True, device_info="Real isolated faster-whisper worker")
            return WhisperEngine(backend)
        if not self.args.app_data:
            raise ValueError("Real Parakeet requires --app-data pointing at existing speech-models and components; nothing is downloaded")
        from services.local_asr import cache
        from services import components
        root = self.args.app_data.resolve()
        self.stack.enter_context(patch.object(cache, "local_app_dir", lambda: str(root)))
        self.stack.enter_context(patch.object(components, "component_dir", lambda key: str(root / "components" / key)))
        from transcriber.optional_backend import LocalSpeechBackend
        backend = LocalSpeechBackend("parakeet", model_name=self.args.model, device=self.args.device)
        backend.reload_model()
        if not backend.is_available():
            raise RuntimeError(backend.last_error or "Parakeet runtime/model is not installed")
        self.stack.callback(backend.cleanup)
        return SpeechWorkerEngine(backend)

    def _instrument(self):
        admission = self.host._work_admission
        original = admission.acquire
        dispatch = self.host._dispatch_unpacked
        dispatch_frame = self.host._dispatch
        timing = threading.local()
        def acquire(*a, **kw):
            started = time.perf_counter()
            result = original(*a, **kw)
            timing.queue_s = time.perf_counter() - started
            return result
        def unpacked(*a, **kw):
            result = dispatch(*a, **kw)
            if isinstance(result.get("result"), dict):
                metrics = result["result"].setdefault("_benchmark", {})
                metrics["admission_wait_s"] = getattr(timing, "queue_s", 0.)
            return result
        def frame(*a, **kw):
            result = dispatch_frame(*a, **kw)
            if isinstance(result.get("result"), dict):
                metrics = result["result"].setdefault("_benchmark", {})
                metrics.update({"wire_" + key: value for key, value in result.items()
                    if key.endswith("_ms") and isinstance(value, (int, float))})
            return result
        # Measurement-only wrappers preserve the implementation's admissions.
        admission.acquire = acquire
        self.host._dispatch_unpacked = unpacked
        self.host._dispatch = frame

    def _switch(self, family, model, device_name):
        if not isinstance(self.engine, TimedEngine):
            raise RuntimeError("Real-engine model changes are intentionally not automated")
        self.engine.generation += 1
        self.host.engine_changed()
        return self.engine.describe()

    def _record(self, op, header, payload, device):
        # Controlled disk sink, not the product's complete database/manifest path.
        path = self.root / f"upload-{device['id']}.bin"
        with path.open("ab") as stream:
            stream.write(payload)
        return {"bytes": len(payload), "benchmark_sink": True}

    def new_client(self, index, history=False):
        from services.remote_asr.client import RemoteConnection
        device, token = self.credentials[index]
        connection = RemoteConnection("127.0.0.1", self.host.port, token,
            self.host.identity.fingerprint, history_enabled=True if history else None)
        connection.connect()
        with self.connection_lock:
            self.connections.append(connection)
        return connection

    def new_meeting(self, index):
        from meeting.asr.remote import MeetingRemoteBackend
        from services.remote_asr.settings import ClientPairing
        device, token = self.credentials[index]
        self.local.pairing = ClientPairing("127.0.0.1", self.host.port,
            fingerprint=self.host.identity.fingerprint, host_name="benchmark", device_id=device["id"])
        self.local.token = token
        backend = MeetingRemoteBackend({"fingerprint": self.host.identity.fingerprint,
                                        "host_name": "benchmark"})
        backend.reload_model()
        if not backend.is_available():
            raise RuntimeError(backend.last_error)
        return backend

    def __exit__(self, *exc):
        self._stopped = True
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            connection.close()
        self.host.stop()
        return self.stack.__exit__(*exc)


def resource_snapshot(pid):
    process = psutil.Process(pid)
    processes = [process, *process.children(recursive=True)]
    rss = threads = 0
    cpu = 0.
    for child in processes:
        try:
            rss += child.memory_info().rss
            threads += child.num_threads()
            times = child.cpu_times()
            cpu += times.user + times.system
        except psutil.NoSuchProcess:
            pass
    return dict(rss_bytes=rss, threads=threads, processes=len(processes), cpu_time_s=cpu)


def _host_process(args, pipe):
    if args.source_root:
        sys.path.insert(0, str(args.source_root.resolve()))
    records = Measurements()
    stop = threading.Event()
    try:
        with BenchmarkHost(args) as fixture:
            pipe.send(dict(port=fixture.host.port, fingerprint=fixture.host.identity.fingerprint,
                           credentials=fixture.credentials, pid=os.getpid(),
                           engine_description=fixture.engine.describe(),
                           resource_start=resource_snapshot(os.getpid())))
            local_thread = None
            while True:
                command = pipe.recv()
                if command[0] == "start_local":
                    local_thread = threading.Thread(target=_local_worker,
                        args=(fixture, records, args, command[1], stop), daemon=True)
                    local_thread.start()
                elif command[0] == "history_query":
                    try:
                        result = fixture.host.history.query(command[1], "list", {"limit": 10})
                        pipe.send({"result": result})
                    except Exception as exc:
                        pipe.send({"error": type(exc).__name__})
                elif command[0] == "stop":
                    stop.set()
                    if local_thread:
                        local_thread.join(45.)
                    time.sleep(args.quiescence_s)
                    quiescent = dict(resource_snapshot(os.getpid()),
                                     registered_speech_clients=len(fixture.host._clients))
                    break
        time.sleep(.2)
        pipe.send({"rows": records.rows, "resource_quiescent": quiescent, "resource_end": resource_snapshot(os.getpid())})
    except Exception as exc:
        pipe.send({"error": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        stop.set()
        pipe.close()


class RemoteFixture:
    """Production host in its own process so resource samples exclude clients."""
    new_client = BenchmarkHost.new_client
    new_meeting = BenchmarkHost.new_meeting
    _load_audio = BenchmarkHost._load_audio
    audio_for = BenchmarkHost.audio_for

    def __init__(self, args):
        self.args = args
        self.stack = ExitStack()
        self.connections = []
        self.connection_lock = threading.Lock()
        self.local = threading.local()
        self.local_rows = []
        self.pipe_lock = threading.Lock()

    def __enter__(self):
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="ow-load-clients-")))
        self.stack.enter_context(patch.dict(os.environ, {"OPENWHISPER_DATA_DIR": str(self.root / "data")}))
        from services.remote_asr import discovery, settings, tailscale
        from services.settings import settings_manager
        self.stack.enter_context(patch.object(tailscale, "status", lambda timeout=4.: tailscale.TailscaleStatus("not_installed")))
        self.stack.enter_context(patch.object(tailscale, "whois", lambda address, timeout=4.: None))
        self.stack.enter_context(patch.object(discovery, "find_host", lambda *a, **kw: None))
        self.stack.enter_context(patch.object(settings, "remember_host_addresses", lambda *a, **kw: None))
        self.stack.enter_context(patch.object(settings, "load_client_pairing", lambda *a, **kw: getattr(self.local, "pairing", None)))
        self.stack.enter_context(patch.object(settings, "load_client_token", lambda: getattr(self.local, "token", None)))
        self.stack.enter_context(patch.object(settings_manager, "load_all_settings", lambda: {}))
        self.audio = self._load_audio()
        context = multiprocessing.get_context("spawn")
        self.pipe, child_pipe = context.Pipe()
        self.process = context.Process(target=_host_process, args=(self.args, child_pipe), daemon=True)
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        try:
            self.process.start()
        finally:
            sys.path.pop(0)
        if not self.pipe.poll(300.):
            self.process.terminate()
            raise RuntimeError("Host process did not initialize within 300 seconds")
        ready = self.pipe.recv()
        if "error" in ready:
            raise RuntimeError(f"Host initialization failed: {ready}")
        self.pid = ready["pid"]
        self.credentials = ready["credentials"]
        self.resource_start = ready["resource_start"]
        self.engine_description = ready["engine_description"]
        self.resource_end = None
        self.resource_quiescent = None
        self.host = SimpleNamespace(port=ready["port"], identity=SimpleNamespace(fingerprint=ready["fingerprint"]))
        return self

    def start_local(self, start):
        self.pipe.send(("start_local", start))

    def history_query(self, device_id):
        with self.pipe_lock:
            self.pipe.send(("history_query", device_id))
            reply = self.pipe.recv()
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def __exit__(self, *exc):
        for connection in list(self.connections):
            connection.close()
        with self.pipe_lock:
            self.pipe.send(("stop",))
            if self.pipe.poll(55. + self.args.quiescence_s):
                final = self.pipe.recv()
                self.local_rows = final.get("rows", [])
                self.resource_end = final.get("resource_end")
                self.resource_quiescent = final.get("resource_quiescent")
        self.process.join(10.)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5.)
        self.pipe.close()
        return self.stack.__exit__(*exc)


def tone(seconds):
    return (.1 * np.sin(np.arange(round(seconds * 16000), dtype=np.float32) / 8)).astype(np.float32)


def percentile(values, fraction):
    return float(np.percentile(values, fraction)) if values else None


def _pause_until(deadline, stop):
    return stop.wait(max(0., deadline - time.perf_counter()))


def request(fixture, connection, records, client, op, seconds, scheduled, *, role="remote", measurement_kind="final", utterance_start=None, **fields):
    started = time.perf_counter()
    row = dict(client=client, role=role, op=op, kind=measurement_kind, audio_s=seconds,
               schedule_lag_s=max(0., started - scheduled))
    try:
        audio = fixture.audio_for(seconds) if seconds else None
        if role == "local":
            result = fixture.engine.transcribe(audio, "en")
            host_s = time.perf_counter() - started
        elif role == "meeting":
            result = connection._request_audio(op, audio, language="en", **fields)
            host_s = None
        else:
            result, host_s = connection.request_timed(op, audio=audio, language="en", timeout=45, **fields)
        row.update(status="preview_skipped" if result.get("preview_skipped") else "ok",
                   host_s=host_s, **result.get("_benchmark", {}))
        row["has_text"] = bool(result.get("text") or any(e.get("text") for e in result.get("events", [])))
        if fixture.args.engine == "real":
            row["text"] = result.get("text", "")
    except Exception as exc:
        code = getattr(exc, "code", None)
        row.update(status="busy" if code == "busy" else "error", error=type(exc).__name__,
                   message=str(exc), retry_after_s=getattr(exc, "retry_after_s", None))
    finished = time.perf_counter()
    row.update(service_s=finished - started, latency_s=finished - scheduled)
    records.add(**row)
    return row


def _history_loop(connection, stop, chatty):
    while not stop.is_set():
        try:
            frame = json.loads(connection.receive_history(timeout=.2))
            if frame.get("type") == "history_query":
                connection.send_history(json.dumps({"type": "history_result", "id": frame["id"],
                    "result": {"records": [{"text": "benchmark" * 100}] if chatty else []}}))
        except TimeoutError:
            continue
        except Exception:
            return


def make_schedule(args, index):
    """Precompute independent release times so overload never reduces offered load."""
    rng = random.Random(args.seed + index * 7919)
    workload = args.workload
    role = "meeting" if workload == "meeting" or workload == "mixed" and index % 4 == 0 else "remote"
    preview = workload == "preview" or workload == "mixed" and index % 4 == 1
    due = rng.uniform(0., min(2., args.duration / 4)) if preview or role == "meeting" else rng.expovariate(args.rate)
    events = []
    record_payload = b"x" * args.records_bytes if workload == "records" and index == 0 else None
    while due < args.duration:
        if role == "meeting":
            events.append(dict(at=due, op="transcribe", seconds=args.meeting_chunk,
                               role=role, kind="meeting", fields={}))
            due += args.meeting_chunk
        elif preview:
            native = args.preview == "native"
            cadence = .7663 if native else 3.0186
            began = due
            for sequence in range(4 if not native else 16):
                due += cadence
                if due >= args.duration:
                    break
                fields = dict(session=f"preview-{index}-{began}", finish=False) if native else {"preview": True}
                events.append(dict(at=due, op="stream" if native else "transcribe",
                    seconds=cadence if native else cadence + (.75 if sequence else 0.),
                    role=role, kind="preview", utterance_start=began, fields=fields))
            if due < args.duration:
                if native:
                    events.append(dict(at=due, op="stream", seconds=0., role=role,
                        kind="preview_finish", fields=dict(session=f"preview-{index}-{began}", finish=True)))
                events.append(dict(at=due, op="transcribe", seconds=due - began,
                                   role=role, kind="final", fields={}))
            due += rng.expovariate(args.rate)
        elif workload == "records" and index == 0:
            events.append(dict(at=due, op="records_put", seconds=0., role=role, kind="record",
                fields=dict(payload=record_payload, kind="dictation", record_id="benchmark",
                            name="audio.wav", offset=0)))
            due += .2
        else:
            seconds = min(30., max(2., rng.lognormvariate(math.log(6.), .65)))
            events.append(dict(at=due, op="transcribe", seconds=seconds, role=role,
                               kind="final", fields={}))
            due += rng.expovariate(args.rate)
    return events


def client_worker(fixture, records, args, index, start, stop):
    schedule = make_schedule(args, index)
    role = schedule[0]["role"] if schedule else "remote"
    connection = None
    history = None
    began = time.perf_counter()
    try:
        connection = fixture.new_meeting(index) if role == "meeting" else fixture.new_client(index)
        records.add(client=index, op="connect", status="ok", latency_s=time.perf_counter() - began)
        if args.workload in ("history", "mixed"):
            try:
                history = fixture.new_client(index, history=True)
                records.add(client=index, op="history_connect", status="ok")
                threading.Thread(target=_history_loop, args=(history, stop, args.chatty_history), daemon=True).start()
            except Exception as exc:
                records.add(client=index, op="history_connect", status="refused", error=type(exc).__name__, message=str(exc))
    except Exception as exc:
        records.add(client=index, op="connect", status="refused", latency_s=time.perf_counter() - began,
                    error=type(exc).__name__, message=str(exc))
        for event in schedule:
            records.add(client=index, role=role, op=event["op"], kind=event["kind"],
                        audio_s=event["seconds"], status="not_connected")
        return
    seen_preview = set()
    next_cycle = start + args.connection_cycle_s if args.connection_cycle_s else math.inf
    try:
        for event in schedule:
            due = start + event["at"]
            if time.perf_counter() > start + args.duration + args.arrival_grace_s or stop.is_set():
                records.add(client=index, role=role, op=event["op"], kind=event["kind"],
                            audio_s=event["seconds"], status="not_started")
                continue
            if _pause_until(due, stop):
                break
            if time.perf_counter() >= next_cycle and event["kind"] in ("final", "meeting"):
                # Lifecycle soak only: close between requests, never discard
                # a native stream chunk or pretend this is an abrupt drop.
                if history:
                    history.close()
                connection.cleanup() if role == "meeting" else connection.close()
                cycle_op = "cycle_connect"
                try:
                    connection = fixture.new_meeting(index) if role == "meeting" else fixture.new_client(index)
                    records.add(client=index, op="cycle_connect", status="ok")
                    if args.workload in ("history", "mixed"):
                        cycle_op = "cycle_history_connect"
                        history = fixture.new_client(index, history=True)
                        threading.Thread(target=_history_loop, args=(history, stop, args.chatty_history), daemon=True).start()
                        records.add(client=index, op="cycle_history_connect", status="ok")
                except Exception as exc:
                    records.add(client=index, op=cycle_op, status="refused",
                                error=type(exc).__name__, message=str(exc))
                next_cycle = time.perf_counter() + args.connection_cycle_s
            row = request(fixture, connection, records, index, event["op"], event["seconds"],
                          due, role=role, measurement_kind=event["kind"],
                          utterance_start=start + event.get("utterance_start", event["at"]),
                          **event["fields"])
            if event["kind"] == "preview" and row.get("has_text"):
                key = event["utterance_start"]
                if key not in seen_preview:
                    records.add(client=index, role=role, op="first_preview", status="ok",
                                latency_s=time.perf_counter() - start - key)
                    seen_preview.add(key)
            if row.get("status") == "error" and args.reconnect:
                try:
                    connection.cleanup() if role == "meeting" else connection.close()
                    connection = fixture.new_meeting(index) if role == "meeting" else fixture.new_client(index)
                    records.add(client=index, op="reconnect", status="ok")
                except Exception as exc:
                    records.add(client=index, op="reconnect", status="refused", error=type(exc).__name__)
    finally:
        _pause_until(start + args.duration, stop)
        if history:
            history.close()
        if connection:
            connection.cleanup() if role == "meeting" else connection.close()


def _local_worker(fixture, records, args, start, stop):
    rng = random.Random(args.seed + 991)
    due = start
    schedule = []
    while due < start + args.duration:
        schedule.append(due)
        due += rng.expovariate(args.rate)
    for due in schedule:
        if stop.is_set() or time.perf_counter() > start + args.duration + args.arrival_grace_s:
            records.add(client="host", role="local", op="transcribe", kind="final",
                        audio_s=6., status="not_started")
            continue
        if _pause_until(due, stop):
            records.add(client="host", role="local", op="transcribe", kind="final",
                        audio_s=6., status="not_started")
            continue
        request(fixture, None, records, "host", "transcribe", 6., due, role="local")


def _history_queries(fixture, records, args, start, stop):
    due = start + 1.
    while due < start + args.duration and not stop.is_set():
        if _pause_until(due, stop):
            return
        for device, _ in fixture.credentials[:args.clients]:
            began = time.perf_counter()
            try:
                fixture.history_query(device["id"]) if isinstance(fixture, RemoteFixture) else fixture.host.history.query(device["id"], "list", {"limit": 10})
                records.add(client=device["name"], op="history_query", status="ok",
                            latency_s=time.perf_counter() - began)
            except Exception as exc:
                records.add(client=device["name"], op="history_query", status="unavailable",
                            error=type(exc).__name__)
        due += 1.


def _resource_worker(records, stop, pid=None):
    process = psutil.Process(pid)
    previous_cpu = None
    previous_at = None
    while not stop.wait(1.):
        try:
            snapshot = resource_snapshot(process.pid)
            cpu = snapshot.pop("cpu_time_s")
            at = time.perf_counter()
            percent = 100. * (cpu - previous_cpu) / (at - previous_at) if previous_at else 0.
            records.resources.append(dict(at_s=at - records.started, cpu_percent=max(0., percent), **snapshot))
            previous_cpu, previous_at = cpu, at
        except psutil.NoSuchProcess:
            return


def summary(records, duration):
    rows = records.rows
    # Request attempts (including censored arrivals) have a workload kind.
    # Lifecycle events must never enter audio/latency aggregates.
    requests = [r for r in rows if "kind" in r and "audio_s" in r]
    ok = [r for r in requests if r["status"] == "ok"]
    finals = [r for r in ok if r["op"] == "transcribe" and r.get("role") != "local" and r.get("kind") == "final"]
    small = [r for r in finals if r["audio_s"] < 15]
    metrics = dict(requests=len(requests), successes=len(ok), busy=sum(r["status"] == "busy" for r in requests),
        errors=sum(r["status"] == "error" for r in requests),
        preview_skipped=sum(r["status"] == "preview_skipped" for r in requests),
        not_started=sum(r["status"] == "not_started" for r in requests),
        not_connected=sum(r["status"] == "not_connected" for r in requests),
        full_featured_connected=len({r["client"] for r in rows if r["op"] == "connect" and r["status"] == "ok"} & {r["client"] for r in rows if r["op"] == "history_connect" and r["status"] == "ok"}),
        connected=sum(r["op"] == "connect" and r["status"] == "ok" for r in rows),
        connection_refusals=sum(r["op"] in ("connect", "reconnect", "cycle_connect") and r["status"] == "refused" for r in rows),
        history_refusals=sum(r["op"] in ("history_connect", "cycle_history_connect") and r["status"] == "refused" for r in rows),
        lifecycle_reconnects=sum(r["op"] in ("reconnect", "cycle_connect") and r["status"] == "ok" for r in rows),
        audio_seconds_per_wall_second=sum(r["audio_s"] for r in ok) / duration,
        final_samples=len(finals), small_final_samples=len(small),
        final_offered=sum(r.get("kind") == "final" and r.get("role") != "local" for r in requests),
        final_failures=sum(r.get("kind") == "final" and r.get("role") != "local" and r["status"] != "ok" for r in requests),
        local_offered=sum(r.get("role") == "local" for r in requests),
        local_unserved=sum(r.get("role") == "local" and r["status"] != "ok" for r in requests),
        preview_busy=sum(r.get("kind") == "preview" and r["status"] == "busy" for r in requests))
    groups = {"latency": [r["latency_s"] for r in ok],
        "preview_staleness": [r["latency_s"] for r in ok if r.get("kind") == "preview"],
        "first_preview": [r["latency_s"] for r in rows if r["op"] == "first_preview"],
        "final_latency": [r["latency_s"] for r in finals],
        "small_final_latency": [r["latency_s"] for r in small],
        "queue_wait": [r["admission_wait_s"] for r in ok if "admission_wait_s" in r],
        "engine_lock_wait": [r["engine_lock_wait_s"] for r in ok if "engine_lock_wait_s" in r],
        "decode": [r["decode_s"] for r in ok if "decode_s" in r],
        "local_latency": [r["latency_s"] for r in ok if r.get("role") == "local"],
        "host_time": [r["host_s"] for r in ok if r.get("host_s") is not None],
        "legacy_host_time": [r["wire_host_ms"] / 1000. for r in ok if "wire_host_ms" in r],
        "server_total_time": [r["wire_server_total_ms"] / 1000. for r in ok if "wire_server_total_ms" in r],
        "schedule_lag": [r["schedule_lag_s"] for r in requests if "schedule_lag_s" in r]}
    for name, values in groups.items():
        for p in (50, 95, 99):
            metrics[f"{name}_p{p}_s"] = percentile(values, p)
    for name in ("rss_bytes", "threads", "cpu_percent"):
        metrics[name + "_max"] = max((r[name] for r in records.resources), default=None)
    metrics["acceptance_observed"] = bool(small and metrics["small_final_latency_p95_s"] < 1.
        and not metrics["final_failures"] and not metrics["connection_refusals"] and not metrics["history_refusals"])
    metrics["acceptance_applicable"] = bool(metrics["final_offered"])
    if not metrics["acceptance_applicable"]:
        metrics["acceptance_observed"] = None
    metrics["zero_final_failures_95pct_upper_rate"] = (1. - .05 ** (1. / metrics["final_offered"])
        if metrics["final_offered"] and not metrics["final_failures"] else None)
    metrics["zero_failure_bound_caveat"] = "Nominal binomial bound assumes independent/exchangeable Bernoulli attempts; correlated fixed-seed queue traces do not establish fleet reliability."
    metrics["acceptance_caveat"] = "Observed in this finite synthetic run, not certified hardware capacity; inspect sample count and offered arrival rate."
    return metrics


def default_args(**overrides):
    args = parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=Path, help="Immutable checkout whose production services are tested")
    p.add_argument("--output", type=Path, default=Path(".tmp/host-load/result.json"))
    p.add_argument("--engine", choices=("fake", "real"), default="fake")
    p.add_argument("--profile", choices=PROFILES, default="gpu-parakeet")
    p.add_argument("--family", choices=("parakeet", "whisper"), default="whisper")
    p.add_argument("--model", default="tiny.en")
    p.add_argument("--model-id", help="Semantic Whisper model ID when --model is a local folder; used by meeting route validation")
    p.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    p.add_argument("--compute-type", default="int8")
    p.add_argument("--cpu-threads", type=int, default=4)
    p.add_argument("--model-cache", default=None)
    p.add_argument("--audio-file", type=Path, help="Speech WAV, PCM16 mono 16kHz; required for real engines")
    p.add_argument("--app-data", type=Path, help="Existing model/runtime root, read-only; Parakeet uses speech-models and components")
    p.add_argument("--clients", type=int, default=4)
    p.add_argument("--duration", type=float, default=60.)
    p.add_argument("--rate", type=float, default=.05, help="Poisson utterance releases per second per client; .2 is stress, not human-realistic")
    p.add_argument("--workload", choices=("dictation", "preview", "meeting", "history", "mixed", "records", "local"), default="dictation")
    p.add_argument("--preview", choices=("window", "native"), default="window")
    p.add_argument("--meeting-chunk", type=float, default=5., help="Production spool target5s; vary up to20s hard cut")
    p.add_argument("--records-bytes", type=int, default=1024 * 1024)
    p.add_argument("--chatty-history", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--jitter", type=float, default=.1)
    p.add_argument("--parallel-decode", action="store_true", help="Sensitivity experiment, not current production locking")
    p.add_argument("--max-connections", type=int, help="Sensitivity override; omit to test production default")
    p.add_argument("--reconnect", action="store_true")
    p.add_argument("--connection-cycle-s", type=float, default=0.,
                   help="Lifecycle soak: gracefully reconnect between requests at this interval; 0 disables")
    p.add_argument("--quiescence-s", type=float, default=.3,
                   help="Settle after clients close before live-host resource snapshot; use 5 for soaks")
    p.add_argument("--arrival-grace-s", type=float, default=0.,
                   help="Issue already-planned arrivals for this many seconds after generation ends")
    return p


def main():
    args = parser().parse_args()
    if args.source_root:
        if not (args.source_root / "services/remote_asr/host.py").is_file():
            raise SystemExit("source-root must contain services/remote_asr/host.py")
        sys.path.insert(0, str(args.source_root.resolve()))
    if args.clients < 1 or any(not math.isfinite(v) or v <= 0 for v in (args.duration, args.rate, args.meeting_chunk)):
        raise SystemExit("clients, duration, rate and meeting-chunk must be positive and finite")
    if not 0 <= args.jitter <= 1 or args.cpu_threads < 1 or args.records_bytes < 1:
        raise SystemExit("jitter must be0..1; cpu-threads and records-bytes must be positive")
    if args.max_connections is not None and args.max_connections < 1:
        raise SystemExit("max-connections must be positive")
    if not math.isfinite(args.connection_cycle_s) or args.connection_cycle_s < 0:
        raise SystemExit("connection-cycle-s must be nonnegative and finite")
    if any(not math.isfinite(v) or v < 0 for v in (args.quiescence_s, args.arrival_grace_s)):
        raise SystemExit("quiescence-s and arrival-grace-s must be nonnegative and finite")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = Measurements(args.output.with_suffix(".ndjson"))
    stop = threading.Event()
    began = time.perf_counter()
    try:
        with RemoteFixture(args) as fixture:
            start = time.perf_counter() + 2.
            threads = [threading.Thread(target=client_worker,
                args=(fixture, records, args, i, start, stop), daemon=True) for i in range(args.clients)]
            resource = threading.Thread(target=_resource_worker, args=(records, stop, fixture.pid), daemon=True)
            resource.start()
            if args.workload in ("local", "mixed"):
                fixture.start_local(start)
            if args.chatty_history and args.workload in ("history", "mixed"):
                threads.append(threading.Thread(target=_history_queries,
                    args=(fixture, records, args, start, stop), daemon=True))
            for thread in threads:
                thread.start()
            # Drain the bounded client queues; distinguish generated duration from wall time.
            deadline = start + args.duration + args.arrival_grace_s + 50.
            for thread in threads:
                thread.join(timeout=max(0., deadline - time.perf_counter()))
            _pause_until(start + args.duration, stop)
            elapsed = time.perf_counter() - start
            stop.set()
            resource.join(3.)
            active_threads = sum(thread.is_alive() for thread in threads)
        for row in fixture.local_rows:
            records.add(**row)
        import services.remote_asr.host as host_module
        source = Path(host_module.__file__).resolve().parents[2]
        try:
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            dirty = bool(subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip())
        except Exception:
            commit, dirty = "unknown", None
        result = dict(schema_version=3, connection_lifetime="held through generation window",
            resources_scope="host process tree (sum RSS/threads/CPU including inference workers); client generator excluded", config={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
            source=dict(root=str(source), commit=commit, dirty=dirty, sha256={
                path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in (
                "services/remote_asr/host.py", "services/remote_asr/client.py",
                "services/remote_asr/engines.py", "transcriber/remote_backend.py")}),
            environment=dict(python=sys.version, platform=platform.platform(), logical_cpus=os.cpu_count()),
            engine_profile=PROFILES[args.profile].metadata() if args.engine == "fake" else {"real": True},
            engine_description=fixture.engine_description,
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            timing_semantics="host_s follows client request_timed behavior; raw wire_host_ms and optional wire_server_total_ms are recorded separately",
            input_audio_sha256=hashlib.sha256(args.audio_file.read_bytes()).hexdigest() if args.audio_file else None,
            elapsed_s=elapsed, setup_and_teardown_s=time.perf_counter() - began - elapsed,
            unfinished_threads=active_threads, summary=summary(records, elapsed),
            rows=records.rows, resources=records.resources,
            resource_start=fixture.resource_start, resource_quiescent=fixture.resource_quiescent,
            resource_end=fixture.resource_end)
        if args.engine == "fake":
            profile = PROFILES[args.profile]
            demand = sum(profile.intercept_s + profile.seconds_per_audio_second * r.get("audio_s", 0.)
                for r in records.rows if r.get("op") in ("transcribe", "stream"))
            result["summary"]["model_offered_utilization"] = demand / args.duration
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), **result["summary"]}, sort_keys=True), flush=True)
        if active_threads:
            raise SystemExit(2)
    finally:
        stop.set()
        records.close()


if __name__ == "__main__":
    main()
