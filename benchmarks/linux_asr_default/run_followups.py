import hashlib
import json
from pathlib import Path
import threading
import time

import run_comparison as common
from benchmarks.live_preview import replay, aggregate
from faster_whisper.audio import decode_audio
import numpy as np
from config import config
from services.recognition_context import RecognitionContext
from transcriber.local_backend import LocalWhisperBackend


ROOT = Path(__file__).resolve().parent
manifest=json.loads((ROOT/'corpus/manifest.json').read_text())
clips=manifest['clips']
report=dict(source_commit='0546928e5b0cd40f36db721b22693a9f66e0a283',
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            language_sensitivity=[],preview=[],lifecycle=[])


def save():
    (ROOT/'followups.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


for profile in ['whisper_base_cpu','whisper_turbo_gpu']:
    backend=None
    row=dict(profile=profile,language='en',clips=[])
    print('ENGLISH',profile,flush=True)
    try:
        backend=common.make_backend(profile)
        if not backend.is_available(): raise RuntimeError(backend.device_info)
        row.update(actual_device=backend.device,compute_type=backend._compute_type)
        targets=[c for c in clips if c['group'] in ('ami_conversation','librispeech_other')]
        backend.transcribe(str(ROOT/'corpus'/targets[0]['audio_path']),recognition=RecognitionContext(language='en'))
        for clip in targets:
            start=time.perf_counter()
            text=backend.transcribe(str(ROOT/'corpus'/clip['audio_path']),recognition=RecognitionContext(language='en'))
            row['clips'].append(dict(id=clip['id'],group=clip['group'],decode_s=time.perf_counter()-start,
                                     transcript=text,score=common.score_text(clip['reference'],text)))
        for group in ('ami_conversation','librispeech_other'):
            chosen=[c for c in row['clips'] if c['group']==group]
            words=sum(c['score']['words'] for c in chosen)
            errors=sum(c['score']['errors'] for c in chosen)
            row[group]=dict(words=words,errors=errors,wer=errors/words,
                            decode_s=sum(c['decode_s'] for c in chosen))
    except Exception as exc:
        row['error']=str(exc)
    finally:
        if backend: backend.cleanup()
        report['language_sensitivity'].append(row)
        save()
        print('ENGLISH_DONE',profile,{k:v for k,v in row.items() if k!='clips'},flush=True)

for profile in ['parakeet_cpu','parakeet_gpu']:
    backend=None
    row=dict(profile=profile,language='auto',clips=[])
    print('PARAKEET_AUTO',profile,flush=True)
    try:
        backend=common.make_backend(profile)
        if not backend.is_available(): raise RuntimeError(backend.device_info)
        row.update(actual_device=backend.device,runtime_component=backend.runtime_component)
        targets=[c for c in clips if c['id'] in ['IN1001','IN1012','IN1016']]
        targets += [c for c in clips if c['group']=='librispeech_other'][::4]
        for clip in targets:
            start=time.perf_counter()
            text=backend.transcribe(str(ROOT/'corpus'/clip['audio_path']),recognition=RecognitionContext(language='auto'))
            row['clips'].append(dict(id=clip['id'],group=clip['group'],decode_s=time.perf_counter()-start,
                                     transcript=text,score=common.score_text(clip['reference'],text)))
        row['default_vs_auto_changed_clips']=[]
        baseline=json.loads((ROOT/'comparison.json').read_text())
        original=next(r for r in baseline['runs'] if r['profile']==profile)
        for c in row['clips']:
            old=next(x for x in original['clips'] if x['id']==c['id'])
            if c['transcript']!=old['transcript']: row['default_vs_auto_changed_clips'].append(c['id'])
    except Exception as exc:
        row['error']=str(exc)
    finally:
        if backend: backend.cleanup()
        report['language_sensitivity'].append(row)
        save()
        print('PARAKEET_AUTO_DONE',profile,row.get('default_vs_auto_changed_clips',row.get('error')),flush=True)

targets=[c for c in clips if c['id'] in ['IN1001','IN1009','IN1013']]
targets += [c for c in clips if c['group']=='librispeech_clean' and c['duration_s']>8][:2]
for repeat in range(2):
    profiles=['tiny_cpu','parakeet_cpu','tiny_gpu','parakeet_gpu']
    for profile in profiles if repeat==0 else list(reversed(profiles)):
        backend=None
        row=dict(profile=profile,pass_index=repeat+1,clips=[])
        print('PREVIEW',repeat+1,profile,flush=True)
        try:
            if profile.startswith('tiny'):
                backend=LocalWhisperBackend(str(ROOT/'whisper-tiny.en'),
                    device='cpu' if profile=='tiny_cpu' else 'auto',compute_type='auto')
            else:
                backend=common.make_backend(profile)
            if not backend.is_available(): raise RuntimeError(backend.device_info)
            row.update(actual_device=backend.device,compute_type=getattr(backend,'_compute_type',None),
                       runtime_component=getattr(backend,'runtime_component',None))
            for clip in targets:
                audio=decode_audio(str(ROOT/'corpus'/clip['audio_path']),sampling_rate=config.SAMPLE_RATE)
                pcm=np.rint(np.clip(audio,-1,32767/32768)*32768).astype(np.int16)
                if not row['clips']:
                    replay(backend,pcm,clip['reference'],mode='window',cadence=3.,overlap=.75)
                result=replay(backend,pcm,clip['reference'],mode='window',cadence=3.,overlap=.75)
                result.update(id=clip['id'],group=clip['group'])
                row['clips'].append(result)
            row['summary']=aggregate(row['clips'],3.0)
            # Check repeated reload and cleanup with the same production adapter.
            backend.cleanup()
            backend.reload_model(model_name=backend.model_name)
            text=backend.transcribe(str(ROOT/'corpus'/targets[0]['audio_path']))
            report['lifecycle'].append(dict(profile=profile,pass_index=repeat+1,
                available_after_reload=backend.is_available(),nonempty_after_reload=bool(text)))
        except Exception as exc:
            row['error']=str(exc)
        finally:
            if backend: backend.cleanup()
            report['preview'].append(row)
            save()
            print('PREVIEW_DONE',repeat+1,profile,row.get('summary',row.get('error')),flush=True)

for profile in ['parakeet_cpu','parakeet_gpu']:
    backend=None
    row=dict(profile=profile,test='cancel_pending_decode_then_reload')
    print('CANCEL',profile,flush=True)
    try:
        backend=common.make_backend(profile)
        backend.transcribe(str(ROOT/'corpus'/clips[0]['audio_path']))
        request_started=threading.Event()
        original_request=backend._process.request
        def observed_request(op, **kwargs):
            if op=='transcribe': request_started.set()
            return original_request(op, **kwargs)
        backend._process.request=observed_request
        outcome={}
        def decode():
            try: outcome['text']=backend.transcribe(str(ROOT/'corpus/long-read-speech.wav'))
            except Exception as exc: outcome['error']=str(exc)
        thread=threading.Thread(target=decode,daemon=True)
        thread.start()
        if not request_started.wait(10): raise RuntimeError('Decode did not reach worker request')
        time.sleep(.2)
        row['decode_pending_before_cancel']=thread.is_alive()
        start=time.perf_counter()
        backend.cancel_transcription()
        row['cancel_call_s']=time.perf_counter()-start
        thread.join(timeout=5)
        row['decode_thread_stopped']=not thread.is_alive()
        row['decode_outcome']=outcome
        backend.reload_model(model_name='parakeet-v3')
        row['nonempty_after_reload']=bool(backend.transcribe(str(ROOT/'corpus'/clips[0]['audio_path'])))
        if not row['decode_pending_before_cancel'] or not row['decode_thread_stopped'] or not row['nonempty_after_reload']:
            raise RuntimeError('Cancel/reload lifecycle check failed')
    except Exception as exc:
        row['error']=str(exc)
    finally:
        if backend: backend.cleanup()
        report['lifecycle'].append(row)
        save()
        print('CANCEL_DONE',profile,row,flush=True)
print('FOLLOWUPS_DONE',flush=True)
