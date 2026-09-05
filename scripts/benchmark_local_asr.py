"""Benchmark installed local engines on one audio file; never downloads models.

Activate the project venv, then run:
    python scripts/benchmark_local_asr.py audio.wav --output results.json
Use --reference transcript.txt to measure normalized word error rate.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import re
import statistics
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def word_error_rate(reference, hypothesis):
    ref = re.findall(r"\w+", reference.lower())
    hyp = re.findall(r"\w+", hypothesis.lower())
    row = list(range(len(hyp) + 1))
    for i, expected in enumerate(ref, 1):
        next_row = [i]
        for j, actual in enumerate(hyp, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j-1] + (expected != actual)))
        row = next_row
    return row[-1] / max(1, len(ref))


def run(args):
    import numpy as np
    import psutil  # Optional developer dependency: pip install psutil
    import app_qt  # Register the same native library paths as normal startup.
    from faster_whisper.audio import decode_audio
    from services.local_asr.catalog import MODELS
    from services.local_asr import cache
    from transcriber.local_backend import LocalWhisperBackend
    from transcriber.optional_backend import LocalSpeechBackend

    os.environ["HF_HUB_OFFLINE"] = "1"
    audio = decode_audio(str(args.audio), sampling_rate=16000)
    duration = len(audio) / 16000
    reference = args.reference.read_text(encoding="utf-8-sig") if args.reference else None
    report = dict(platform=platform.platform(), python=platform.python_version(),
                  duration_s=duration, repeats=args.repeats, results=[])
    matrix = [("base", "cpu"), ("turbo", "cuda")]
    matrix += [(key, device) for key in MODELS for device in
               (("cpu",) if MODELS[key].backend == "moonshine" else ("cpu", "cuda"))]
    if args.models:
        matrix = [(key, dev) for key, dev in matrix if key in args.models.split(",")]

    with ExitStack() as context:
        if args.test_root:
            root = args.test_root.resolve()
            mapping = {"asr-nvidia-cpu": "nemo-cpu", "asr-nvidia-cuda": "nemo-cuda",
                       "asr-qwen": "qwen-runtime", "asr-moonshine": "moonshine-runtime"}
            context.enter_context(patch.object(cache, "local_app_dir", return_value=str(root/"test-app")))
            context.enter_context(patch("services.components.component_dir", side_effect=lambda key: str(root/mapping[key])))
            context.enter_context(patch("services.components.is_installed", side_effect=lambda key: (root/mapping[key]/"python.exe").exists()))
        for key, device in matrix:
            row = dict(model=key, requested_device=device)
            backend = None
            monitor_stop = threading.Event()
            peak = [0]
            def monitor():
                while not monitor_stop.wait(.05):
                    try:
                        proc = psutil.Process()
                        worker = getattr(backend, "_process", None)
                        children = [psutil.Process(worker.process.pid)] if worker else []
                        peak[0] = max(peak[0], sum(p.memory_info().rss for p in [proc, *children] if p.is_running()))
                    except (psutil.Error, OSError):
                        pass
            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()
            print("Benchmark", key, device, flush=True)
            try:
                started = time.perf_counter()
                if key in MODELS:
                    backend = LocalSpeechBackend(MODELS[key].backend, key, device)
                    backend.reload_model()
                else:
                    backend = LocalWhisperBackend(key, device=device, compute_type="int8" if device=="cpu" else "float16")
                row["load_s"] = time.perf_counter()-started
                if not backend.is_available():
                    raise RuntimeError(backend.device_info)
                row["device_info"] = backend.device_info
                row["actual_device"] = backend.device
                if backend.device != device:
                    raise RuntimeError(f"Requested {device}, but loaded {backend.device}: {backend.device_info}")
                timings = []
                texts = []
                for _ in range(args.repeats + 1):
                    started = time.perf_counter()
                    texts.append(backend.transcribe(str(args.audio)))
                    timings.append(time.perf_counter()-started)
                row.update(first_decode_s=timings[0], warm_s=timings[1:],
                           warm_median_s=statistics.median(timings[1:]),
                           warm_p95_s=float(np.percentile(timings[1:], 95)),
                           real_time_factor=statistics.median(timings[1:])/duration,
                           transcript=texts[-1])
                if reference is not None:
                    row["normalized_wer"] = word_error_rate(reference, texts[-1])
            except Exception as exc:
                row["error"] = str(exc)
            finally:
                if backend is not None:
                    backend.cleanup()
                monitor_stop.set()
                monitor_thread.join()
                row["peak_process_tree_rss_mb"] = peak[0]/1_000_000
            report["results"].append(row)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, default=Path("local-asr-results.json"))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--models", help="Comma-separated catalog keys; defaults to all")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--test-root", type=Path, help="Developer runtime/cache layout in a workspace directory")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    run(args)

