"""Correctness-only mixed clip routing probe, real TLS and real CPU inference."""
import os
os.environ['ORT_DISABLE_TELEMETRY']='1'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['DO_NOT_TRACK']='1'
import argparse,json,threading,time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from real_transport import ROOT,audio_file,CrossRequestBatchEngine,BenchmarkHost,default_args
from native_tls import NativeEngine
from experiment_paths import PATHS,add_paths,required
from services.remote_asr import host as host_module

def main():
 p=add_paths(argparse.ArgumentParser());p.add_argument('--family',choices=['whisper','parakeet'],required=True);p.add_argument('--output',required=True);a=p.parse_args()
 full=audio_file(required(PATHS.audio_file,'--audio-file'))
 clips=[full[:4*16000],full[4*16000:8*16000],full[7*16000:],full]
 if a.family=='whisper':
  from faster_whisper import WhisperModel
  model=WhisperModel(str(required(PATHS.whisper_model,'--whisper-model')),device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
  engine=CrossRequestBatchEngine(model,full,4,5)
 else:engine=NativeEngine(True,5,False)
 original=host_module.SpeechHost
 def host(**kwargs):return original(**kwargs,max_active_jobs=4)
 rows=[]
 try:
  with patch.object(BenchmarkHost,'_make_engine',lambda fixture:engine),patch.object(host_module,'SpeechHost',host):
   with BenchmarkHost(default_args(clients=4,engine='real',audio_file=required(PATHS.audio_file,'--audio-file'))) as fixture:
    clients=[fixture.new_client(i) for i in range(4)]
    expected=[clients[i].request_timed('transcribe',audio=clip,language='en',timeout=120)[0]['text'] for i,clip in enumerate(clips)]
    for repetition in range(2):
     barrier=threading.Barrier(4)
     def call(i):
      barrier.wait()
      result,_=clients[i].request_timed('transcribe',audio=clips[i],language='en',timeout=120)
      return dict(repetition=repetition,client=i,audio_s=len(clips[i])/16000,expected=expected[i],actual=result['text'],matches=result['text']==expected[i])
     with ThreadPoolExecutor(4) as pool:rows.extend(pool.map(call,range(4)))
  result=dict(family=a.family,scope='Mixed-length cropped public speech, independent callers, real TLS; correctness only, not semantic leakage proof',rows=rows,matches=sum(x['matches'] for x in rows),total=len(rows))
  Path(a.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
 finally:engine.close()
if __name__=='__main__':main()
