"""Real TLS and production HostRecordStore upload contention against fake ASR.

Records are staged and SHA-256 checked on actual temporary filesystem; no fake
storage callback. No record commit/import or user database is involved.
"""
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave

ROOT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
from options import add_common, configure
add_common(parser)
parser.add_argument('--seconds',type=float,default=45)
parser.add_argument('--repeats',type=int,default=3)
parser.add_argument('--uploaders',default='0,1,4')
parser.add_argument('--mib',type=int,default=64)
args=configure(parser,parser.parse_args())
import numpy as np
sys.path.insert(0,args.source_root)
DATA=tempfile.TemporaryDirectory(prefix='ow-records-bench-')
os.environ['OPENWHISPER_DATA_DIR']=DATA.name
from services.remote_asr import tailscale, protocol
from services.remote_asr.client import RemoteConnection
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity
from services.remote_records.host_store import HostRecordStore

tailscale.status=lambda timeout=4.0:tailscale.TailscaleStatus('not_installed')
tailscale.whois=lambda address,timeout=4.0:None

class Engine(HostEngine):
    identity=('records-bench',)
    def __init__(self):self.lock=threading.Lock()
    def describe(self):return dict(family='parakeet',model='records-fake',label='Record Fake',available=True,streaming=False,device='fake',status='ready')
    def transcribe(self,audio,language,**kwargs):
        with self.lock:time.sleep(.03)
        return {'text':'records benchmark','segments':[]}

class RegistryStore:
    def __init__(self):self.rows=[]
    def load(self):return [dict(x) for x in self.rows]
    def save(self,rows):self.rows=[dict(x) for x in rows]


def run(uploaders,repeat):
    root=Path(DATA.name)/f'{uploaders}-{repeat}'
    root.mkdir()
    records=HostRecordStore(str(root/'records'),{})
    store=RegistryStore()
    registry=DeviceRegistry(store.load,store.save)
    engine=Engine()
    host=SpeechHost(engine_provider=lambda:engine,registry=registry,
                    identity=ensure_host_identity(str(root/'tls')),host_name='records-isolated',
                    records=records.handle,records_enabled=lambda:True,records_summary=lambda device:{})
    queue_rows=[]
    local=threading.local()
    original_acquire=host._work_admission.acquire
    def acquire(*a,**kw):
        started=time.perf_counter()
        result=original_acquire(*a,**kw)
        local.wait_ms=(time.perf_counter()-started)*1000
        return result
    host._work_admission.acquire=acquire
    original_dispatch=host._dispatch_unpacked
    def dispatch(header,*a,**kw):
        start=time.perf_counter()
        result=original_dispatch(header,*a,**kw)
        queue_rows.append(dict(op=header.get('op'),queue_ms=getattr(local,'wait_ms',None),
                               dispatch_ms=(time.perf_counter()-start)*1000))
        return result
    host._dispatch_unpacked=dispatch
    host.start(port=0,bind='127.0.0.1')
    connections=[]
    for i in range(3+uploaders):
        _,token=registry.add(f'bench-{i}')
        connection=RemoteConnection('127.0.0.1',host.port,token,host.identity.fingerprint)
        connection.connect()
        connections.append(connection)
    chunk=np.random.default_rng(85).integers(0,256,protocol.RECORD_CHUNK_BYTES,dtype=np.uint8).tobytes()
    sha=hashlib.sha256()
    for _ in range(args.mib):sha.update(chunk)
    digest=sha.hexdigest()
    duration=args.seconds
    barrier=threading.Barrier(3+uploaders)
    rows=[]
    upload_rows=[]
    with wave.open(str(args.audio)) as f:
        audio=np.frombuffer(f.readframes(48000),'<i2').astype(np.float32)/32768
    def normal(i):
        rng=np.random.default_rng(100+i+repeat)
        barrier.wait()
        deadline=time.monotonic()+duration
        next_at=time.monotonic()
        while time.monotonic()<deadline:
            time.sleep(max(0,next_at-time.monotonic()))
            start=time.perf_counter()
            error=None
            try:_,host_s=connections[i].request_timed('transcribe',audio=audio,language='en',timeout=30)
            except Exception as exc:host_s=None;error=repr(exc)
            rows.append(dict(client=i,elapsed_ms=(time.perf_counter()-start)*1000,host_ms=None if host_s is None else host_s*1000,error=error))
            next_at+=float(rng.exponential(.45))
    def uploader(i):
        barrier.wait()
        deadline=time.monotonic()+duration
        serial=0
        while time.monotonic()<deadline:
            record=f'upload-{i}-{serial}'
            conn=connections[3+i]
            start=time.perf_counter()
            error=None
            count=0
            try:
                conn.request('records_begin',kind='dictation',record_id=record,files=1,bytes=args.mib*len(chunk))
                for offset in range(args.mib):
                    result=conn.request('records_put',kind='dictation',record_id=record,name='audio.wav',size=args.mib*len(chunk),sha256=digest,offset=offset*len(chunk),payload=chunk,timeout=30)
                    count+=len(chunk)
                assert result['done'] is True
                conn.request('records_abort',kind='dictation',record_id=record)
            except Exception as exc:error=repr(exc)
            upload_rows.append(dict(client=i,bytes=count,elapsed_ms=(time.perf_counter()-start)*1000,error=error))
            serial+=1
    start=time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=3+uploaders) as pool:
            futures=[pool.submit(normal,i) for i in range(3)]+[pool.submit(uploader,i) for i in range(uploaders)]
            for future in futures:future.result()
    finally:
        for c in connections:c.close()
        host.stop()
    result=dict(uploaders=uploaders,repeat=repeat,wall_s=time.perf_counter()-start,
                dictations=rows,uploads=upload_rows,host_operations=queue_rows)
    print(json.dumps(dict(uploaders=uploaders,repeat=repeat,dictations=len(rows),p95_ms=float(np.percentile([x['elapsed_ms'] for x in rows],95)),errors=sum(x['error'] is not None for x in rows),upload_mib=sum(x['bytes'] for x in upload_rows)/1024**2)),flush=True)
    return result

started_utc=datetime.now(timezone.utc).isoformat()
results=[]
for count in map(int,args.uploaders.split(',')):
    for repeat in range(args.repeats):
        results.append(run(count,repeat))
        Path(args.output).write_text(json.dumps(dict(file_mib=args.mib,source_root=args.source_root,source_revision=args.source_revision,audio_path=str(args.audio),audio_sha256=args.audio_sha256,started_utc=started_utc,checkpoint_utc=datetime.now(timezone.utc).isoformat(),note='Real loopback TLS, real HostRecordStore staged verified files, fake serialized30ms ASR. Three Poisson0.45s think-time clients; uploader saturation. No record commit/import.',runs=results),indent=2))
DATA.cleanup()
