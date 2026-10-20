"""Real Parakeet CPU pinned runtime over OpenWhisper TLS, experimental native batching.

worker = current production SpeechWorkerEngine/LocalSpeechBackend/SpeechProcess.
serialized/native = direct verified C ABI adapter, NOT production IPC, for mechanism
comparison only. Never substitute CPU timings for RTX2060/1050Ti results.
"""
from __future__ import annotations
import os
os.environ['ORT_DISABLE_TELEMETRY']='1'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['DO_NOT_TRACK']='1'
import argparse
import math,array,ctypes as c,json,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import numpy as np
import psutil
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from real_transport import audio_file,REPO,BASELINE
from experiment_paths import PATHS,add_paths,required
sys.path.insert(0,str(REPO))
from benchmarks.host_load.runner import BenchmarkHost,default_args
sys.path.insert(0,str(BASELINE))

class BatchingConfig(c.Structure):
    _fields_=[('size',c.c_size_t),('enable',c.c_bool),('max_batch_size',c.c_int32),
              ('max_queue_delay_us',c.c_int32),('max_queue_depth',c.c_int32),
              ('ingress_cohort_delay_us',c.c_int32),('state_arena_slots',c.c_int32)]

class NativeEngine:
    accepts_phrases=False
    identity=('experimental-native-parakeet','cpu')
    def __init__(self,enabled,window_ms,serialized=False):
        from services.local_asr.nvidia import NvidiaRecognizer,_load_linux,BackendConfig,ModelConfig,RecognizerConfig
        self.recognizer=NvidiaRecognizer.__new__(NvidiaRecognizer)
        r=self.recognizer
        r._dll_dir=None;r.gpu_name='';r.streams={};r._stream_contexts={}
        r.lib=_load_linux(str(required(PATHS.native_runtime,'--native-runtime')),'cpu');r._bind()
        backend=BackendConfig(c.sizeof(BackendConfig),-1)
        model=ModelConfig(c.sizeof(ModelConfig),str(required(PATHS.native_model,'--native-model')).encode(),None)
        batching=BatchingConfig(c.sizeof(BatchingConfig),enabled,8,int(window_ms*1000),64,0,8)
        config=RecognizerConfig();config.size=c.sizeof(RecognizerConfig)
        config.backend=c.addressof(backend);config.model=c.addressof(model)
        config.batching=c.addressof(batching)
        r.handle=c.c_void_p()
        r._check(r.lib.nemo_speech_asr_create(c.byref(config),c.byref(r.handle)))
        self.lock=threading.Lock() if serialized else None
    def describe(self):
        return dict(family='parakeet',model='parakeet-v3',label='Real native Parakeet CPU',
                    device='cpu',streaming=False,available=True,status='benchmark')
    def transcribe(self,audio,language,**kwargs):
        start=time.perf_counter()
        if self.lock:self.lock.acquire()
        acquired=time.perf_counter()
        try:
            result=self.recognizer.transcribe(array.array('f',audio),'en')
            result['_benchmark']={'engine_lock_wait_s':acquired-start,'decode_s':time.perf_counter()-acquired}
            return result
        finally:
            if self.lock:self.lock.release()
    def cancel_stream(self,session):pass
    def close(self):self.recognizer.close()


def main():
    p=add_paths(argparse.ArgumentParser())
    p.add_argument('--mode',choices=['worker','serialized','native'],default='worker')
    p.add_argument('--batching',type=int,choices=[0,1],default=0)
    p.add_argument('--window-ms',type=float,default=5)
    p.add_argument('--clients',type=int,default=2)
    p.add_argument('--repetitions',type=int,default=2)
    p.add_argument('--active-jobs',type=int,default=2)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    if args.clients<1 or args.repetitions<1 or args.active_jobs<1:
        p.error('clients, repetitions and active-jobs must be positive')
    if not math.isfinite(args.window_ms) or not 0<=args.window_ms<=1000:
        p.error('window-ms must be finite and between 0 and 1000')
    from services import components
    from services.local_asr import cache
    from services.remote_asr import host as host_module
    from services.remote_asr.engines import SpeechWorkerEngine
    from transcriber.optional_backend import LocalSpeechBackend
    original=host_module.SpeechHost
    def configured_host(**kwargs):return original(**kwargs,max_active_jobs=args.active_jobs)
    def load(fixture):
        load_started=time.perf_counter()
        if args.mode!='worker':
            engine=NativeEngine(bool(args.batching),args.window_ms,args.mode=='serialized')
            meta['engine_load_s']=time.perf_counter()-load_started
            fixture.stack.callback(engine.close)
            return engine
        backend=LocalSpeechBackend('parakeet',model_name='parakeet-v3',device='cpu')
        backend.reload_model()
        if not backend.is_available():raise RuntimeError(backend.last_error)
        meta['engine_load_s']=time.perf_counter()-load_started
        fixture.stack.callback(backend.cleanup)
        return SpeechWorkerEngine(backend)
    audio=audio_file(required(PATHS.audio_file,'--audio-file'))
    rows=[]
    process=psutil.Process()
    meta={'args':vars(args),'transport':'real TLS loopback SpeechHost/RemoteConnection',
          'source_host':str(host_module.__file__),'audio_s':len(audio)/16000,'runtime':'NeMo-Speech.cpp0.1.0 official CPU archive',
          'expected_pinned_gguf_sha256':'e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e',
          'threads_before':process.num_threads(),'rss_before':process.memory_info().rss}
    with ExitStack() as stack:
        stack.enter_context(patch.object(components,'is_installed',lambda component:True))
        stack.enter_context(patch.object(components,'component_dir',lambda component:str(required(PATHS.native_runtime,'--native-runtime'))))
        stack.enter_context(patch.object(cache,'is_cached',lambda model:True))
        stack.enter_context(patch.object(cache,'load_path',lambda model:str(required(PATHS.native_model,'--native-model'))))
        stack.enter_context(patch.object(BenchmarkHost,'_make_engine',load))
        stack.enter_context(patch.object(host_module,'SpeechHost',configured_host))
        with BenchmarkHost(default_args(clients=args.clients,engine='real',audio_file=required(PATHS.audio_file,'--audio-file'))) as fixture:
            clients=[fixture.new_client(i) for i in range(args.clients)]
            warm,timing=clients[0].request_timed('transcribe',audio=audio,language='en',timeout=120)
            meta['warmup_text']=warm['text'];meta['warmup_host_s']=timing
            started=time.perf_counter()
            for rep in range(args.repetitions):
                barrier=threading.Barrier(args.clients)
                def request(i):
                    barrier.wait();t=time.perf_counter()
                    try:
                        result,host_s=clients[i].request_timed('transcribe',audio=audio,language='en',timeout=120)
                        return {'repetition':rep,'client':i,'latency_s':time.perf_counter()-t,'host_s':host_s,
                            'status':'ok','text':result['text'],**result.get('_benchmark',{})}
                    except Exception as exc:return {'repetition':rep,'client':i,'latency_s':time.perf_counter()-t,'status':'error','error':repr(exc)}
                with ThreadPoolExecutor(args.clients) as pool:wave_rows=list(pool.map(request,range(args.clients)))
                rows.extend(wave_rows);print(json.dumps({'wave':rep,'rows':wave_rows}),flush=True)
            meta['timed_wall_s']=time.perf_counter()-started
    meta.update(threads_after=process.num_threads(),rss_after=process.memory_info().rss)
    ok=[r for r in rows if r['status']=='ok']
    summary={'samples':len(rows),'successes':len(ok),'errors':len(rows)-len(ok),
        'p50_s':float(np.percentile([r['latency_s'] for r in ok],50)) if ok else None,
        'p95_s':float(np.percentile([r['latency_s'] for r in ok],95)) if ok else None,
        'audio_seconds_per_wall_second':meta['audio_s']*len(ok)/meta['timed_wall_s'],
        'transcript_matches_warmup':sum(r['text']==meta['warmup_text'] for r in ok)}
    Path(args.output).write_text(json.dumps({'metadata':meta,'summary':summary,'rows':rows},indent=2,default=str)+'\n')
    print(json.dumps(summary),flush=True)
if __name__=='__main__':main()
