"""Benchmark bounded Qwen batches using the app's isolated runtime Python.

The input manifest contains pre-cut 16 kHz float32 windows with ``audio_path``
and ``id``. This deliberately changes no app settings or installed packages.
Run from the Qwen runtime to avoid mixing Transformers/tokenizer versions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1 or any(size < 1 for size in args.batch_sizes):
        parser.error("runs and batch sizes must be positive")

    import numpy as np
    import torch
    from qwen_asr import Qwen3ASRModel

    root = Path(__file__).resolve().parents[2]
    sys.path.append(str(root))
    from services.local_asr.worker import check_qwen_gpu

    cuda = args.device == "cuda"
    if cuda:
        check_qwen_gpu(torch)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    windows = manifest["windows"]
    audios = [(np.fromfile(row["audio_path"], dtype=np.float32), 16000) for row in windows]
    report = dict(
        torch=torch.__version__, model_path=args.model_path, device=args.device,
        gpu=torch.cuda.get_device_name(0) if cuda else None,
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        windows=windows, duration_s=sum(len(audio) / rate for audio, rate in audios),
        max_new_tokens=2048, runs=[],
    )
    started = time.perf_counter()
    engine = Qwen3ASRModel.from_pretrained(
        args.model_path, device_map=args.device,
        dtype=torch.float16 if cuda else torch.float32,
        max_inference_batch_size=1, max_new_tokens=2048,
    )
    report["load_s"] = time.perf_counter() - started
    generate = engine.model.generate

    def checked_generate(*a, **kw):
        output = generate(*a, **kw)
        if output.sequences.shape[1] - kw["input_ids"].shape[1] >= kw["max_new_tokens"]:
            raise RuntimeError("Qwen reached its output limit")
        return output

    engine.model.generate = checked_generate
    started = time.perf_counter()
    engine.transcribe(audio=audios[0], language="English")
    report["warmup_s"] = time.perf_counter() - started
    for run in range(args.runs):
        sizes = args.batch_sizes if run % 2 == 0 else list(reversed(args.batch_sizes))
        for size in sizes:
            engine.max_inference_batch_size = size
            if cuda:
                torch.cuda.empty_cache()
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
            started = time.perf_counter()
            texts, calls = [], []
            row = dict(run=run + 1, batch_size=size)
            try:
                for offset in range(0, len(audios), size):
                    call_started = time.perf_counter()
                    results = engine.transcribe(audio=audios[offset:offset + size], language="English")
                    texts.extend(result.text for result in results)
                    if cuda:
                        torch.cuda.synchronize()
                    calls.append(time.perf_counter() - call_started)
                row.update(texts=texts, calls_s=calls)
            except Exception as error:
                row["error"] = str(error)
            row["elapsed_s"] = time.perf_counter() - started
            if cuda:
                row.update(peak_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                           peak_reserved_mib=torch.cuda.max_memory_reserved() / 2**20)
            report["runs"].append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({key: value for key, value in row.items() if key not in ("texts", "calls_s")}), flush=True)


if __name__ == "__main__":
    main()
