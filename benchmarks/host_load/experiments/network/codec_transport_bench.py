"""Research-only codec transport, not current protocol support.

An experiment-only SpeechHost subclass decodes trusted benchmark payloads back
to PCM. Real authentication, pinned TLS, RemoteConnection and admission remain.
FFmpeg process overhead is included unless NATIVE_CODECS=1 selects in-process
libsndfile/libopus. This is NOT a
production codec implementation or an ASR accuracy evaluation.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import wave
import network_bench as bench
from paced_proxy import PacedProxy
import numpy as np
NATIVE = bench.args.codec_backend == "native"
if NATIVE:
    from native_codecs import flac_encode, flac_decode, opus_encode, opus_decode
FFMPEG = bench.args.ffmpeg

ROOT=Path(__file__).resolve().parent

class CodecHost(bench.SpeechHost):
    def _dispatch_unpacked(self,header,payload,*args,**kwargs):
        codec=header.get('experiment_codec')
        if codec and NATIVE:
            payload = flac_decode(payload) if codec == 'flac' else opus_decode(payload)
        elif codec:
            payload=subprocess.run([FFMPEG,'-hide_banner','-loglevel','error','-i','pipe:0',
                                    '-ar','16000','-ac','1','-t','120','-f','s16le','pipe:1'],
                                   input=payload,capture_output=True,check=True).stdout
        return super()._dispatch_unpacked(header,payload,*args,**kwargs)


def encode_only(pcm,codec):
    if NATIVE:
        return flac_encode(pcm) if codec=='flac' else opus_encode(pcm)
    cmd=[FFMPEG,'-hide_banner','-loglevel','error','-f','s16le','-ar','16000','-ac','1','-i','pipe:0']
    if codec=='flac':cmd+=['-c:a','flac','-compression_level','5','-f','flac']
    else:cmd+=['-c:a','libopus','-b:a','24k','-application','voip','-frame_duration','20','-f','ogg']
    return subprocess.run(cmd+['pipe:1'],input=pcm,capture_output=True,check=True).stdout


def main():
    started_utc=datetime.now(timezone.utc).isoformat()
    with wave.open(str(bench.args.audio)) as f:
        source=np.frombuffer(f.readframes(f.getnframes()),'<i2')
    rows=[]
    for rtt,mbps in [(0,10000),(20,20),(100,5)]:
        store=bench.Store()
        registry=bench.DeviceRegistry(store.load,store.save)
        engine=bench.DelayEngine()
        host=CodecHost(engine_provider=lambda:engine,registry=registry,
                       identity=bench.ensure_host_identity(str(Path(bench.DATA.name)/f'codec-id-{rtt}')),
                       host_name='isolated-codec-experiment')
        host.start(port=0,bind='127.0.0.1')
        conn=None
        try:
            with PacedProxy(host.port,mbps=mbps,rtt_ms=rtt) as proxy:
                _,token=registry.add('codec-bench')
                conn=bench.RemoteConnection('127.0.0.1',proxy.port,token,host.identity.fingerprint)
                conn.connect()
                for seconds in (3,15,30):
                    pcm=np.resize(source,seconds*16000).astype('<i2').tobytes()
                    for codec in ('pcm','flac','opus_24'):
                        for rep in range(bench.args.reps):
                            start=time.perf_counter()
                            blob=pcm if codec=='pcm' else encode_only(pcm,codec)
                            encoded=time.perf_counter()
                            result,host_s=conn.request_timed('transcribe',payload=blob,language='en',
                                                            experiment_codec=None if codec=='pcm' else codec)
                            end=time.perf_counter()
                            rows.append(dict(rtt_ms=rtt,mbps=mbps,seconds=seconds,codec=codec,rep=rep,
                                             pcm_bytes=len(pcm),wire_payload_bytes=len(blob),
                                             encode_ms=(encoded-start)*1000,request_ms=(end-encoded)*1000,
                                             release_to_result_ms=(end-start)*1000,host_ms=host_s*1000))
                        subset=rows[-bench.args.reps:]
                        print(json.dumps(dict(rtt_ms=rtt,mbps=mbps,seconds=seconds,codec=codec,
                                              p50_ms=float(np.median([x['release_to_result_ms'] for x in subset])))),flush=True)
                        bench.args.output.write_text(json.dumps(dict(native=NATIVE,source_revision=bench.args.source_revision,audio_path=str(bench.args.audio),audio_sha256=bench.args.audio_sha256,started_utc=started_utc,checkpoint_utc=datetime.now(timezone.utc).isoformat(),note=__doc__,rows=rows),indent=2))
                conn.close()
        finally:
            if conn:conn.close()
            host.stop()

if __name__=='__main__':main()
