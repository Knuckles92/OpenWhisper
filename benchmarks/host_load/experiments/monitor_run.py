"""Resource sampler for a bounded subprocess experiment (all processes are ours)."""
import argparse,json,subprocess,time
from pathlib import Path
import psutil
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--timeout',type=float,default=100);p.add_argument('command',nargs=argparse.REMAINDER);a=p.parse_args()
command=a.command[1:] if a.command[0]=='--' else a.command
started=time.perf_counter();proc=subprocess.Popen(command);root=psutil.Process(proc.pid);samples=[];timed_out=False
while proc.poll() is None:
 now=time.perf_counter()
 if now-started>a.timeout:
  timed_out=True
  try:owned=root.children(recursive=True)+[root]
  except psutil.NoSuchProcess:owned=[]
  for process in owned:
   try:process.terminate()
   except psutil.NoSuchProcess:pass
  _,alive=psutil.wait_procs(owned,timeout=5)
  for process in alive:
   try:process.kill()
   except psutil.NoSuchProcess:pass
  proc.wait()
  break
 try:
  processes=[root]+root.children(recursive=True)
  samples.append(dict(at_s=now-started,threads=sum(x.num_threads() for x in processes),rss=sum(x.memory_info().rss for x in processes),cpu_seconds=sum(sum(x.cpu_times()[:2]) for x in processes)))
 except (psutil.NoSuchProcess,psutil.AccessDenied):pass
 time.sleep(.05)
result=dict(command=command,exit_code=proc.wait(),timed_out=timed_out,wall_s=time.perf_counter()-started,sampling_seconds=.05,
 scope='Complete experiment process tree; same-process TLS host and clients are included, unlike main harness host-only resource metric',samples=samples)
Path(a.output).write_text(json.dumps(result,indent=2)+'\n')

raise SystemExit(124 if timed_out else result['exit_code'])
