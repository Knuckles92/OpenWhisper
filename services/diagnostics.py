"""Opt-in support reports built from allowlisted fields, never application logs.

The local failure journal records categories and code locations only. Messages,
exception text, tracebacks, settings dumps, paths and device identities are not
safe to export: any of them can contain dictated text or credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import platform
import sys
import threading

JOURNAL_NAME = "diagnostic-failures.json"
METRICS_NAME = "diagnostic-metrics.json"
MAX_FAILURES = 50
MAX_METRIC_SAMPLES = 100
METRIC_NAMES = frozenset({"startup_to_window_s", "stop_to_result_s", "cancel_s", "shutdown_s",
                          "captured_frames", "dropped_frames", "device_switches",
                          "input_overflows", "peak_rss_mb",
                          "context_capture_ms", "context_capture_timeouts",
                          "mouse_hook_callback_ms"})
_metrics = None
_COMPONENTS = (
    "recorder", "audio_processor", "application_controller", "database",
    "history_manager", "runtime.transcription", "components", "local_asr",
    "streaming_transcriber", "app_update", "remote_engine",
)
_EXCEPTIONS = (TimeoutError, PermissionError, FileNotFoundError, MemoryError,
               ConnectionError, OSError, ValueError, RuntimeError)


def _read_json(path: Path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _failure_events(value) -> list[dict]:
    """Revalidate persisted events; editing a journal cannot inject payloads."""
    result = []
    for event in value[-MAX_FAILURES:] if isinstance(value, list) else ():
        if not isinstance(event, dict):
            continue
        component = event.get("component")
        if component not in (*_COMPONENTS, "transcriber", "ui", "meeting", "application"):
            continue
        category = event.get("category")
        if category not in (*[cls.__name__ for cls in _EXCEPTIONS], "Error", "LoggedError"):
            continue
        timestamp = event.get("utc")
        try:
            timestamp = datetime.fromisoformat(timestamp).isoformat()
        except (ValueError, TypeError):
            continue
        line = event.get("line")
        result.append(dict(utc=timestamp, component=component, category=category,
                           line=line if type(line) is int and 0 < line < 100000 else None))
    return result


class FailureCapture(logging.Handler):
    def __init__(self, path: Path):
        super().__init__(logging.ERROR)
        self.path = path
        self.events = _failure_events(_read_json(path))
        self.metrics_capture = None

    def emit(self, record: logging.LogRecord) -> None:
        component = "application"
        for known in _COMPONENTS:
            if record.name == "services." + known or record.name.startswith("services." + known + "."):
                component = known
                break
        else:
            for prefix, name in (("transcriber.", "transcriber"), ("ui_qt.", "ui"), ("meeting.", "meeting")):
                if record.name.startswith(prefix):
                    component = name
        category = "LoggedError"
        if record.exc_info and record.exc_info[1] is not None:
            category = next((cls.__name__ for cls in _EXCEPTIONS
                             if isinstance(record.exc_info[1], cls)), "Error")
        event = dict(utc=datetime.now(timezone.utc).isoformat(), component=component,
                     category=category, line=record.lineno)
        self.events = [*self.events[-MAX_FAILURES + 1:], event]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.events, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            # Disk-full and permission failures must not recursively log here.
            pass

    def close(self) -> None:
        # main.py explicitly runs logging.shutdown before os._exit. Give the
        # latest shutdown metric a short chance to land, never wait on disk.
        if self.metrics_capture is not None:
            self.metrics_capture.close(timeout=.1)
        super().close()


def install_failure_capture() -> None:
    from config import config

    global _metrics
    folder = Path(config.LOG_FILE).parent
    root = logging.getLogger()
    handler = next((handler for handler in root.handlers if isinstance(handler, FailureCapture)), None)
    if handler is None:
        handler = FailureCapture(folder / JOURNAL_NAME)
        root.addHandler(handler)
    if handler.metrics_capture is None:
        handler.metrics_capture = MetricsCapture(folder / METRICS_NAME)
    _metrics = handler.metrics_capture


def _metric_samples(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: [sample for sample in samples[-MAX_METRIC_SAMPLES:]
                  if type(sample) in (int, float) and math.isfinite(sample) and 0 <= sample <= 1e12]
            for key, samples in value.items() if key in METRIC_NAMES and isinstance(samples, list)}


def _metric_summary(value) -> dict:
    result = {}
    for key, samples in _metric_samples(value).items():
        if not samples:
            continue
        ordered = sorted(samples)
        def percentile(fraction):
            position = (len(ordered) - 1) * fraction
            lower = math.floor(position)
            upper = math.ceil(position)
            return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        result[key] = dict(count=len(samples), p50=percentile(.5), p95=percentile(.95),
                           maximum=ordered[-1], latest=samples[-1])
    return result


class MetricsCapture:
    """Bounded numeric samples with one coalescing daemon writer.

    Callers only update a small in-memory snapshot and wake the writer. There
    is no queue of writes to grow when storage stalls and no filesystem call
    under the condition lock. Diagnostics may lose their newest samples on a
    forced exit; application work must never wait for metric persistence.
    """
    def __init__(self, path: Path):
        self.path = path
        self.samples = _metric_samples(_read_json(path))
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._generation = 0
        self._completed_generation = 0
        self._saved_generation = 0
        self._closing = False
        self._writer = threading.Thread(target=self._run, name="diagnostic-metrics", daemon=True)
        self._writer.start()

    def record(self, values):
        valid = _metric_samples({key: [value] for key, value in values.items()})
        valid = {key: samples for key, samples in valid.items() if samples}
        if not valid:
            return
        with self._condition:
            if self._closing:
                return
            for key, samples in valid.items():
                self.samples[key] = [*self.samples.get(key, []), *samples][-MAX_METRIC_SAMPLES:]
            self._generation += 1
        self._wake.set()

    def _write_snapshot(self, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot), encoding="utf-8")
        os.replace(temporary, self.path)

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._condition:
                if self._completed_generation >= self._generation:
                    if self._closing:
                        return
                    continue
                generation = self._generation
                snapshot = {key: list(samples) for key, samples in self.samples.items()}
            saved = False
            try:
                self._write_snapshot(snapshot)
                saved = True
            except Exception:
                # Best effort only; logging here would recurse during shutdown.
                pass
            with self._condition:
                self._completed_generation = generation
                if saved:
                    self._saved_generation = generation
                self._condition.notify_all()
                if self._closing and generation >= self._generation:
                    return

    def flush(self, timeout=.1) -> bool:
        """Wait at most timeout for samples already submitted; never retry IO."""
        with self._condition:
            target = self._generation
            self._condition.wait_for(lambda: self._completed_generation >= target, timeout=max(0, timeout))
            return self._saved_generation >= target

    def close(self, timeout=.1) -> bool:
        with self._condition:
            self._closing = True
        self._wake.set()
        return self.flush(timeout)


def record_metrics(**values) -> None:
    """Record approved numeric timings/counters after logging is initialized.

    Never call from an audio callback. Missing metrics stay missing, not zero.
    No-op when diagnostics were not installed (including isolated workers).
    """
    if _metrics is not None:
        _metrics.record(values)


def collect_report(*, root: Path | None = None) -> dict:
    from _version import __version__
    from config import SETTINGS_FILENAME, config, data_root
    from services.local_asr.catalog import MODELS

    root = Path(root) if root is not None else Path(data_root())
    settings = _read_json(root / SETTINGS_FILENAME)
    settings = settings if isinstance(settings, dict) else {}
    allowed = {
        "selected_model": {"local_whisper", "api", "remote", *[model.backend for model in MODELS.values()]},
        "whisper_model": set(config.WHISPER_MODEL_CHOICES),
        "whisper_device": {"auto", "cpu", "cuda"},
        "whisper_compute_type": {"auto", "default", "float16", "float32", "int8", "int8_float16", "int8_float32", "bfloat16"},
    }
    # Catalog identifiers are public constants; never export custom model paths.
    engine = {key: value if isinstance(value, str) and value in choices else "default_or_custom"
              for key, choices in allowed.items() for value in [settings.get(key)]}
    models = settings.get("local_asr_models", {})
    if isinstance(models, dict):
        engine["local_models"] = sorted({value for value in models.values()
                                         if isinstance(value, str) and value in MODELS})
    packages = {}
    for name in ("PyQt6", "numpy", "av", "faster-whisper", "ctranslate2", "openai", "sounddevice", "SQLAlchemy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    from services.gpu_info import nvidia_gpu
    gpu = nvidia_gpu()
    gpu_info = dict(name=gpu.name, total_mib=gpu.total_mib,
                    compute_capability=gpu.compute_capability) if gpu else None
    return dict(schema=1, app_version=__version__, packaged=bool(getattr(sys, "frozen", False)),
                generated_utc=datetime.now(timezone.utc).isoformat(),
                platform=dict(system=platform.system(), release=platform.release(),
                              machine=platform.machine(), cpu_count=os.cpu_count(),
                              python=platform.python_version(), nvidia_gpu=gpu_info),
                engine=engine, packages=packages,
                recent_failures=_failure_events(_read_json(root / JOURNAL_NAME)),
                recent_metrics=_metric_summary(_read_json(root / METRICS_NAME)),
                privacy="No transcript, audio, log messages, paths, credentials, hostnames or pairing data included.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export a local, sanitized OpenWhisper support report. Nothing is uploaded.")
    parser.add_argument("output", type=Path, help="New JSON file to create (existing files are not overwritten)")
    args = parser.parse_args(argv)
    report = collect_report()
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, indent=2)
        destination.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
