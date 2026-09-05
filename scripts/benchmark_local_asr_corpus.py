"""Compare installed engines against a JSON manifest of audio_path/reference/group.

No downloads are performed. Activate the venv before running this script.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_local_asr import word_error_rate


def run(args):
    import app_qt
    from faster_whisper.audio import decode_audio
    from services.local_asr.catalog import MODELS
    from services.local_asr import cache
    from transcriber.local_backend import LocalWhisperBackend
    from transcriber.optional_backend import LocalSpeechBackend

    os.environ['HF_HUB_OFFLINE'] = '1'
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    clips = []
    for clip in manifest['clips']:
        path = (args.manifest.parent / clip['audio_path']).resolve()
        clips.append(dict(clip, path=str(path), duration_s=len(decode_audio(str(path), sampling_rate=16000))/16000))
    matrix = [('base', 'cpu'), ('turbo', 'cuda')]
    matrix += [(key, 'cpu' if model.backend == 'moonshine' else 'cuda') for key, model in MODELS.items()]
    if args.cpu_only:
        matrix = [(key, 'cpu') for key, _ in matrix]
    if args.models:
        matrix = [(key, device) for key, device in matrix if key in args.models.split(',')]
    report = dict(source=manifest.get('source'), normalization='lowercase, Unicode word tokens; punctuation ignored; spelling, abbreviations, and number formatting count', results=[])
    with ExitStack() as context:
        if args.test_root:
            root = args.test_root.resolve()
            context.enter_context(patch.object(cache, 'local_app_dir', return_value=str(root/'test-app')))
            context.enter_context(patch('services.components.component_dir', side_effect=lambda key: str(root/'install-validated'/key)))
            context.enter_context(patch('services.components.is_installed', side_effect=lambda key: (root/'install-validated'/key/'python.exe').exists()))
        for key, device in matrix:
            row = dict(model=key, requested_device=device, clips=[], groups={})
            backend = None
            print('Corpus', key, device, flush=True)
            try:
                start = time.perf_counter()
                if key in MODELS:
                    backend = LocalSpeechBackend(MODELS[key].backend, key, device)
                    backend.reload_model()
                else:
                    backend = LocalWhisperBackend(key, device=device, compute_type='int8' if device == 'cpu' else 'float16')
                row['load_s'] = time.perf_counter()-start
                row['actual_device'] = backend.device
                if not backend.is_available() or backend.device != device:
                    raise RuntimeError(backend.device_info)
                backend.transcribe(clips[0]['path'])
                for clip in clips:
                    start = time.perf_counter()
                    transcript = backend.transcribe(clip['path'])
                    elapsed = time.perf_counter()-start
                    words = len(re.findall(r'\w+', clip['reference'].lower()))
                    errors = round(word_error_rate(clip['reference'], transcript)*words)
                    group = row['groups'].setdefault(clip.get('group', 'all'), dict(words=0, errors=0, decode_s=0., audio_s=0.))
                    for field, value in dict(words=words, errors=errors, decode_s=elapsed, audio_s=clip['duration_s']).items():
                        group[field] += value
                    row['clips'].append(dict(id=clip.get('id', clip['audio_path']), group=clip.get('group', 'all'), transcript=transcript, words=words, errors=errors, decode_s=elapsed))
                for group in row['groups'].values():
                    group['normalized_wer'] = group['errors']/max(1, group['words'])
                    group['real_time_factor'] = group['decode_s']/group['audio_s']
            except Exception as exc:
                row['error'] = str(exc)
            finally:
                if backend:
                    backend.cleanup()
            report['results'].append(row)
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps({k:v for k,v in row.items() if k != 'clips'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, default=Path('local-asr-corpus-results.json'))
    parser.add_argument('--models')
    parser.add_argument('--cpu-only', action='store_true')
    parser.add_argument('--test-root', type=Path)
    run(parser.parse_args())
