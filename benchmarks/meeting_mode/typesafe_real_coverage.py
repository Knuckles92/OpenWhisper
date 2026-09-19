"""Blinded full-reference coverage ratings for the common meeting-record fields."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import random
from benchmarks.meeting_mode.typesafe_real_eval import DEST, MEETINGS, ROOT, reference_segments, save
from benchmarks.meeting_mode.typesafe_real_audit import chat, schema
from services.text_llm import get_profile

ARMS=('baseline','assisted','thin-gemini','thin-gemini-assisted')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.parse_args()
    path=DEST/'coverage-audit.json'
    if path.exists():raise ValueError('Result already exists')
    settings=json.loads((ROOT/'openwhisper_settings.json').read_text(encoding='utf-8'))
    profile=get_profile(settings['transcript_cleanup_provider'],settings)
    model=settings['transcript_cleanup_model']
    grade={'type':'integer','enum':[1,2,3,4,5]}
    label=schema({'coverage':grade,'fidelity':grade,'commitment_precision':grade,
                  'missing_important_content':{'type':'array','items':{'type':'string'}},
                  'unsupported_or_overstated':{'type':'array','items':{'type':'string'}},
                  'reason':{'type':'string'}})
    system=('Compare four anonymized meeting records against the full HUMAN REFERENCE. Evaluate only their common topic, summary, '
            'key_points, decisions, action_items and risks fields. Do not infer what might be in omitted UI fields. '
            'Rate each independently from 1 (poor) to 5 (excellent): coverage of major substantive topics/outcomes; factual fidelity; '
            'precision of commitments (suggestions and conditional offers must not become accepted tasks). '
            'Do not reward word count, polished prose, or extra trivial items. Sparse action items are correct when actual commitments are sparse. '
            'Distinguish lost source detail from citation-window limitations: you have the FULL human reference. '
            'List at most three important omissions and at most three substantive unsupported/overstated claims per record, '
            'based only on reference evidence. Names may be known from conversation context, but must not be invented. '
            'The order is randomized. Judge only the supplied common fields, without guessing model or method.')
    def call(job):
        mid,repeat=job
        ordered=list(ARMS)
        random.Random(mid+':coverage').shuffle(ordered)
        if repeat:ordered.reverse()
        mapping={chr(65+i):arm for i,arm in enumerate(ordered)}
        packages={}
        for label_name,arm in mapping.items():
            d=json.loads((DEST/f'package-{mid}-{arm}.json').read_text(encoding='utf-8'))
            s=d['state']
            packages[label_name]={'topic':s.get('topic',{}).get('current',''),'summary':s.get('rolling_summary',''),
                'cards':{k:[i['text'] for i in s['cards'].get(k,[]) if i.get('status')!='removed'] for k in ('key_points','decisions','action_items','risks')}}
        result=chat(profile,model,settings.get('transcript_cleanup_reasoning','off'),[
            {'role':'system','content':system},
            {'role':'user','content':json.dumps({'human_reference':reference_segments(mid),'records':packages})}],
            schema({'ratings':schema({k:label for k in mapping})}))
        return {'meeting':mid,'repetition':repeat,'mapping':mapping,**result}
    report={'protocol':'Full human reference, common fields only, same judge model as Gemini writers; two reversed-order repetitions. Subjective model ratings, not human ground truth or full-UI equivalence.',
            'judge_model':model,'runs':[]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(call,(mid,rep)) for mid in MEETINGS for rep in (0,1)]
        for future in as_completed(futures):
            try:report['runs'].append(future.result())
            except Exception as exc:report['runs'].append({'error':type(exc).__name__})
            save(path,report)
            print(json.dumps({'coverage_completed':len(report['runs']),'total':6}),flush=True)


if __name__=='__main__':main()
