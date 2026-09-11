"""Compare installed engines against a JSON manifest of audio_path/reference/group.

No downloads are performed. Activate the venv before running this script.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.meeting_mode.metrics import score_text
from benchmarks.provenance import identity, model_identity


def run(args):
    import app_qt  # noqa: F401 -- registers production native library paths
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
    if not clips or any(clip['duration_s'] <= 0 for clip in clips):
        raise ValueError('Corpus must contain nonempty audio clips')
    matrix = [('base', 'cpu'), ('turbo', 'cuda')]
    matrix += [(key, 'cpu' if model.backend == 'moonshine' else 'cuda') for key, model in MODELS.items()]
    if args.cpu_only:
        matrix = [(key, 'cpu') for key, _ in matrix]
    if args.models:
        matrix = [(key, device) for key, device in matrix if key in args.models.split(',')]
    if not matrix or (args.models and set(args.models.split(',')) - {key for key, _ in matrix}):
        raise ValueError('No matching models or unknown model selection')
    report = dict(source=manifest.get('source'), normalization='NFKC lowercase ordered words; punctuation and AMI acronym separators ignored',
                  provenance=identity(inputs=[args.manifest, *(clip['path'] for clip in clips)]), results=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
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
                row['model_identity'] = model_identity(backend)
                backend.transcribe(clips[0]['path'])
                for clip in clips:
                    start = time.perf_counter()
                    transcript = backend.transcribe(clip['path'])
                    elapsed = time.perf_counter()-start
                    score = score_text(clip['reference'], transcript)
                    counts = {k: score[k] for k in ('words', 'errors', 'substitutions', 'deletions', 'insertions')}
                    group = row['groups'].setdefault(clip.get('group', 'all'),
                        dict.fromkeys((*counts, 'decode_s', 'audio_s'), 0))
                    for field, value in dict(**counts, decode_s=elapsed, audio_s=clip['duration_s']).items():
                        group[field] += value
                    row['clips'].append(dict(id=clip.get('id', clip['audio_path']),
                        group=clip.get('group', 'all'), transcript=transcript,
                        score=score, **counts, decode_s=elapsed))
                row['aggregate'] = {
                    key: sum(group[key] for group in row['groups'].values())
                    for key in ('words', 'errors', 'substitutions', 'deletions', 'insertions', 'decode_s', 'audio_s')
                }
                for group in [*row['groups'].values(), row['aggregate']]:
                    group['normalized_wer'] = group['errors']/group['words'] if group['words'] else None
                    group['real_time_factor'] = group['decode_s']/group['audio_s']
            except Exception as exc:
                row['error'] = str(exc)
            finally:
                if backend:
                    backend.cleanup()
            report['results'].append(row)
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps({k:v for k,v in row.items() if k != 'clips'}), flush=True)

    return int(any('error' in row for row in report['results']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, default=Path('local-asr-corpus-results.json'))
    parser.add_argument('--models')
    parser.add_argument('--cpu-only', action='store_true')
    parser.add_argument('--test-root', type=Path)
    raise SystemExit(run(parser.parse_args()))
