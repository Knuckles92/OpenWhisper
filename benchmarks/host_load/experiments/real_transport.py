"""Isolated real-CPU SpeechHost TLS and cross-request microbatch experiment.

The experimental batch engine is not an OpenWhisper production implementation.
It uses common English/transcribe options, clips <=30s, no word timestamps, and
one immutable model. Production worker cancellation/reload and streaming remain
out of scope. Results must not be projected to GPU hardware.
"""
from __future__ import annotations
import argparse
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import os
os.environ["ORT_DISABLE_TELEMETRY"]="1"
os.environ["HF_HUB_DISABLE_TELEMETRY"]="1"
os.environ["DO_NOT_TRACK"]="1"
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import wave
import numpy as np
import psutil

import onnxruntime as ort
ort.disable_telemetry_events()
os.environ["HF_HUB_DISABLE_TELEMETRY"]="1"
os.environ["DO_NOT_TRACK"]="1"

from experiment_paths import PATHS,add_paths,required
ROOT=Path(__file__).resolve().parent
REPO=PATHS.harness_root.resolve()
BASELINE=PATHS.source_root.resolve()
sys.path.insert(0,str(REPO))
from benchmarks.host_load.runner import BenchmarkHost,default_args
sys.path.insert(0,str(BASELINE))
from services.remote_asr.engines import WhisperEngine


def audio_file(path):
    with wave.open(str(path),'rb') as wav:
        assert wav.getframerate()==16000 and wav.getsampwidth()==2 and wav.getnchannels()==1
        return np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(np.float32)/32768

@dataclass
class Job:
    audio: np.ndarray
    arrived: float = field(default_factory=time.perf_counter)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: Exception | None = None

class CrossRequestBatchEngine:
    """Bounded experimental adapter: one model, one decode owner, common options."""
    accepts_phrases=False
    identity=('experimental-cross-request-batch','tiny.en','cpu')
    def __init__(self,model,audio,batch_size,window_ms):
        from faster_whisper import BatchedInferencePipeline
        from faster_whisper.tokenizer import Tokenizer
        self.model=model
        self.pipeline=BatchedInferencePipeline(model)
        _,info=self.pipeline.transcribe(audio,language='en',beam_size=5,vad_filter=False,
            without_timestamps=True,word_timestamps=False,batch_size=1)
        self.options=info.transcription_options
        self.tokenizer=Tokenizer(model.hf_tokenizer,model.model.is_multilingual,task='transcribe',language='en')
        self.batch_size=batch_size
        self.window=window_ms/1000
        self.jobs=queue.Queue(maxsize=64)
        self.batches=[]
        self.thread=threading.Thread(target=self.run,name='experimental-microbatch',daemon=True)
        self.thread.start()
    def describe(self):
        return dict(family='local_whisper',model='tiny.en',label='EXPERIMENTAL CPU batch',device='cpu',
                    streaming=False,available=True,status='benchmark only')
    def transcribe(self,audio,language,**kwargs):
        if language!='en' or len(audio)>30*16000:
            raise ValueError('Experiment supports English clips at most30 seconds only')
        job=Job(audio)
        self.jobs.put(job,timeout=30)
        if not job.done.wait(120): raise TimeoutError('Experimental batch did not complete')
        if job.error: raise job.error
        return job.result
    def run(self):
        from faster_whisper.audio import pad_or_trim
        while True:
            first=self.jobs.get()
            if first is None: return
            batch=[first]
            deadline=first.arrived+self.window
            while len(batch)<self.batch_size:
                remaining=max(0,deadline-time.perf_counter())
                try:
                    item=self.jobs.get(timeout=remaining)
                except queue.Empty: break
                if item is None: return
                batch.append(item)
            started=time.perf_counter()
            try:
                features=np.stack([pad_or_trim(self.model.feature_extractor(j.audio)[...,:-1]) for j in batch])
                _,outputs=self.pipeline.generate_segment_batched(features,self.tokenizer,self.options)
                ended=time.perf_counter()
                for job,output in zip(batch,outputs,strict=True):
                    job.result={'text':self.tokenizer.decode(output['tokens']).strip(),'segments':[],
                        '_benchmark':{'batch_size':len(batch),'batch_wait_s':started-job.arrived,
                                      'decode_s':ended-started}}
                self.batches.append({'size':len(batch),'decode_s':ended-started,
                    'waits_s':[started-j.arrived for j in batch]})
            except Exception as exc:
                for job in batch: job.error=exc
            finally:
                for job in batch: job.done.set()
    def close(self):
        self.jobs.put(None)
        self.thread.join(5)
        if self.thread.is_alive(): raise RuntimeError('Batch thread leaked')


def main():
    p=add_paths(argparse.ArgumentParser())
    p.add_argument('--mode',choices=['production','direct','batch'],default='production')
    p.add_argument('--clients',type=int,default=4)
    p.add_argument('--repetitions',type=int,default=4)
    p.add_argument('--batch-size',type=int,default=1)
    p.add_argument('--window-ms',type=float,default=0)
    p.add_argument('--cpu-threads',type=int,default=2)
    p.add_argument('--active-jobs',type=int,default=2)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    if args.clients<1 or args.repetitions<1 or args.active_jobs<1:
        p.error('clients, repetitions and active-jobs must be positive')
    if not math.isfinite(args.window_ms) or not 0<=args.window_ms<=1000:
        p.error('window-ms must be finite and between 0 and 1000')
    if args.batch_size<1 or args.cpu_threads<1:
        p.error('batch-size and cpu-threads must be positive')
    from faster_whisper import WhisperModel
    import faster_whisper,ctranslate2
    from services.remote_asr import host as host_module
    audio=audio_file(required(PATHS.audio_file,'--audio-file'))
    model_load_started=time.perf_counter()
    if args.mode=='production':
        from services.isolated import IsolatedWhisperModel
        model=IsolatedWhisperModel(str(required(PATHS.whisper_model,'--whisper-model')),device='cpu',compute_type='int8',cpu_threads=args.cpu_threads,num_workers=1,local_files_only=True)
        model.load()
    else:
        model=WhisperModel(str(required(PATHS.whisper_model,'--whisper-model')),device='cpu',compute_type='int8',cpu_threads=args.cpu_threads,num_workers=1,local_files_only=True)
    model_load_s=time.perf_counter()-model_load_started
    if args.mode in ('production','direct'):
        backend=SimpleNamespace(model=model,last_loaded_model='tiny.en',device='cpu',compute_type='int8',
            is_available=lambda:True,device_info='Real CPU tiny.en')
        engine=WhisperEngine(backend)
    else:
        engine=CrossRequestBatchEngine(model,audio,args.batch_size,args.window_ms)
    original=host_module.SpeechHost
    def configured_host(**kwargs):
        return original(**kwargs,max_active_jobs=args.active_jobs)
    rows=[]
    process=psutil.Process()
    meta={'args':vars(args),'model_load_s':model_load_s,'faster_whisper':faster_whisper.__version__,'ctranslate2':ctranslate2.__version__,
          'audio_seconds':len(audio)/16000,'audio_sha256':hashlib.sha256((required(PATHS.audio_file,'--audio-file')).read_bytes()).hexdigest(),
          'source_host':str(host_module.__file__),'transport':'real TLS loopback SpeechHost/RemoteConnection',
          'engine_path':('production isolated faster-whisper IPC' if args.mode=='production' else 'direct real faster-whisper, not isolated production IPC'),
          'batch_limitations':'English shared options, <=30s clips, text only; not production compatible timestamps/cancel/reload',
          'threads_before':process.num_threads(),'rss_before':process.memory_info().rss}
    started=time.perf_counter()
    with patch.object(BenchmarkHost,'_make_engine',lambda self:engine),patch.object(host_module,'SpeechHost',configured_host):
        with BenchmarkHost(default_args(clients=args.clients,engine='real',audio_file=required(PATHS.audio_file,'--audio-file'))) as fixture:
            clients=[fixture.new_client(i) for i in range(args.clients)]
            warm,warm_host=clients[0].request_timed('transcribe',audio=audio,language='en',timeout=120)
            meta['warmup_text']=warm['text'];meta['warmup_host_s']=warm_host
            for repetition in range(args.repetitions):
                barrier=threading.Barrier(args.clients)
                def request(i):
                    barrier.wait();t=time.perf_counter()
                    try:
                        result,host_s=clients[i].request_timed('transcribe',audio=audio,language='en',timeout=120)
                        return {'repetition':repetition,'client':i,'latency_s':time.perf_counter()-t,
                                'host_s':host_s,'status':'ok','text':result['text'],**result.get('_benchmark',{})}
                    except Exception as exc:
                        return {'repetition':repetition,'client':i,'latency_s':time.perf_counter()-t,
                                'status':'error','error':repr(exc)}
                with ThreadPoolExecutor(args.clients) as pool:
                    wave_rows=list(pool.map(request,range(args.clients)))
                rows.extend(wave_rows)
                print(json.dumps({'wave':repetition,'rows':wave_rows}),flush=True)
    if args.mode=='batch': engine.close()
    if args.mode=='production': model.close()
    meta.update(wall_s=time.perf_counter()-started,threads_after=process.num_threads(),rss_after=process.memory_info().rss)
    ok=[r for r in rows if r['status']=='ok']
    summary={'samples':len(rows),'successes':len(ok),'errors':len(rows)-len(ok),
        'p50_s':float(np.percentile([r['latency_s'] for r in ok],50)) if ok else None,
        'p95_s':float(np.percentile([r['latency_s'] for r in ok],95)) if ok else None,
        'p99_s':float(np.percentile([r['latency_s'] for r in ok],99)) if ok else None,
        'transcript_matches_warmup':sum(r['text']==meta['warmup_text'] for r in ok)}
    Path(args.output).write_text(json.dumps({'metadata':meta,'summary':summary,'rows':rows,
        'batches':getattr(engine,'batches',[])},indent=2,default=str)+'\n')
    print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
