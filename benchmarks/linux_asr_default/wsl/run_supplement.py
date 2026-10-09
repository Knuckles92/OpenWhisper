"""Supplement the native Linux run with the same GPU profiles under WSL."""
import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ['XDG_DATA_HOME'] = str(ROOT/'xdg')

import run_comparison as common
from transcriber.local_backend import LocalWhisperBackend

original = common.make_backend
turbo = Path(os.environ['OPENWHISPER_BENCHMARK_TURBO_SNAPSHOT'])
if not (turbo/'model.bin').is_file():
    raise ValueError('OPENWHISPER_BENCHMARK_TURBO_SNAPSHOT must point to cached turbo weights')

def make_backend(profile):
    if profile == 'whisper_turbo_gpu':
        return LocalWhisperBackend(str(turbo), device='auto', compute_type='auto')
    return original(profile)

common.make_backend = make_backend
args = argparse.Namespace(repeats=3, profiles='whisper_turbo_gpu,parakeet_gpu',
                          smoke=False, output='wsl-comparison.json')
raise SystemExit(common.run(args))
