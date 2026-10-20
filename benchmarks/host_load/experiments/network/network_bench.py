"""Real host/client TLS latency under explicitly limited userspace link pacing."""
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave
from paced_proxy import PacedProxy

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
from options import add_common, configure
add_common(parser)
parser.add_argument("--codec-backend", choices=("native", "ffmpeg"), default="native")
parser.add_argument('--quick', action='store_true')
parser.add_argument('--shapes', default='')
parser.add_argument('--clients', default='1,3,6')
parser.add_argument('--durations', default='3,15,30')
parser.add_argument('--label', default='exploratory')
parser.add_argument('--reps', type=int, default=10)
args = configure(parser, parser.parse_args())
import numpy as np
sys.path.insert(0, args.source_root)
DATA = tempfile.TemporaryDirectory(prefix='ow-network-')
os.environ['OPENWHISPER_DATA_DIR'] = DATA.name
from services.remote_asr import tailscale
from services.remote_asr.client import RemoteConnection
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity

tailscale.status = lambda timeout=4.0: tailscale.TailscaleStatus('not_installed')
tailscale.whois = lambda address, timeout=4.0: None


class DelayEngine(HostEngine):
    """Serialized affine fake, not measured ASR hardware."""
    identity = ('network-bench',)
    def __init__(self):
        self.lock = threading.Lock()
    def describe(self):
        return dict(family='parakeet', model='network-fake', label='Network Fake',
                    available=True, streaming=False, device='fake', status='ready')
    def transcribe(self, audio, language, **kwargs):
        with self.lock:
            time.sleep(.012 + .0062 * len(audio)/16000)
        return {'text':'network benchmark', 'segments':[]}


class Store:
    def __init__(self):
        self.devices = []
    def load(self):
        return [dict(x) for x in self.devices]
    def save(self, devices):
        self.devices = [dict(x) for x in devices]


def main():
    started_utc = datetime.now(timezone.utc).isoformat()
    with wave.open(str(args.audio)) as f:
        source = np.frombuffer(f.readframes(f.getnframes()), '<i2').astype(np.float32)/32768
    results = []
    shapes = [(0,10000,0), (20,20,0), (20,5,0), (50,10,0), (100,20,0), (100,5,0), (100,5,.005)]
    if args.shapes:
        shapes = [(*map(float, item.split(':')),0) for item in args.shapes.split(',')]
    counts = list(map(int,args.clients.split(',')))
    seconds_set = list(map(int,args.durations.split(',')))
    if args.quick:
        shapes, counts, seconds_set = [(0,10000,0),(100,5,0)], [1], [3,30]
    for rtt, mbps, stalls in shapes:
        for n in counts:
            store = Store()
            registry = DeviceRegistry(store.load, store.save)
            engine = DelayEngine()
            host = SpeechHost(engine_provider=lambda:engine, registry=registry,
                              identity=ensure_host_identity(str(Path(DATA.name)/f'id-{rtt}-{mbps}-{n}')),
                              host_name='isolated-network-bench')
            host.start(port=0,bind='127.0.0.1')
            clients=[]
            try:
                with PacedProxy(host.port, mbps=mbps,rtt_ms=rtt,stall_probability=stalls) as proxy:
                    for i in range(n):
                        _device, token = registry.add(f'load-{i}')
                        c = RemoteConnection('127.0.0.1',proxy.port,token,host.identity.fingerprint)
                        c.connect()
                        clients.append(c)
                    with ThreadPoolExecutor(max_workers=n) as pool:
                        for seconds in seconds_set:
                            audio = np.resize(source,16000*seconds)
                            barrier = threading.Barrier(n)
                            def one_client(index):
                                rows=[]
                                for rep in range(args.reps):
                                    barrier.wait()
                                    start=time.perf_counter()
                                    try:
                                        result,host_s = clients[index].request_timed('transcribe',audio=audio,language='en',timeout=30)
                                        error=None
                                    except Exception as exc:
                                        host_s=None
                                        error=type(exc).__name__+': '+str(exc)
                                    elapsed=time.perf_counter()-start
                                    rows.append(dict(rtt_ms=rtt,mbps=mbps,stall_probability=stalls,clients=n,
                                                     seconds=seconds,rep=rep,client=index,elapsed_ms=elapsed*1000,
                                                     host_ms=None if host_s is None else host_s*1000,error=error))
                                return rows
                            rows=sum(list(pool.map(one_client,range(n))),[])
                            results.extend(rows)
                            print(json.dumps(dict(rtt_ms=rtt,mbps=mbps,n=n,seconds=seconds,p50_ms=float(np.median([r['elapsed_ms'] for r in rows])),errors=sum(r['error'] is not None for r in rows))),flush=True)
                            Path(args.output).write_text(json.dumps(dict(label=args.label,source_root=args.source_root,source_revision=args.source_revision,audio_path=str(args.audio),audio_sha256=args.audio_sha256,started_utc=started_utc,checkpoint_utc=datetime.now(timezone.utc).isoformat(),note='Real SpeechHost/RemoteConnection TLS; fake affine serialized engine; userspace aggregate byte pacing, NOT tc netem. Stalls are sensitivity, NOT packet loss.',rows=results),indent=2))
                    for c in clients:
                        c.close()
            finally:
                for c in clients:
                    c.close()
                host.stop()
    DATA.cleanup()

if __name__=='__main__':
    main()
