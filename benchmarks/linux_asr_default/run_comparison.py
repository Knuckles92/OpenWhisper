"""Run repeated Linux ASR comparisons through isolated production backends."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'source'
sys.path.insert(0, str(SOURCE))
os.environ.update(OPENWHISPER_DATA_DIR=str(ROOT/'settings'), HF_HUB_OFFLINE='1',
                  TRANSFORMERS_OFFLINE='1', QT_QPA_PLATFORM='offscreen')

import main as _bootstrap  # noqa: F401
from benchmarks.meeting_mode.metrics import score_text
from benchmarks.provenance import identity, model_identity
from transcriber.local_backend import LocalWhisperBackend
from transcriber.optional_backend import LocalSpeechBackend


def gpu_sample():
    try:
        p = subprocess.run(['nvidia-smi','--query-gpu=memory.used,utilization.gpu,temperature.gpu',
                            '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=4)
        return [float(x.strip()) for x in p.stdout.splitlines()[0].split(',')]
    except Exception:
        return None


def process_tree(pid):
    result=[]
    try:
        for child in Path(f'/proc/{pid}/task/{pid}/children').read_text().split():
            result += process_tree(int(child))
        stat=Path(f'/proc/{pid}/statm').read_text().split()
        result.append(int(stat[1])*os.sysconf('SC_PAGE_SIZE'))
    except (OSError,ValueError):
        pass
    return result


def environment():
    cpu=[line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
         if line.startswith('model name')][0]
    gpu=subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total',
                        '--format=csv,noheader'],capture_output=True,text=True).stdout.strip()
    return dict(cpu=cpu,gpu=gpu,platform=platform.platform(),python=platform.python_version(),
                meminfo=Path('/proc/meminfo').read_text(),loadavg=os.getloadavg(),gpu_sample=gpu_sample())


def make_backend(profile):
    if profile=='whisper_base_cpu':
        return LocalWhisperBackend(str(ROOT/'whisper-base'),device='cpu',compute_type='auto')
    if profile=='whisper_turbo_gpu':
        return LocalWhisperBackend('auto',device='auto',compute_type='auto')
    backend=LocalSpeechBackend('parakeet','parakeet-v3','cpu' if profile=='parakeet_cpu' else 'auto')
    backend.reload_model()
    return backend


def run_profile(profile, clips, manifest, pass_index, report, args):
    row=dict(profile=profile,pass_index=pass_index,clips=[],environment_before=environment())
    backend=None
    stop=threading.Event()
    peaks={'process_tree_rss_bytes':0,'gpu_total_used_mib':0}
    baseline=sum(process_tree(os.getpid()))
    def monitor():
        while not stop.wait(.25):
            peaks['process_tree_rss_bytes']=max(peaks['process_tree_rss_bytes'],sum(process_tree(os.getpid())))
            if profile.endswith('gpu'):
                sample=gpu_sample()
                if sample: peaks['gpu_total_used_mib']=max(peaks['gpu_total_used_mib'],sample[0])
    thread=threading.Thread(target=monitor,daemon=True)
    thread.start()
    print('LOAD',pass_index,profile,flush=True)
    try:
        start=time.perf_counter()
        backend=make_backend(profile)
        row['load_s']=time.perf_counter()-start
        if not backend.is_available(): raise RuntimeError(backend.device_info)
        row.update(actual_device=backend.device,model=backend.model_name,
                   compute_type=getattr(backend,'_compute_type',None),
                   runtime_component=getattr(backend,'runtime_component',None),device_info=backend.device_info)
        if profile.endswith('_cpu') and backend.device!='cpu': raise RuntimeError('CPU profile did not load CPU')
        if profile.endswith('_gpu') and backend.device!='cuda': raise RuntimeError('GPU profile did not load GPU')
        if profile not in report['model_identities']:
            report['model_identities'][profile]=model_identity(backend)
        start=time.perf_counter()
        warm=backend.transcribe(str(ROOT/'corpus'/clips[0]['audio_path']))
        row['first_decode_s']=time.perf_counter()-start
        row['first_decode_text']=warm
        for index,clip in enumerate(clips):
            start=time.perf_counter()
            text=backend.transcribe(str(ROOT/'corpus'/clip['audio_path']))
            elapsed=time.perf_counter()-start
            score=score_text(clip['reference'],text)
            row['clips'].append(dict(id=clip['id'],group=clip['group'],duration_s=clip['duration_s'],
                                     decode_s=elapsed,transcript=text,score=score))
            if (index+1)%10==0 or index==len(clips)-1:
                print('PROGRESS',pass_index,profile,index+1,len(clips),round(sum(c['decode_s'] for c in row['clips']),2),flush=True)
        row['groups']={}
        for group in sorted({c['group'] for c in clips}):
            chosen=[c for c in row['clips'] if c['group']==group]
            total={field:sum(c['score'][field] for c in chosen)
                   for field in ['words','errors','substitutions','deletions','insertions']}
            total.update(clips=len(chosen),audio_s=sum(c['duration_s'] for c in chosen),
                         decode_s=sum(c['decode_s'] for c in chosen))
            total['wer']=total['errors']/total['words'] if total['words'] else None
            total['rtf']=total['decode_s']/total['audio_s']
            row['groups'][group]=total
        row['decode_s']=sum(c['decode_s'] for c in row['clips'])
    except Exception as exc:
        row['error']=str(exc)
        row['traceback']=traceback.format_exc()
        print('ERROR',profile,row['error'],flush=True)
    finally:
        stop.set(); thread.join(timeout=6)
        row.update(resources=peaks,parent_baseline_rss_bytes=baseline)
        if backend: backend.cleanup()
        row['environment_after']=environment()
        report['runs'].append(row)
        (ROOT/args.output).write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('DONE',pass_index,profile,'error' if 'error' in row else round(row['decode_s'],2),flush=True)


def run(args):
    manifest=json.loads((ROOT/'corpus/manifest.json').read_text())
    clips=manifest['clips']
    if args.smoke: clips=[clips[0],next(c for c in clips if c['group']=='ami_conversation'),clips[-2],clips[-1]]
    for clip in clips:
        digest=hashlib.sha256((ROOT/'corpus'/clip['audio_path']).read_bytes()).hexdigest()
        if digest!=clip['wav_sha256']: raise ValueError('Audio identity mismatch: '+clip['id'])
    profiles=args.profiles.split(',')
    report=dict(schema=1,date='2026-10-09',source_commit='0546928e5b0cd40f36db721b22693a9f66e0a283',
                method='Same PCM16 16 kHz clips, production full-file adapters, fresh settings: Whisper auto language and Parakeet requests en, Whisper production beam=5/VAD; warmup per model, alternating profile order',
                provenance=identity(inputs=[ROOT/'corpus/manifest.json',ROOT/'run_comparison.py']),
                corpus=dict(clips=len(clips),audio_s=sum(c['duration_s'] for c in clips),sources=manifest['sources']),
                environment=environment(),model_identities={},runs=[])
    for repeat in range(args.repeats):
        order=profiles if repeat%2==0 else list(reversed(profiles))
        for profile in order: run_profile(profile,clips,manifest,repeat+1,report,args)
    return int(any('error' in row for row in report['runs']))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--profiles',default='whisper_base_cpu,parakeet_cpu,whisper_turbo_gpu,parakeet_gpu')
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--output',default='comparison.json')
    args=parser.parse_args()
    raise SystemExit(run(args))
