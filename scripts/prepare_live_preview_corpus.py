"""Prepare cached clean/noisy speech plus fixed AMI excerpts; never downloads."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.meeting_mode.ami import DEFAULT_MEETINGS, parse_reference_words


def prepare(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    clips = [dict(c, audio_path=str((args.manifest.parent/c['audio_path']).resolve()))
             for c in original['clips']]
    sources = []
    for meeting in DEFAULT_MEETINGS:
        path = args.ami_dir/'audio'/meeting.audio_filename
        words = parse_reference_words(args.ami_dir/'annotations', meeting.meeting_id)
        with wave.open(str(path), 'rb') as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            if width != 2:
                raise ValueError(f'Expected PCM16: {path}')
            start_frame = int((wav.getnframes()/rate-args.seconds)/2*rate)
            if start_frame < 0:
                raise ValueError('Excerpt is longer than meeting')
            wav.setpos(start_frame)
            frames = wav.readframes(round(args.seconds*rate))
        start = start_frame/rate
        end = start+len(frames)/(rate*channels*width)
        selected = [w for w in words if start <= (w.start_s+w.end_s)/2 < end]
        name = f'{meeting.meeting_id}-middle-{args.seconds:g}s.wav'
        with wave.open(str(output/name), 'wb') as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
            wav.writeframes(frames)
        annotations = sorted((args.ami_dir/'annotations'/'words').glob(f'{meeting.meeting_id}.*.words.xml'))
        sources.append(dict(meeting=meeting.meeting_id, start_s=start, end_s=end,
                            audio_url=meeting.audio_url,
                            annotations_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in annotations}))
        clips.append(dict(id=meeting.meeting_id, audio_path=name, group='ami_conversation',
                          reference=' '.join(w.text for w in selected),
                          words=[dict(text=w.text, end_s=min(end-start, w.end_s-start)) for w in selected]))
    with wave.open(str(output/'silence.wav'), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(6*16000*2))
    clips.append(dict(id='silence-6s', audio_path='silence.wav', reference='', group='silence'))
    manifest = dict(source=dict(librispeech=original['source'], ami=dict(
        annotations='AMI manual 1.6.2', license='CC BY 4.0',
        selection='Centered fixed-duration excerpt from each curated meeting; word midpoint determines membership',
        excerpts=sources)), clips=clips)
    destination = output/'manifest.json'
    destination.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--ami-dir', type=Path, default=Path('benchmarks/meeting_mode/data/ami'))
    parser.add_argument('--seconds', type=float, default=30.)
    parser.add_argument('--output', type=Path, default=Path('.tmp/live-preview/corpus'))
    args = parser.parse_args()
    if not 0 < args.seconds <= 30:
        parser.error('seconds must be in (0, 30]; meeting native previews reset at 30 seconds')
    prepare(args)
