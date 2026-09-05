"""Summarize completed live-preview runs while retaining groups and exact counts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.live_preview import aggregate


def summarize(inputs):
    reports = [json.loads(path.read_text(encoding='utf-8-sig')) for path in inputs]
    base = reports[0]
    for report in reports[1:]:
        for field in ('manifest_sha256', 'chunk_s', 'overlap_s', 'native_cadence_s', 'normalization', 'decoding', 'corpus', 'source_sha256'):
            if report[field] != base[field]:
                raise ValueError(f'Cannot pool runs with different {field}')
    summary = {key: value for key, value in base.items() if key not in ('results', 'git_status')}
    summary['raw_reports'] = [dict(file=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in inputs]
    summary['results'], summary['failures'] = [], []
    pools = {}
    for report in reports:
        for row in report['results']:
            if 'error' in row:
                summary['failures'].append(dict(model=row['model'], error=row['error']))
            for profile in row['profiles']:
                if 'summary' not in profile:
                    continue
                key = (row['model'], row['actual_device'], profile['mode'])
                pool = pools.setdefault(key, dict(clips=[], rows=[], profiles=[]))
                pool['clips'].extend(profile['clips'])
                pool['rows'].append(row)
                pool['profiles'].append(profile)
    for (model, device, mode), pool in pools.items():
        clips, rows, profiles = pool['clips'], pool['rows'], pool['profiles']
        cadence = profiles[0]['effective_cadence_s']
        result = dict(model=model, device=device, mode=mode, passes=len(profiles),
                      effective_cadence_s=cadence, summary=aggregate(clips, cadence),
                      load_s=[r['load_s'] for r in rows], warmup_s=[p['warmup_s'] for p in profiles],
                      hardware_before=[r['hardware_before'] for r in rows],
                      groups={g: aggregate([c for c in clips if c['group'] == g], cadence)
                              for g in sorted({c['group'] for c in clips})},
                      per_pass=[p['summary'] for p in profiles])
        for field in ('artifacts', 'model_snapshot', 'runtime_manifest', 'device_info'):
            if field in rows[0]:
                result[field] = rows[0][field]
        result['clip_scores'] = [dict(id=c['id'], group=c['group'], live=c['live'], drained=c['drained'],
                                     first_text_before_stop_s=c['first_text_before_stop_s']) for c in clips]
        summary['results'].append(result)
    return summary


def percent(value):
    return f'{100*value:.1f}%' if value is not None else 'n/a'


def number(value, decimals=0):
    return f'{value:.{decimals}f}' if value is not None else 'none'


def markdown(summary):
    lines = ['# Live preview measured results', '',
             'Timing is measured service plus a simulated serial arrival clock; it excludes UI paint and recorder queue drops.', '',
             '| Model / path | Device | Update p50 / p95 (ms) | First text (s) | Live WER | Drained WER | RTF |',
             '|---|---|---:|---:|---:|---:|---:|']
    for row in summary['results']:
        s = row['summary']
        lines.append(f"| {row['model']} / {row['mode']} | {row['device']} | {number(s['update_median_ms'])} / {number(s['update_p95_ms'])} | {number(s['first_text_median_s'], 2)} | {percent(s['live']['wer'])} | {percent(s['drained']['wer'])} | {s['rtf']:.3f} |")
    lines.extend(['', 'Live WER includes unshown tails at EOF; drained WER includes diagnostic final flush. Lower is better.', '',
                  '| Model / path | Clean live / drained WER | Noisy live / drained WER | AMI live / drained WER | AMI checkpoint WER |',
                  '|---|---:|---:|---:|---:|'])
    for row in summary['results']:
        groups = row['groups']
        cells = [f"{percent(groups[g]['live']['wer'])} / {percent(groups[g]['drained']['wer'])}"
                 if g in groups else 'n/a' for g in ('clean', 'noise10db', 'ami_conversation')]
        lines.append(f"| {row['model']} / {row['mode']} | {' | '.join(cells)} | {percent(groups.get('ami_conversation', {}).get('checkpoint_wer'))} |")
    lines.extend(['', 'Checkpoint WER repeatedly scores timed prefixes during the AMI excerpts; it is a lag-sensitive diagnostic, not ordinary corpus WER.', '',
                  '| Model / path | Passes | Updates slower than cadence | Clips with no live text | Silence insertions (live / drained) |',
                  '|---|---:|---:|---:|---:|'])
    for row in summary['results']:
        s, silence = row['summary'], row['groups'].get('silence')
        ins = f"{silence['live']['insertions']} / {silence['drained']['insertions']}" if silence else 'n/a'
        lines.append(f"| {row['model']} / {row['mode']} | {row['passes']} | {s['update_deadline_misses']} / {s['calls']} | {s['clips_without_live_text']} / {s['clips']} | {ins} |")
    if summary['failures']:
        lines.extend(['', 'Failures: '+json.dumps(summary['failures'])])
    return '\n'.join(lines)+'\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    args.output.with_suffix('.md').write_text(markdown(result), encoding='utf-8')
    print(markdown(result))

