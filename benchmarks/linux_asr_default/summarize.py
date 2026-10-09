"""Aggregate repeated predictions without treating repeats as new audio."""
from collections import defaultdict
import json
from pathlib import Path
import statistics
import sys

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from benchmarks.meeting_mode.metrics import score_text, normalize_tokens

ROOT=Path(__file__).resolve().parent
CORE={'librispeech_clean','librispeech_other','ami_conversation'}
FILLERS={'um','uh','hmm','mm','uhh','umm','mhm'}
ONES=['zero','one','two','three','four','five','six','seven','eight','nine',
      'ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen']
TENS=['','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety']


def equivalent(text):
    result=[]
    for token in normalize_tokens(text):
        if token in FILLERS: continue
        if token.isdigit() and 0<=int(token)<100:
            number=int(token)
            result += ([ONES[number]] if number<20 else
                       [TENS[number//10]]+([ONES[number%10]] if number%10 else []))
        else: result.append(token)
    return ' '.join(result)


def totals(clips):
    result={key:sum(c['score'][key] for c in clips) for key in
            ['words','errors','substitutions','deletions','insertions']}
    result.update(clips=len(clips),audio_s=sum(c['duration_s'] for c in clips),
                  decode_s=sum(c['decode_s'] for c in clips))
    result['wer']=result['errors']/result['words'] if result['words'] else None
    result['rtf']=result['decode_s']/result['audio_s']
    return result


def summarize(path):
    report=json.loads(path.read_text())
    manifest=json.loads((ROOT/'corpus/manifest.json').read_text())
    inputs={c['id']:c for c in manifest['clips']}
    profiles={}
    for profile in sorted({r['profile'] for r in report['runs']}):
        runs=[r for r in report['runs'] if r['profile']==profile]
        successful=[r for r in runs if 'error' not in r]
        row=dict(runs=len(runs),failures=[r['error'] for r in runs if 'error' in r])
        if not successful:
            profiles[profile]=row
            continue
        first=successful[0]
        row.update({k:first.get(k) for k in ['actual_device','model','compute_type','runtime_component']})
        row['all_timing_s']=[r['decode_s'] for r in successful]
        row['all_timing_median_s']=statistics.median(row['all_timing_s'])
        row['load_median_s']=statistics.median(r['load_s'] for r in successful)
        row['first_decode_median_s']=statistics.median(r['first_decode_s'] for r in successful)
        row['rss_peak_mib']=max(r['resources']['process_tree_rss_bytes']/1024**2 for r in successful)
        row['gpu_peak_total_mib']=max(r['resources']['gpu_total_used_mib'] for r in successful)
        row['gpu_baseline_mib']=first['environment_before']['gpu_sample'][0]
        row['core']=totals([c for c in first['clips'] if c['group'] in CORE])
        row['core']['timing_runs_s']=[sum(c['decode_s'] for c in r['clips'] if c['group'] in CORE) for r in successful]
        row['core']['timing_median_s']=statistics.median(row['core']['timing_runs_s'])
        row['groups']={}
        for group in first['groups']:
            records=[r['groups'][group] for r in successful]
            total=dict(records[0])
            total['timing_runs_s']=[g['decode_s'] for g in records]
            total['timing_median_s']=statistics.median(total['timing_runs_s'])
            total['wer_by_pass']=[g['wer'] for g in records]
            row['groups'][group]=total
        changed=[]
        initial={c['id']:c for c in first['clips']}
        for identifier in initial:
            texts={next(c['transcript'] for c in r['clips'] if c['id']==identifier) for r in successful}
            if len(texts)>1: changed.append(identifier)
        row['changed_transcripts_between_passes']=changed
        row['duration_bins']={}
        for name,low,high in [('under_10s',0,10),('10_to_30s',10,30.001),('over_30s',30.001,float('inf'))]:
            latencies=[statistics.median(next(c['decode_s'] for c in r['clips'] if c['id']==item['id']) for r in successful)
                       for item in first['clips'] if low<=item['duration_s']<high and item['group'] in CORE]
            row['duration_bins'][name]=dict(clips=len(latencies),median_s=statistics.median(latencies) if latencies else None,
                                           p95_s=float(np.percentile(latencies,95)) if latencies else None)
        transformed=[]
        for c in first['clips']:
            if c['group']!='ami_conversation': continue
            transformed.append(score_text(equivalent(inputs[c['id']]['reference']),equivalent(c['transcript'])))
        words=sum(s['words'] for s in transformed)
        errors=sum(s['errors'] for s in transformed)
        row['ami_without_fillers_with_small_number_equivalence']=dict(words=words,errors=errors,wer=errors/words)
        profiles[profile]=row

    comparisons={}
    rng=np.random.default_rng(20261009)
    for baseline,candidate in [('whisper_base_cpu','parakeet_cpu'),('whisper_turbo_gpu','parakeet_gpu')]:
        a=next((r for r in report['runs'] if r['profile']==baseline and 'error' not in r),None)
        b=next((r for r in report['runs'] if r['profile']==candidate and 'error' not in r),None)
        if not a or not b: continue
        ac={c['id']:c for c in a['clips']}; bc={c['id']:c for c in b['clips']}
        clusters=defaultdict(lambda:np.zeros(3))
        for identifier,source in inputs.items():
            if source['group'] not in CORE: continue
            cluster=('speaker',source['speaker_id']) if 'speaker_id' in source else ('meeting',identifier)
            clusters[cluster] += np.array([ac[identifier]['score']['words'],ac[identifier]['score']['errors'],bc[identifier]['score']['errors']])
        values=np.array(list(clusters.values()))
        draws=values[rng.integers(0,len(values),size=(10000,len(values)))].sum(axis=1)
        differences=(draws[:,2]-draws[:,1])/draws[:,0]
        comparisons[candidate+'_vs_'+baseline]=dict(cluster_count=len(values),bootstrap_replicates=10000,
            candidate_minus_baseline_wer=profiles[candidate]['core']['wer']-profiles[baseline]['core']['wer'],
            wer_difference_95pct_cluster_bootstrap=list(np.percentile(differences,[2.5,97.5])),
            warm_core_speedup=profiles[baseline]['core']['timing_median_s']/profiles[candidate]['core']['timing_median_s'])
    summary=dict(profiles=profiles,comparisons=comparisons,
        accuracy='Counts from first completed pass; repeated output variation reported separately',
        timing='Median of three complete warm passes; duration bins use median repeat latency per independent clip',
        bootstrap='Paired resampling by LibriSpeech speaker or AMI meeting; English corpus only',
        scoring_sensitivity='AMI-only: um/uh/hmm/mm/uhh/umm/mhm removed; digit tokens 0..99 expanded to English words; diagnostic sensitivity, not the primary metric')
    (ROOT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    summarize(ROOT/(sys.argv[1] if len(sys.argv)>1 else 'comparison.json'))
