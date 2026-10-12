"""Render in AudioForge's own Python/native environment (currently Python 3.13)."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--profile", choices=("audioforge_rnnoise", "audioforge_full"), required=True
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve() / "python"))
    core = importlib.import_module("mic_eq.mic_eq_core")
    audio = np.load(args.input, allow_pickle=False)
    full = args.profile == "audioforge_full"
    # No reference speech masks or invented VAD probabilities enter the engine.
    # Disable model-backed VAD explicitly; compare the threshold gate independently.
    settings = {
        "full_chain": True,
        "return_output_audio": True,
        "processing_mode": "normal",
        "vad_available": False,
        "gate_mode": 0,
        "gate_enabled": full,
        "gate_threshold_db": -45.0,
        "gate_attack_ms": 5.0,
        "gate_release_ms": 100.0,
        "gate_auto_threshold_enabled": False,
        "suppressor_enabled": True,
        "noise_model": "rnnoise",
        "suppressor_strength": 1.0,
        "eq_enabled": full,
        "deesser_enabled": False,
        "compressor_enabled": full,
        "compressor_threshold_db": -24.0,
        "compressor_ratio": 2.0,
        "compressor_makeup_gain_db": 3.0,
        "compressor_auto_makeup_enabled": full,
        "limiter_enabled": True,
        "limiter_ceiling_db": -1.0,
    }
    bands = [
        (frequency, 2.0 if full and frequency == 3200 else 0.0, 0.707)
        for frequency in (32, 64, 125, 250, 500, 1000, 2000, 3200, 8000, 16000)
    ]
    started = time.perf_counter()
    diagnostics = core.simulate_auto_eq_chain(
        np.ascontiguousarray(audio, dtype=np.float32), 48000.0, bands, settings
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    output = np.asarray(diagnostics.pop("output_audio"), dtype=np.float32)
    # Upstream's simulator already aligns both suppression and downstream delay.
    if (
        output.shape != audio.shape
        or not np.isfinite(output).all()
        or diagnostics["non_finite_output"]
    ):
        raise ValueError("AudioForge returned invalid audio")
    np.save(args.output, output, allow_pickle=False)
    extension = Path(core.__file__).resolve()
    import hashlib

    metadata = {
        "settings": settings,
        "bands": bands,
        "simulation_ms": elapsed_ms,
        "algorithmic_latency_ms": diagnostics["chain_latency_samples"] / 48,
        "frame_ms": None,
        "extension": str(extension),
        "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
        "python": sys.version,
        "diagnostics": diagnostics,
    }
    args.metadata.write_text(
        json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
