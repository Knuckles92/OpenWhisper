"""Aggregate saved TypeSafe real-meeting experiments without making API calls."""
from collections import Counter
import hashlib
import json
import math
import statistics
from benchmarks.meeting_mode.typesafe_real_eval import ANNOTATIONS, DEST, MEETINGS, ROOT, save


def read(name):
    return json.loads((DEST / name).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_counts(name):
    data = read(name)
    counts = {}
    for case in data['cases']:
        verdict = data['labels'].get(case['id'], {}).get('verdict', 'missing')
        counts.setdefault(case['arm'], Counter())[verdict] += 1
    return {'judge': data['judge_model'], 'protocol': data['protocol'], 'counts': counts,
            'cases': len(data['cases']), 'labeled': len(data['labels'])}


def main():
    signals = read('signals.json')
    report = {'date': '2026-09-18', 'scope': 'Public AMI cached ASR; production consolidation from empty state; excludes microphone, ASR runtime, live scheduler and UI.',
              'limitations': ['Three research meetings; one run per writer/arm.',
                              'Reference transcripts are human annotations; most evaluation labels are model judgments, not human gold.',
                              'Gemini judges its own writer family; no model-quality ranking is established.',
                              'Whole-record coverage audits failed; completeness is unmeasured.',
                              'Production cancellations preserve partial state and are not completed results.',
                              'Seeded ledger cases were selected during development, not held out.',
                              'Thin writers produce common record fields, not every production-agent artifact.'],
              'corpus': {'name': 'AMI Meeting Corpus', 'url': 'https://groups.inf.ed.ac.uk/ami/corpus/',
                         'license': 'CC BY 4.0', 'annotation_release': '1.7 (local README)',
                         'annotation_files': []}, 'meetings': []}
    reference_files = [ANNOTATIONS/'00README_MANUAL.txt', ANNOTATIONS/'LICENCE.txt']
    for mid in MEETINGS:
        reference_files.extend(sorted((ANNOTATIONS/'words').glob(mid+'.*.words.xml')))
        meeting = signals['meetings'][mid]
        report['meetings'].append({'id': mid, 'minutes': meeting['duration_s']/60, 'asr_wer': meeting['asr_wer'],
                                   'source': meeting['source'], 'signals': meeting['summary']})
    report['corpus']['annotation_files'] = [{'path':str(p.relative_to(ROOT)), 'sha256':sha(p)} for p in reference_files]
    runs = [r for mid in MEETINGS for r in signals['meetings'][mid]['runs']]
    latencies = sorted(r['seconds'] for r in runs)
    report['signals'] = {'model': signals['model'], 'calls': len(runs), 'questions': len(runs)*4,
                         'errors': sum('error' in r for r in runs), 'concurrency':4,
                         'threshold':.8, 'lookback_seconds':90, 'max_prior_segments':20,
                         'median_request_seconds':statistics.median(latencies),
                         'p95_request_seconds_nearest_rank':latencies[math.ceil(.95*len(latencies))-1],
                         'total_audio_minutes':sum(signals['meetings'][m]['duration_s']/60 for m in MEETINGS),
                         'selected_segments':sum(signals['meetings'][m]['summary']['selected_segments'] for m in MEETINGS),
                         'input_tokens':sum(r.get('response',{}).get('usage',{}).get('input_tokens',0) for r in runs)}
    report['signals']['estimated_cost_usd'] = report['signals']['input_tokens']*.042/1_000_000
    audit = read('signal-reference-audit-isolated.json')
    report['signal_reference_audit'] = {'judge':audit['judge_model'], 'protocol':audit['protocol'],
                                       'labeled':len(audit['labels']), 'samples':{},
                                       'known_ambiguity':'IN1005:388 judge calls a conditional link offer accepted; manual text review and seeded probe retain proposed. Raw labels unchanged.'}
    for name in ('uniform','positive','challenging'):
        cases = [c for c in audit['cases'] if c['sample'][name]]
        labels = [audit['labels'].get(c['id'],{}) for c in cases]
        report['signal_reference_audit']['samples'][name] = {
            'cases':len(cases), 'commitments':Counter(l.get('commitment','missing') for l in labels),
            'positive_labels':{k:sum(bool(l.get(k)) for l in labels) for k in ('assumption_broken','actionable','decision')}}
    report['signal_reference_audit']['sample_note'] = 'Samples overlap; uniform has 60 cases, enriched union totals 101. Do not infer population recall from enriched cases.'
    report['package_audits'] = [audit_counts('package-reference-audit-production.json'), audit_counts('package-reference-audit-gemini.json')]
    report['packages'] = []
    observed_llm_cost = 0
    check_rows = []
    for mid in MEETINGS:
        for arm in ('baseline','assisted','thin','thin-assisted','thin-gemini','thin-gemini-assisted'):
            name = f'package-{mid}-{arm}.json'
            d = read(name)
            row = {k:d.get(k) for k in ('meeting','arm','provider','model','ok','error','seconds','draft_seconds','invalid_evidence_items')}
            row['artifact'] = name
            row['items_in_saved_common_fields'] = sum(len([i for i in rows if i.get('status')!='removed']) for key,rows in d.get('state',{}).get('cards',{}).items() if key in ('key_points','decisions','action_items','risks')) if 'state' in d else None
            row['record_complete'] = bool(d.get('ok'))
            row['stage_reported_llm_cost_usd'] = {}
            for stage in ('generation','repair'):
                cost = d.get(stage,{}).get('usage',{}).get('cost')
                if isinstance(cost,(int,float)):
                    row['stage_reported_llm_cost_usd'][stage] = cost
                    observed_llm_cost += cost
            for stage in ('verification','reverification'):
                checks = d.get(stage,{}).get('checks',[])
                if stage in d:
                    row[stage] = {'items':len(checks),'flagged':sum(c['needs_review'] for c in checks),
                                  'seconds':d[stage]['wall_seconds'], 'errors':sum('error' in c for c in checks)}
                    check_rows.extend(checks)
            report['packages'].append(row)
    ledger = read('ledger-probe.json')
    report['ledger'] = {'protocol':ledger['protocol'],'cases':[]}
    for result in ledger['results']:
        answer = result.get('response',{}).get('answers',{}).get('transition',{})
        report['ledger']['cases'].append({'id':result['case']['id'],'expected':result['case']['expected'],
                                         'choice':answer.get('choice'),'confidence':answer.get('confidence'), 'seconds':result['seconds']})
    report['ledger']['matched'] = sum(c['choice']==c['expected'] for c in report['ledger']['cases'])
    report['ledger']['confidence_at_least_08'] = sum((c['confidence'] or 0)>=.8 for c in report['ledger']['cases'])
    extra_tokens = sum(r.get('response',{}).get('usage',{}).get('input_tokens',0) for r in check_rows+ledger['results'])
    report['costs'] = {'signals_estimated_usd':report['signals']['estimated_cost_usd'],
                       'saved_verification_and_ledger_estimated_usd':extra_tokens*.042/1_000_000,
                       'saved_writer_stages_reported_usd':observed_llm_cost,
                       'note':'Component costs only, not invoice total. Excludes judges, lost/cancelled/failed responses and unreported production-agent charges; zero Pi cost metadata is not treated as free.'}
    coverage = read('coverage-audit-isolated.json')
    report['coverage_audit'] = {'status':'unscorable','attempted_records':len(coverage['runs']),
                               'failed_validation':sum('error' in r for r in coverage['runs']),
                               'note':'Earlier multi-record judge returned empty objects. Isolated retry failed validation on all 12 records. No completeness scores reported.'}
    save(DEST/'summary.json',report)
    save(ROOT/'docs/benchmarks/typesafe-real-meetings-2026-09-18.json',report)
    print(json.dumps({'signals':report['signals'],'costs':report['costs'],'ledger_matches':report['ledger']['matched'],
                      'coverage':report['coverage_audit']},indent=2))


if __name__ == '__main__':
    main()
