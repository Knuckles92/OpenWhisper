"""Real TLS records begin/abort churn; measure retained upload lock bookkeeping."""
import gc
import json
from pathlib import Path
import sys
import time
import tracemalloc
import network_bench as bench
from services.remote_records.host_store import HostRecordStore

ROOT=Path(__file__).resolve().parent
store=bench.Store()
registry=bench.DeviceRegistry(store.load,store.save)
engine=bench.DelayEngine()
records=HostRecordStore(str(Path(bench.DATA.name)/'records'),{})
host=bench.SpeechHost(engine_provider=lambda:engine,registry=registry,
                     identity=bench.ensure_host_identity(str(Path(bench.DATA.name)/'tls')),
                     host_name='lock-churn',records=records.handle,records_enabled=lambda:True,
                     records_summary=lambda device:{})
host.start(port=0,bind='127.0.0.1')
_,token=registry.add('churn-client')
client=bench.RemoteConnection('127.0.0.1',host.port,token,host.identity.fingerprint)
client.connect()
tracemalloc.start()
start=time.perf_counter()
rows=[]
try:
    for i in range(5001):
        if i in (0,100,1000,5000):
            gc.collect()
            current,peak=tracemalloc.get_traced_memory()
            size=sys.getsizeof(records._locks)+sum(sys.getsizeof(k)+sys.getsizeof(v) for k,v in records._locks.items())
            rows.append(dict(records=i,locks=len(records._locks),lock_map_bytes_lower_bound=size,
                             traced_current=current,traced_peak=peak,wall_s=time.perf_counter()-start))
            print(json.dumps(rows[-1]),flush=True)
        if i==5000:break
        name=f'churn-{i}'
        client.request('records_begin',kind='dictation',record_id=name,files=1,bytes=0)
        client.request('records_abort',kind='dictation',record_id=name)
finally:
    client.close()
    host.stop()
    tracemalloc.stop()
    bench.args.output.write_text(json.dumps(rows,indent=2))
