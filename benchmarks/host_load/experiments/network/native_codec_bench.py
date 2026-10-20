import hashlib,json,time,wave
from pathlib import Path
import argparse
from options import add_common, configure
parser=argparse.ArgumentParser(description="In-process FLAC/Opus size and cost research")
add_common(parser,host=False)
parser.add_argument('--reps',type=int,default=10)
args=configure(parser,parser.parse_args(),host=False)
import numpy as np
from native_codecs import flac_encode,flac_decode,opus_encode,opus_decode
ROOT=Path(__file__).parent
with wave.open(str(args.audio)) as f:source=np.frombuffer(f.readframes(f.getnframes()),'<i2')
rows=[]
rng=np.random.default_rng(4729)
for seconds in (3,11,30,90):
 for snr in (None,20,10):
  a=np.resize(source,16000*seconds).astype(np.float64)
  if snr is not None:a+=rng.normal(0,np.sqrt(np.mean(a*a))/10**(snr/20),len(a))
  pcm=np.clip(np.rint(a),-32768,32767).astype('<i2').tobytes()
  for codec,encoder,decoder in [('flac',flac_encode,flac_decode),('opus_24_c5',opus_encode,opus_decode)]:
   for rep in range(args.reps):
    start=time.perf_counter();blob=encoder(pcm);encoded=time.perf_counter();decoded=decoder(blob);done=time.perf_counter()
    if codec=='flac':assert decoded==pcm
    rows.append(dict(seconds=seconds,snr_db=snr,codec=codec,rep=rep,pcm_bytes=len(pcm),encoded_bytes=len(blob),ratio=len(blob)/len(pcm),encode_ms=(encoded-start)*1000,decode_ms=(done-encoded)*1000,exact_pcm_roundtrip=pcm==decoded))
   print(json.dumps(rows[-1]),flush=True)
   args.output.write_text(json.dumps(dict(source=str(args.audio),source_sha256=args.audio_sha256,note='In-process native codecs. Supplied speech repeated+deterministic white noise. Opus24kbit/s complexity5,20ms packets plus2-byte framing. No recognition accuracy measured.',rows=rows),indent=2))
