"""Measure production preview chunks and native local streams without downloads.

Activate the venv, then run ``python -m benchmarks.live_preview --help``.
Replay does not sleep: measured service times drive a serial virtual clock.
Visibility excludes recorder queues, dropped blocks, and Qt paint latency.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time
from unittest.mock import patch

import numpy as np

from benchmarks.meeting_mode.metrics import edit_counts, normalize_tokens
from config import config
from services.streaming_transcriber import NativePreviewLedger, StreamingTranscriber


def score(reference, hypothesis):
    tokens = normalize_tokens(reference)
    counts = edit_counts(tokens, normalize_tokens(hypothesis))
    return dict(asdict(counts), words=len(tokens), errors=counts.errors,
                wer=counts.errors/len(tokens) if tokens else None)


def block_ends(length, sample_rate, cadence):
    # Recorder callbacks are indivisible: 3 s at 44.1 kHz takes 130 x 1024 frames.
    size = math.ceil(cadence*sample_rate/config.CHUNK_SIZE)*config.CHUNK_SIZE
    return list(range(size, length+1, size))


def visible_text(updates, at_s):
    return next((u['text'] for u in reversed(updates)
                 if u['visible_s'] <= at_s and not u['flush']), '')


# The production dictation preview assembles native events with this ledger,
# so the benchmark scores the text the app would show.
NativeTranscript = NativePreviewLedger


def replay(backend, audio, reference, *, mode, cadence, overlap, timed_words=None):
    rate = config.SAMPLE_RATE
    duration = len(audio)/rate
    preview = StreamingTranscriber(backend, cadence, overlap)
    preview.sample_rate = rate
    ledger = NativeTranscript()
    updates, calls = [], []
    clock, start, text = 0., 0, ''
    endpoints = [(end, False) for end in block_ends(len(audio), rate, cadence)]
    if not endpoints or endpoints[-1][0] < len(audio):
        endpoints.append((len(audio), True))
    if mode == 'native':
        endpoints.append((len(audio), True))  # Explicit empty final flush.
    session = 'live-preview-benchmark'
    try:
        for index, (end, flush) in enumerate(endpoints):
            final = mode == 'native' and index == len(endpoints)-1
            ready = end/rate
            began = time.perf_counter()
            if mode == 'window':
                count = preview._chunk_count
                preview._process_incremental_chunk([audio[start:end]])
                # Production catches inference errors; never score those as silence.
                if preview._chunk_count != count+1:
                    raise RuntimeError('Preview decode failed; inspect inference log')
                text, events = preview.preview_text, None
            else:
                frames = (preview._prepare_audio_for_whisper(audio[start:end])
                          if end > start else np.empty(0, np.float32))
                events = backend.stream_audio(session, frames, 'auto', finish=final)
                text = ledger.apply(events)
            elapsed = time.perf_counter()-began
            clock = max(clock, ready)+elapsed
            call = dict(audio_end_s=ready, service_s=elapsed, visible_s=clock,
                        backlog_s=clock-ready, flush=flush)
            calls.append(call)
            if text and (not updates or text != updates[-1]['text']):
                update = dict(call, text=text)
                if events is not None:
                    update['events'] = [{k: v for k, v in e.items() if k != 'words'} for e in events]
                updates.append(update)
            start = end
    finally:
        if mode == 'native':
            backend.cancel_stream(session)
    live = visible_text(updates, duration)
    result = dict(duration_s=duration, calls=calls, updates=updates,
                  live_text=live, drained_text=text, live=score(reference, live),
                  drained=score(reference, text),
                  first_text_before_stop_s=next((u['visible_s'] for u in updates
                      if not u['flush'] and u['visible_s'] <= duration), None),
                  drain_after_stop_s=max(0., clock-duration))
    if timed_words is not None:
        checkpoints = []
        for end in block_ends(len(audio), rate, config.STREAMING_CHUNK_DURATION_SEC):
            at = end/rate
            ref = ' '.join(w['text'] for w in timed_words if w['end_s'] <= at)
            checkpoints.append(dict(at_s=at, **score(ref, visible_text(updates, at))))
        result['checkpoints'] = checkpoints
    return result


def percentile(values, p):
    return float(np.percentile(values, p)) if values else None


def aggregate(clips, cadence):
    calls = [call for clip in clips for call in clip['calls'] if not call['flush']]
    services = [c['service_s'] for c in calls]
    first = [c['first_text_before_stop_s'] for c in clips if c['first_text_before_stop_s'] is not None]
    total_audio = sum(c['duration_s'] for c in clips)
    result = dict(clips=len(clips), audio_s=total_audio, calls=len(calls),
                  update_median_ms=percentile(services, 50)*1000 if services else None,
                  update_p95_ms=percentile(services, 95)*1000 if services else None,
                  first_text_median_s=percentile(first, 50),
                  clips_without_live_text=sum(not c['live_text'] for c in clips),
                  rtf=sum(call['service_s'] for c in clips for call in c['calls'])/total_audio,
                  update_deadline_misses=sum(s > cadence for s in services),
                  max_backlog_s=max((c['backlog_s'] for c in calls), default=0.))
    for field in ('live', 'drained'):
        counts = {k: sum(c[field][k] for c in clips)
                  for k in ('words', 'errors', 'substitutions', 'deletions', 'insertions')}
        result[field] = dict(counts, wer=counts['errors']/counts['words'] if counts['words'] else None)
    checkpoints = [p for c in clips for p in c.get('checkpoints', [])]
    words = sum(p['words'] for p in checkpoints)
    if checkpoints:
        result['checkpoint_wer'] = sum(p['errors'] for p in checkpoints)/words if words else None
    return result


def command_output(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def hardware():
    return dict(platform=platform.platform(), processor=platform.processor(),
                python=platform.python_version(), cpu_count=os.cpu_count(),
                gpu=command_output(['nvidia-smi', '--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu', '--format=csv']))


def load_clips(manifest_path, limit):
    from faster_whisper.audio import decode_audio
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    items = manifest['clips'][:limit]
    if not items:
        raise ValueError('Manifest contains no clips')
    clips = []
    for item in items:
        path = (manifest_path.parent/item['audio_path']).resolve()
        audio = decode_audio(str(path), sampling_rate=config.SAMPLE_RATE)
        if not len(audio):
            raise ValueError(f'Empty audio: {path}')
        # Match the recorder PCM16 callback and include overlap resampling cost.
        pcm = np.rint(np.clip(audio, -1, 32767/32768)*32768).astype(np.int16)
        clips.append((item, pcm, hashlib.sha256(path.read_bytes()).hexdigest()))
    return manifest, clips


def run(args):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    import app_qt  # Registers production native DLL search paths before loading.
    from faster_whisper.utils import download_model
    from services.local_asr import cache
    from services.local_asr.catalog import MODELS, artifacts
    from transcriber.local_backend import LocalWhisperBackend
    from transcriber.optional_backend import LocalSpeechBackend

    manifest, clips = load_clips(args.manifest, args.limit)
    keys = args.models.split(',') if args.models else ['tiny.en', *MODELS]
    if set(keys)-{'tiny.en', *MODELS}:
        raise ValueError('Unknown model key')
    report = dict(schema=1, started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  hardware=hardware(), git_head=command_output(['git', 'rev-parse', 'HEAD']),
                  git_status=command_output(['git', 'status', '--short']),
                  source=manifest.get('source'), manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                  repeats=args.repeats, sample_rate=config.SAMPLE_RATE, callback_frames=config.CHUNK_SIZE,
                  chunk_s=args.chunk, overlap_s=args.overlap, native_cadence_s=args.native_cadence,
                  normalization='meeting_mode.normalize_tokens: NFKC, lowercase; punctuation ignored, apostrophes retained, AMI acronym underscores removed; fillers/numbers retained',
                  timing='Measured serial service; predicted arrival=max(previous completion,audio end)+service. No sleeps, recorder queues/drops, GUI paint, or concurrent final ASR.',
                  live_accuracy='Full reference versus preview visible at audio EOF, before tail/finish; missing tails count as deletions.',
                  drained_accuracy='Diagnostic after remaining window decode or native finish; not the product final transcription or current stop_streaming behavior.',
                  checkpoint_accuracy='Timed clips: repeated prefix WER at common recorder-quantized 3 s checkpoints; reference words ending by checkpoint and updates completed by checkpoint.',
                  corpus=[dict(id=c.get('id', c['audio_path']), group=c.get('group', 'all'),
                               audio_sha256=sha, duration_s=len(a)/config.SAMPLE_RATE,
                               reference=c['reference']) for c, a, sha in clips], results=[])
    tracked_sources = ['config.py', 'services/streaming_transcriber.py', 'transcriber/optional_backend.py', 'services/local_asr/worker.py', 'services/local_asr/nvidia.py', 'services/local_asr/moonshine.py', 'benchmarks/live_preview.py']
    report['source_sha256'] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in tracked_sources}
    report['decoding'] = 'Window: production beam_size=1, vad_filter=False, default language; native: auto language, no overlap. Whisper tiny.en is English-only.'
    report['packages'] = {p: version(p) for p in ('faster-whisper', 'ctranslate2', 'numpy')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    with ExitStack() as context:
        if args.test_root:
            root = args.test_root.resolve()
            context.enter_context(patch.object(cache, 'local_app_dir', return_value=str(root/'test-app')))
            context.enter_context(patch('services.components.component_dir', side_effect=lambda k: str(root/'install-validated'/k)))
            context.enter_context(patch('services.components.is_installed', side_effect=lambda k: (root/'install-validated'/k/'python.exe').is_file()))
        for repeat in range(args.repeats):
            # Reverse alternating passes to expose order/thermal drift.
            for key in keys if repeat % 2 == 0 else list(reversed(keys)):
                backend = None
                row = dict(model=key, repeat=repeat+1, profiles=[], hardware_before=hardware())
                print(f'Loading {key}, pass {repeat+1}/{args.repeats}', flush=True)
                try:
                    device = 'cpu' if key.startswith('moonshine') else args.device
                    began = time.perf_counter()
                    if key == 'tiny.en':
                        backend = LocalWhisperBackend(key, device=device, compute_type='int8' if device == 'cpu' else 'float16')
                        row['model_snapshot'] = Path(download_model(key, local_files_only=True)).name
                    else:
                        backend = LocalSpeechBackend(MODELS[key].backend, key, device)
                        backend.reload_model()
                        row['artifacts'] = artifacts(key)
                        from services.components import component_dir
                        installed = Path(component_dir(backend.runtime_component))/'manifest.json'
                        row['runtime_manifest'] = json.loads(installed.read_text()) if installed.exists() else None
                    row['load_s'] = time.perf_counter()-began
                    row['requested_device'], row['actual_device'] = device, backend.device
                    row['device_info'] = backend.device_info
                    if not backend.is_available() or backend.device != device:
                        raise RuntimeError(backend.device_info)
                    modes = ['window']+(['native'] if key in MODELS and MODELS[key].streaming else [])
                    if args.mode != 'both':
                        modes = [m for m in modes if m == args.mode]
                    for mode in modes:
                        cadence = args.chunk if mode == 'window' else args.native_cadence
                        effective = math.ceil(cadence*config.SAMPLE_RATE/config.CHUNK_SIZE)*config.CHUNK_SIZE/config.SAMPLE_RATE
                        began = time.perf_counter()
                        replay(backend, clips[0][1], clips[0][0]['reference'], mode=mode, cadence=cadence, overlap=args.overlap)
                        profile = dict(mode=mode, effective_cadence_s=effective, warmup_s=time.perf_counter()-began, clips=[])
                        row['profiles'].append(profile)
                        for clip_index, (item, audio, _sha) in enumerate(clips):
                            if clip_index % 10 == 0:
                                print(f'  {key} {mode}: clip {clip_index+1}/{len(clips)}', flush=True)
                            result = replay(backend, audio, item['reference'], mode=mode, cadence=cadence,
                                            overlap=args.overlap, timed_words=item.get('words'))
                            result.update(id=item.get('id', item['audio_path']), group=item.get('group', 'all'))
                            profile['clips'].append(result)
                        profile['summary'] = aggregate(profile['clips'], effective)
                        profile['groups'] = {g: aggregate([c for c in profile['clips'] if c['group'] == g], effective)
                                             for g in sorted({c['group'] for c in profile['clips']})}
                        print(json.dumps(dict(model=key, mode=mode, **profile['summary'])), flush=True)
                except Exception as exc:
                    failed = True
                    row['error'] = f'{type(exc).__name__}: {exc}'
                    print(row['error'], flush=True)
                finally:
                    if backend is not None:
                        backend.cleanup()
                report['results'].append(row)
                args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, default=Path('.tmp/live-preview/results.json'))
    parser.add_argument('--models', help='Comma-separated keys; defaults to tiny.en and all optional models')
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cuda')
    parser.add_argument('--mode', choices=['window', 'native', 'both'], default='both')
    parser.add_argument('--chunk', type=float, default=config.STREAMING_CHUNK_DURATION_SEC)
    parser.add_argument('--overlap', type=float, default=config.STREAMING_OVERLAP_SEC)
    parser.add_argument('--native-cadence', type=float, default=.75)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--test-root', type=Path, help='Use existing test-app/speech-models and install-validated runtimes')
    args = parser.parse_args(argv)
    if args.repeats < 1 or (args.limit is not None and args.limit < 1):
        parser.error('repeats and limit must be positive')
    if not all(math.isfinite(v) for v in (args.chunk, args.native_cadence, args.overlap)):
        parser.error('cadences and overlap must be finite')
    if args.chunk <= 0 or args.native_cadence <= 0 or not 0 <= args.overlap < args.chunk:
        parser.error('cadences must be positive and overlap must be in [0, chunk)')
    return args


if __name__ == '__main__':
    raise SystemExit(run(parse_args()))

