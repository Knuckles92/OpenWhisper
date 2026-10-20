"""Plot retained process-memory prefixes; none is a completed shutdown test."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--old-fake',type=Path,required=True)
parser.add_argument('--fake',type=Path,required=True)
parser.add_argument('--native',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
fig,axes=plt.subplots(1,3,figsize=(14,5))
for ax,path,title in zip(axes,[args.old_fake,args.fake,args.native],['Earlier simulated host','Scratch simulated host','Native CPU host + worker']):
 rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
 rows=[r for r in rows if r.get('host_tree')]
 x=[r['at_s']/60 for r in rows];y=[r['host_tree']['rss_bytes']/1024**2 for r in rows]
 ax.plot(x,y,color='#245A81',linewidth=1.5)
 ax.axvline(30,color='#888888',linestyle=':',linewidth=1)
 ax.set(title=f'{title}\n{x[-1]:.2f} minutes observed',xlabel='Elapsed minutes',ylabel='RSS (MiB)',xlim=(0,40))
 ax.grid(alpha=.2);ax.spines[['top','right']].set_visible(False)
fig.suptitle('Interrupted long-run resource observations, not completed soak passes',fontsize=16,x=.07,ha='left')
fig.text(.07,.04,'Per-panel memory scales differ. Shared-machine workloads; no final quiescence/shutdown evidence. Dotted line: 30 minutes.',fontsize=10)
fig.subplots_adjust(left=.07,right=.98,bottom=.19,top=.76,wspace=.3)
fig.savefig(args.output,dpi=150)
