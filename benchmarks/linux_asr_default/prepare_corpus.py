import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import urllib.parse
import urllib.request
import wave

import numpy as np
from faster_whisper.audio import decode_audio


ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'corpus'
DEST.mkdir(exist_ok=True)


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=45) as response:
        return json.load(response)


def wav_write(path, samples):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())


def fetch_rows(item):
    config, offset = item
    query = urllib.parse.urlencode(dict(dataset='openslr/librispeech_asr', config=config,
                                       split='test', offset=offset, length=2))
    data = fetch_json('https://datasets-server.huggingface.co/rows?' + query)
    clips = []
    for entry in data['rows']:
        row = entry['row']
        name = f"librispeech-{config}-{row['id']}"
        flac = DEST / (name + '.flac')
        url = row['audio'][0]['src']
        revision = urllib.parse.urlsplit(url).path.split('/--/')[1]
        if not flac.exists():
            with urllib.request.urlopen(url, timeout=45) as response:
                flac.write_bytes(response.read())
        audio = decode_audio(str(flac), sampling_rate=16000)
        wav_write(DEST / (name + '.wav'), audio)
        clips.append(dict(id=name, audio_path=name+'.wav', group='librispeech_'+config,
                          reference=row['text'], duration_s=len(audio)/16000,
                          speaker_id=row['speaker_id'], source_revision=revision,
                          source_row=entry['row_idx'], flac_sha256=hashlib.sha256(flac.read_bytes()).hexdigest()))
    print('PREPARED', config, offset, flush=True)
    return clips


items = [('clean', offset) for offset in range(0, 2600, 200)]
items += [('other', offset) for offset in range(0, 2800, 250)]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    clips = [clip for group in executor.map(fetch_rows, items) for clip in group]

ami = ROOT.parents[1] / '.tmp/preview-optimization/corpus'
original = json.loads((ami/'manifest.json').read_text(encoding='utf-8-sig'))
for clip in original['clips']:
    if clip['group'] != 'ami_conversation':
        continue
    new = dict(clip)
    name = 'ami-' + clip['audio_path']
    shutil.copy2(ami/clip['audio_path'], DEST/name)
    new.update(audio_path=name, duration_s=len(decode_audio(str(DEST/name)))/16000)
    clips.append(new)

for clip in [c for c in clips if c['group']=='librispeech_clean'][::4]:
    audio = decode_audio(str(DEST/clip['audio_path']), sampling_rate=16000)
    rng = np.random.default_rng(int(hashlib.sha256(clip['id'].encode()).hexdigest()[:8],16))
    noise = rng.normal(size=len(audio)).astype(np.float32)
    # Seeded white noise at 10 dB SNR, measured over the complete source clip.
    noise *= np.sqrt(np.mean(audio**2)) / (np.sqrt(np.mean(noise**2))*np.sqrt(10))
    name = clip['id']+'-noise10.wav'
    wav_write(DEST/name, audio+noise)
    clips.append(dict(clip, id=clip['id']+'-noise10',audio_path=name,
                      group='synthetic_noise_10db',derived_from=clip['id'], snr_db=10))

clean = [c for c in clips if c['group']=='librispeech_clean']
selected = clean[::2][:10]
long_audio = np.concatenate([piece for clip in selected for piece in (
    decode_audio(str(DEST/clip['audio_path']), sampling_rate=16000), np.zeros(8000,np.float32))])
wav_write(DEST/'long-read-speech.wav', long_audio)
clips.append(dict(id='long-read-speech',audio_path='long-read-speech.wav',group='concatenated_read_speech',
                  reference=' '.join(c['reference'] for c in selected),
                  duration_s=len(long_audio)/16000,derived_from=[c['id'] for c in selected]))
for name, audio in [('silence', np.zeros(6*16000,np.float32)),
                    ('quiet-noise', np.random.default_rng(42).normal(0,0.005,6*16000).astype(np.float32))]:
    wav_write(DEST/(name+'.wav'),audio)
    clips.append(dict(id=name,audio_path=name+'.wav',group='nonspeech',reference='',duration_s=6))

for clip in clips:
    clip['wav_sha256']=hashlib.sha256((DEST/clip['audio_path']).read_bytes()).hexdigest()
manifest = dict(sources={
    'librispeech':{'repository':'https://huggingface.co/datasets/openslr/librispeech_asr',
                  'license':'CC BY 4.0','split':'test',
                  'selection':'Two rows at clean offsets 0..2400 step 200 and other offsets 0..2750 step 250; chosen before model output'},
    'ami':{'url':'https://groups.inf.ed.ac.uk/ami/corpus/','license':'CC BY 4.0',
           'selection':original['source'],
           'original_manifest_sha256':hashlib.sha256((ami/'manifest.json').read_bytes()).hexdigest()},
    'noise':{'kind':'seeded Gaussian white noise','snr_db':10,'caveat':'Synthetic robustness check, not real environmental noise'},
    'long':{'kind':'Ten clean utterances concatenated with 0.5-second silence','caveat':'Synthetic duration/lifecycle check, not a continuous meeting'}},
    clips=clips)
(DEST/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps({'clips':len(clips),'audio_s':sum(c['duration_s'] for c in clips),
                  'groups':{g:sum(c['group']==g for c in clips) for g in sorted({c['group'] for c in clips})},
                  'librispeech_speakers':len({c['speaker_id'] for c in clips if 'speaker_id' in c})}),flush=True)
