"""The speech worker's transcribe_batch request, through its JSON-lines protocol."""
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

BOOTSTRAP = """
import runpy, sys, types

class OutOfMemoryError(RuntimeError):
    pass

class Qwen:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        # Each transcribe call must be exactly one batch of what it's given.
        assert kwargs['max_inference_batch_size'] == -1
        engine = cls()
        engine.model = types.SimpleNamespace(generate=None, modules=lambda: [])
        return engine

    def transcribe(self, audio, language):
        if isinstance(audio, tuple):
            return [types.SimpleNamespace(text=f'{language} {int(audio[0][0])}')]
        values = [int(samples[0]) for samples, _rate in audio]
        if len(values) > 2:
            raise OutOfMemoryError('CUDA out of memory')
        if 7 in values:
            raise RuntimeError('Qwen reached its output limit.')
        return [types.SimpleNamespace(text=f'{language} {value}') for value in values]

def empty_cache():
    print('empty_cache', file=sys.stderr)

sys.modules['torch'] = types.SimpleNamespace(float16='f16', float32='f32', cuda=types.SimpleNamespace(
    is_available=lambda: True, get_device_capability=lambda index: (7, 5), get_arch_list=lambda: ['sm_75'],
    mem_get_info=lambda: (8 * 1024**3, 8 * 1024**3), memory_reserved=lambda: 0, memory_allocated=lambda: 0,
    empty_cache=empty_cache, OutOfMemoryError=OutOfMemoryError))
sys.modules['qwen_asr'] = types.SimpleNamespace(Qwen3ASRModel=Qwen)
runpy.run_path('services/local_asr/worker.py', run_name='__main__')
"""


def run_worker(requests):
    result = subprocess.run(
        [sys.executable, "-u", "-c", BOOTSTRAP], cwd=Path(__file__).resolve().parents[1],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()], result.stderr


def audio_files(tmp_path, values):
    paths = [str(tmp_path / f"window-{value}.f32") for value in values]
    for path, value in zip(paths, values):
        np.full(1600, value, np.float32).tofile(path)
    return paths


def test_worker_recovers_failed_batches_and_still_decodes_single_windows(tmp_path):
    rows, errors = run_worker([
        dict(id=1, op="load", backend="qwen_asr", device="cuda", model_path="model"),
        # Out of memory at four windows: the rest of the file goes two at a time.
        dict(id=2, op="transcribe_batch", audio_paths=audio_files(tmp_path, [1, 2, 3, 4]),
             language="fr", batch_size=16),
        # One window at its output limit fails the batch, not the others.
        dict(id=3, op="transcribe_batch", audio_paths=audio_files(tmp_path, [7, 5]),
             language="en", batch_size=16),
        dict(id=4, op="transcribe", audio_path=audio_files(tmp_path, [6])[0], language="en"),
        dict(id=5, op="shutdown"),
    ])
    assert rows[1] == dict(id=2, result=dict(texts=["French 1", "French 2", "French 3", "French 4"], batch_size=2))
    assert rows[2] == dict(id=3, result=dict(texts=["English 7", "English 5"], batch_size=16))
    assert rows[3]["result"]["text"] == "English 6"
    assert errors.count("empty_cache") == 2


def test_worker_refuses_batches_off_cuda(tmp_path):
    rows, _errors = run_worker([
        dict(id=1, op="load", backend="qwen_asr", device="cpu", model_path="model"),
        dict(id=2, op="transcribe_batch", audio_paths=audio_files(tmp_path, [1, 2]),
             language="en", batch_size=16),
        dict(id=3, op="shutdown"),
    ])
    assert rows[1]["error"] == "Only Qwen on CUDA decodes windows in batches"
