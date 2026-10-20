"""Portable paths shared by isolated CPU experiment entry points."""
from __future__ import annotations
import argparse
import os
import tempfile
from pathlib import Path

def add_paths(parser):
    root=Path(__file__).resolve().parents[3]
    parser.add_argument('--source-root',type=Path,default=root,help='OpenWhisper production checkout under test')
    parser.add_argument('--harness-root',type=Path,default=root,help='Checkout containing benchmarks.host_load.runner')
    parser.add_argument('--audio-file',type=Path,help='Required: 16 kHz mono PCM16 public test WAV')
    parser.add_argument('--whisper-model',type=Path,help='Local downloaded faster-whisper model directory')
    parser.add_argument('--native-model',type=Path,help='Verified pinned Parakeet GGUF')
    parser.add_argument('--native-runtime',type=Path,help='Runtime parent containing nemo-speech/lib')
    return parser

# Isolate app imports and inference subprocesses before any application code loads.
_TEMP_DATA=tempfile.TemporaryDirectory(prefix='ow-batching-experiment-')
os.environ['OPENWHISPER_DATA_DIR']=str(Path(_TEMP_DATA.name)/'data')

PATHS=add_paths(argparse.ArgumentParser(add_help=False)).parse_known_args()[0]

def required(value,label):
    if value is None:raise ValueError(f'{label} must be supplied explicitly; no models or runtimes are downloaded by this experiment')
    return value.resolve()
