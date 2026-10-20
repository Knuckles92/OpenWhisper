"""Measured codec sizes/times on public speech and deterministic noisy variants.
FFmpeg process startup is included; codec-only timing is not claimed.
"""
import hashlib
import os
import argparse
import json
import subprocess
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results'
DATA = ROOT / 'data'


def ffmpeg(data, codec):
    cmd = [os.environ.get('OW_BENCH_FFMPEG','ffmpeg'), '-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', '16000',
           '-ac', '1', '-i', 'pipe:0']
    if codec == 'flac':
        cmd += ['-c:a', 'flac', '-compression_level', '5', '-f', 'flac']
    else:
        cmd += ['-c:a', 'libopus', '-b:a', codec.split('_')[1] + 'k', '-application', 'voip',
                '-frame_duration', '20', '-f', 'ogg']
    start = time.perf_counter()
    blob = subprocess.run(cmd + ['pipe:1'], input=data, capture_output=True, check=True).stdout
    enc = time.perf_counter() - start
    start = time.perf_counter()
    decoded = subprocess.run([os.environ.get('OW_BENCH_FFMPEG','ffmpeg'), '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
                             '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1'], input=blob,
                            capture_output=True, check=True).stdout
    return blob, decoded, enc, time.perf_counter() - start


def main():
    global OUT, DATA
    from options import add_common, configure
    parser=argparse.ArgumentParser(description=__doc__)
    add_common(parser,host=False)
    args=configure(parser,parser.parse_args(),host=False)
    import numpy as np
    OUT=args.output.parent
    DATA=args.output.parent/(args.output.stem+"-encoded-examples")
    DATA.mkdir(exist_ok=False)
    with wave.open(str(args.audio)) as f:
        assert f.getframerate() == 16000 and f.getsampwidth() == 2 and f.getnchannels() == 1
        samples = np.frombuffer(f.readframes(f.getnframes()), '<i2')
    rng = np.random.default_rng(4729)
    rows = []
    for seconds in (3, 11, 30, 90):
        clean = np.resize(samples, seconds * 16000).astype(np.float64)
        for snr in (None, 20, 10):
            noise = np.zeros(len(clean)) if snr is None else rng.normal(
                0, np.sqrt(np.mean(clean**2)) / 10**(snr/20), len(clean))
            pcm = np.clip(np.rint(clean + noise), -32768, 32767).astype('<i2').tobytes()
            for codec in ('flac', 'opus_16', 'opus_24', 'opus_32'):
                for rep in range(3):
                    blob, decoded, enc, dec = ffmpeg(pcm, codec)
                    if codec == 'flac':
                        assert pcm == decoded
                    rows.append(dict(seconds=seconds, snr_db=snr, codec=codec, rep=rep,
                                     pcm_bytes=len(pcm), encoded_bytes=len(blob), ratio=len(blob)/len(pcm),
                                     encode_ms=enc*1000, decode_ms=dec*1000,
                                     exact_pcm_roundtrip=pcm == decoded,
                                     encoded_sha256=hashlib.sha256(blob).hexdigest()))
                # Small examples only for subsequent transport-size tests.
                (DATA / f'{seconds}s_{snr}_{codec}.bin').write_bytes(blob)
    result = dict(source=str(args.audio),
                  source_sha256=hashlib.sha256(args.audio.read_bytes()).hexdigest(),
                  note='Supplied local speech repeated for longer clips; deterministic added white noise. Not a speech corpus or WER evaluation. FFmpeg process launch included.', rows=rows)
    args.output.write_text(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
