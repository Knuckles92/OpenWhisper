"""Seeded commitment tracking using only publicly released AMI corpus excerpts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
import httpx
from dotenv import dotenv_values
from benchmarks.meeting_mode.typesafe_real_eval import DEST, ROOT, save
from benchmarks.typesafe_experiments import ENDPOINT, MODEL

SPECS = [
    ('paper_offer','IN1009',[112],'Share the discussed research papers or links.','absent',[],'proposed'),
    ('paper_acceptance','IN1009',[113,114],'Share the discussed research papers or links.','proposed',[112],'accepted'),
    ('paper_repeat','IN1009',[150],'Send the discussed research paper links to the visitors.','accepted',[113,114],'unchanged'),
    ('conditional_offer','IN1005',[388],'Send the correlated topic model link to the group.','absent',[],'proposed'),
    ('math_not_task','IN1005',[35],'Calculate PageRank of Z after the meeting.','absent',[],'none'),
    ('paper_handoff','IN1007',[243],'Send the related papers to the colleague.','absent',[],'accepted'),
    ('code_handoff','IN1007',[244,245],'Provide access or locations for the discussed source code.','absent',[],'accepted'),
]
QUESTION = {'type':'choice','instructions':
    'Update only `tracked_task` from `previous_status`, `memory_evidence`, `prior_context` and `new_speech`. '
    'The task name is a candidate to test, not evidence that a task exists. Use the source evidence behind memory. '
    'First-person acceptance may have an unnamed owner. A conditional offer remains proposed unless accepted. '
    'Repeating an already accepted promise is unchanged even if it was many minutes ago. '
    'Separate a mathematical explanation or live demonstration from a concrete future work commitment. '
    'Use none when an absent candidate task is not established. Do not infer names or deadlines.',
    'criteria':{'none':'No concrete task proposal or acceptance for this absent candidate.',
                'proposed':'New conditional offer or unaccepted task suggestion.',
                'accepted':'Previously absent/proposed task is now explicitly committed to or accepted.',
                'changed':'An accepted task has a changed owner, scope, condition or deadline.',
                'withdrawn':'An earlier task is explicitly cancelled or rejected.',
                'unchanged':'Prior status holds; repetition or unrelated speech does not create a new task.'}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.parse_args()
    path=DEST/'ledger-probe.json'
    if path.exists():raise ValueError('Result already exists')
    key=os.environ.get('TYPESAFE_API_KEY') or dotenv_values(ROOT/'.env').get('TYPESAFE_API_KEY')
    if not key:raise ValueError('TypeSafe key unavailable')
    signals=json.loads((DEST/'signals.json').read_text())
    rows=[]
    for name,mid,focus,task,status,memory,expected in SPECS:
        runs=signals['meetings'][mid]['runs']
        state={'tracked_task':task,'previous_status':status,
               'memory_evidence':[runs[i]['request']['state']['focus'] for i in memory],
               'prior_context':runs[focus[0]]['request']['state']['prior_context'],
               'new_speech':[runs[i]['request']['state']['focus'] for i in focus]}
        rows.append({'id':name,'meeting':mid,'state':state,'expected':expected})
    started=time.perf_counter()
    with httpx.Client(headers={'Authorization':'Bearer '+key},timeout=30,follow_redirects=False) as client:
        def call(row):
            payload={'model':MODEL,'state':row['state'],'questions':{'transition':QUESTION}}
            result={'case':row,'request':payload}
            begin=time.perf_counter()
            try:
                reply=client.post(ENDPOINT,json=payload)
                reply.raise_for_status()
                result['response']=reply.json()
            except Exception as exc:result['error']=type(exc).__name__
            result['seconds']=time.perf_counter()-begin
            return result
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(call,rows))
    save(path,{'provenance':'Public AMI corpus IN1009/IN1005/IN1007; CC BY 4.0; no private meetings.',
               'protocol':'Seven hand-selected actual-ASR episodes with manually seeded task candidates and memory. Development probe, not automatic discovery or held-out accuracy. Labels excluded from requests.',
               'results':results,'wall_seconds':time.perf_counter()-started})
    print(json.dumps([{'case':r['case']['id'],'expected':r['case']['expected'],'answer':r.get('response',{}).get('answers',{}),'seconds':r['seconds']} for r in results],indent=2))


if __name__=='__main__':main()
