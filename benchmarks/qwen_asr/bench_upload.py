"""Qwen file transcription through the app's backend: one window per request vs batched.

Run with the app's Python from the repository root. Loads the real Qwen worker
on CUDA, transcribes a long file built from a corpus manifest both ways, and
checks a one-window file and a cancel during a batch. Settings and history go
to --work; installed runtimes and models are only read.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import wave

import numpy as np


def write_wav(path, samples):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes((np.clip(samples, -1, .999969) * 32768).astype(np.int16).tobytes())


def gpu_memory():
    """Whole-card (used, free) MiB, including other apps."""
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout.split(",")
    return int(out[0]), int(out[1])


class PeakMemory:
    def __enter__(self):
        self.peak, self._stop = 0, threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def _sample(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, gpu_memory()[0])
            self._stop.wait(.15)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--model", default="qwen-0.6b")
    parser.add_argument("--model-path", type=Path, help="Load --model from here instead of the model cache")
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ.update(OPENWHISPER_DATA_DIR=str(work / "data"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from faster_whisper.audio import decode_audio

    from benchmarks.meeting_mode.metrics import score_text
    from services.local_asr import cache
    from transcriber.optional_backend import LocalSpeechBackend

    corpus = json.loads(args.corpus_manifest.read_text(encoding="utf-8"))
    clips = [clip for clip in corpus["clips"] if clip["group"] in (
        "librispeech_clean", "librispeech_other", "ami_conversation", "concatenated_read_speech")]
    rng = np.random.default_rng(48)
    # Opens like the call in #49: 25 s of near-silence with one click.
    quiet = rng.normal(0, 10 ** (-67 / 20), 25 * 16000).astype(np.float32)
    quiet[16000] = 10 ** (-43 / 20)
    parts = [quiet]
    for index, clip in enumerate(clips):
        parts += [decode_audio(str(args.corpus_manifest.parent / clip["audio_path"])), np.zeros(8000, np.float32)]
        if index == len(clips) // 2:
            parts.append(np.zeros(40 * 16000, np.float32))  # a long pause mid-file
    long_file, one_window = work / "long.wav", work / "short.wav"
    write_wav(long_file, np.concatenate(parts))
    write_wav(one_window, parts[1])
    reference = " ".join(clip["reference"] for clip in clips)
    duration = sum(map(len, parts)) / 16000
    report = dict(model=args.model, duration_s=duration, clips=len(clips), runs=[], checks=[])
    output = work / f"{args.model}-results.json"

    def save(row, key):
        report[key].append(row)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k != "text"}), flush=True)

    is_cached, load_path = cache.is_cached, cache.load_path
    with patch.object(LocalSpeechBackend, "_settings", staticmethod(lambda: {"local_asr_language": "en"})), \
            patch.object(cache, "is_cached", lambda key: bool(args.model_path) and key == args.model or is_cached(key)), \
            patch.object(cache, "load_path", lambda key: str(args.model_path) if args.model_path and key == args.model
                         else load_path(key)):
        report["gpu_before_load_mib"] = gpu_memory()
        backend = LocalSpeechBackend("qwen_asr", args.model, "cuda")
        backend.reload_model()
        if backend.device != "cuda":
            raise SystemExit(backend.last_error or f"Loaded on {backend.device}")
        report["gpu_after_load_mib"] = gpu_memory()
        try:
            # Remote engines decode one window per request; this forces that path.
            backend.is_remote = True
            serial_short = backend.transcribe(str(one_window))
            backend.is_remote = False
            batched_short = backend.transcribe(str(one_window))
            save(dict(check="one-window file", identical=serial_short == batched_short), "checks")
            serial_text = None
            for run in range(args.runs):
                for mode in ("serial", "batched") if run % 2 == 0 else ("batched", "serial"):
                    requests, worker_request = [], backend._process.request

                    def counted(op, **payload):
                        requests.append(len(payload.get("audio_paths", [None])))
                        return worker_request(op, **payload)

                    backend.is_remote = mode == "serial"
                    with patch.object(backend._process, "request", side_effect=counted), PeakMemory() as peak:
                        started = time.perf_counter()
                        text = backend.transcribe(str(long_file))
                        elapsed = time.perf_counter() - started
                    time.sleep(.5)
                    serial_text = serial_text or (text if mode == "serial" else None)
                    save(dict(run=run + 1, mode=mode, elapsed_s=round(elapsed, 2), speed=round(duration / elapsed, 1),
                              windows_per_request=requests, peak_gpu_used_mib=peak.peak,
                              gpu_used_after_mib=gpu_memory()[0], vs_reference=score_text(reference, text),
                              vs_serial=score_text(serial_text, text) if serial_text else None, text=text), "runs")
            backend.is_remote = False

            # A cancel while a batch decodes ends in an error, never a transcript.
            entered, worker = threading.Event(), backend._process
            worker_request = worker.request

            def entering(op, **payload):
                entered.set()
                return worker_request(op, **payload)

            with patch.object(worker, "request", side_effect=entering), concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(backend.transcribe, str(long_file))
                entered.wait(60)
                time.sleep(.3)
                backend.cancel_transcription()
                try:
                    outcome = f"returned {len(future.result(timeout=60))} characters"
                except RuntimeError as error:
                    outcome = str(error)
            backend.reload_model()
            save(dict(check="cancel during a batch", outcome=outcome, worker_stopped=worker.process.poll() is not None,
                      next_transcription_identical=backend.transcribe(str(one_window)) == batched_short), "checks")
        finally:
            backend.cleanup()


if __name__ == "__main__":
    main()
