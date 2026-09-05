"""Prepare a small clean/noisy comparison from the public LibriSpeech dummy parquet.

Requires developer-only pyarrow and soundfile. The input is downloaded separately
with hf; this script never contacts the network.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path


def prepare(parquet: Path, output: Path, count: int):
    import numpy as np
    import pyarrow.parquet as pq
    import soundfile as sf

    rows = pq.read_table(parquet).to_pylist()
    if not 1 <= count <= len(rows):
        raise ValueError(f'count must be between 1 and {len(rows)}')
    output.mkdir(parents=True, exist_ok=True)
    clips, speakers = [], set()
    for index in np.linspace(0, len(rows)-1, count, dtype=int):
        row = rows[index]
        speakers.add(row['speaker_id'])
        audio, rate = sf.read(io.BytesIO(row['audio']['bytes']), dtype='float32')
        for group in ('clean', 'noise10db'):
            frames = audio.copy()
            if group == 'noise10db':
                noise = np.random.default_rng(int(index)).normal(size=frames.shape)
                frames = frames + noise*(np.sqrt(np.mean(frames**2))/np.sqrt(np.mean(noise**2))/10**.5)
            name = f"{row['id']}-{group}.wav"
            sf.write(output/name, frames, rate, subtype='PCM_16')
            clips.append(dict(id=row['id'], audio_path=name, reference=row['text'], group=group))
    manifest = dict(source=dict(
        dataset='hf-internal-testing/librispeech_asr_dummy', split='clean/validation',
        parquet_sha256=hashlib.sha256(parquet.read_bytes()).hexdigest(),
        selection=f'{count} evenly spaced rows from {len(rows)} (numpy linspace integer indices)',
        speaker_ids=sorted(speakers), noise='White Gaussian noise, seed=row index, 10 dB whole-clip RMS SNR',
    ), clips=clips)
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(output/'manifest.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('parquet', type=Path)
    parser.add_argument('--output', type=Path, default=Path('.tmp/local-asr-eval'))
    parser.add_argument('--count', type=int, default=16)
    args = parser.parse_args()
    prepare(args.parquet, args.output, args.count)
