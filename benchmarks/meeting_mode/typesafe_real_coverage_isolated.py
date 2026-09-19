"""One-record-at-a-time full-reference coverage audit of public AMI meetings."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import random
from benchmarks.meeting_mode.typesafe_real_eval import DEST, MEETINGS, ROOT, reference_segments, save
from benchmarks.meeting_mode.typesafe_real_audit import chat, schema
from services.text_llm import get_profile


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.parse_args()
    path=DEST/'coverage-audit-isolated.json'
    if path.exists():raise ValueError('Result already exists')
    settings=json.loads((ROOT/'openwhisper_settings.json').read_text(encoding='utf-8'))
    profile=get_profile(settings['transcript_cleanup_provider'],settings)
    model=settings['transcript_cleanup_model']
    grade={'type':'integer','enum':[1,2,3,4,5]}
    shape=schema({'coverage':grade,'fidelity':grade,'commitment_precision':grade,
                  'missing_important_content':{'type':'array','items':{'type':'string'}},
                  'unsupported_or_overstated':{'type':'array','items':{'type':'string'}},'reason':{'type':'string'}})
    system=('Evaluate one meeting record against the FULL HUMAN REFERENCE. Rate coverage of major substantive topics and outcomes, '
            'factual fidelity, and commitment precision from 1 (poor) to 5 (excellent). Do not reward length or polished style. '
            'Suggestions and conditional offers must not become accepted tasks. Empty actions are correct when none were accepted. '
            'Evaluate the common topic, summary, key_points, decisions, actions and risks fields only. '
            'List at most three major omissions and three unsupported claims using only reference evidence. Names must be grounded in context. '
            'Return exactly JSON with these fields: {"coverage":1,"fidelity":1,"commitment_precision":1, '
            '"missing_important_content":[],"unsupported_or_overstated":[],"reason":"explain using source content"}. '
            'Fill in your actual ratings. All fields are required.')
    def call(job):
        mid,arm=job
        d=json.loads((DEST/f'package-{mid}-{arm}.json').read_text(encoding='utf-8'))
        s=d['state']
        record={'topic':s.get('topic',{}).get('current',''),'summary':s.get('rolling_summary',''),
                'cards':{k:[i['text'] for i in s['cards'].get(k,[]) if i.get('status')!='removed'] for k in ('key_points','decisions','action_items','risks')}}
        result=chat(profile,model,settings.get('transcript_cleanup_reasoning','off'),[
            {'role':'system','content':system},{'role':'user','content':json.dumps({'human_reference':reference_segments(mid),'record':record})}],shape)
        return {'meeting':mid,'arm':arm,**result}
    jobs=[(mid,arm) for mid in MEETINGS for arm in ('baseline','assisted','thin-gemini','thin-gemini-assisted')]
    random.Random(20260918).shuffle(jobs)
    report={'judge_model':model,'protocol':'One record per request; arm/model hidden; full human reference; common fields only. Same judge model as Gemini writers; subjective ratings, not human ground truth.', 'runs':[]}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(call,job):job for job in jobs}
        for future in as_completed(futures):
            try:report['runs'].append(future.result())
            except Exception as exc:report['runs'].append({'job':futures[future],'error':type(exc).__name__})
            save(path,report)
            print(json.dumps({'coverage_completed':len(report['runs']),'total':12}),flush=True)


if __name__=='__main__':main()
