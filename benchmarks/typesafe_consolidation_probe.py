"""Exercise each actual end-of-meeting consolidation path on one synthetic meeting."""
import argparse
import json
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from benchmarks.meeting_mode.product_eval import ProductEvalHost
from benchmarks.typesafe_hybrid import MEETING
from meeting.agent.base import create_agent_core, find_provider_api_key
from meeting.agent.prompts import build_system_prompt
from meeting.interfaces import AgentConfig, CheckpointPayload
from services.components import meeting_agent_payload_dir
from services.settings import resolve_meeting_llm_provider, resolve_meeting_llm_model, resolve_meeting_llm_endpoint


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness',choices=('direct','direct-json','pi','opencode'),required=True)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new output path')
    segments=[dict(id=sid,speaker=speaker,text=text,start_s=i*8,end_s=i*8+6,channel='mic') for i,(sid,speaker,text) in enumerate(MEETING)]
    host=ProductEvalHost('synthetic_typesafe_consolidation',segments)
    host.allow_agent_writes()
    kind='direct' if args.harness=='direct-json' else args.harness
    payload_dir=meeting_agent_payload_dir(kind) if kind in ('pi','opencode') else None
    agent=create_agent_core(kind,payload_dir)
    model=resolve_meeting_llm_model()
    provider=resolve_meeting_llm_provider()
    report={'harness':args.harness,'actual_class':type(agent).__name__,'model':model,'provider':provider,'segments':segments}
    started=time.perf_counter()
    timer=None
    try:
        agent.initialize(AgentConfig(host.meeting_id,provider,model,find_provider_api_key(provider),build_system_prompt(),endpoint=resolve_meeting_llm_endpoint()),host)
        report['initialize_seconds']=time.perf_counter()-started
        if args.harness=='direct-json':agent._json_mode=True
        timer=threading.Timer(120,agent.cancel)
        timer.daemon=True
        timer.start()
        begin=time.perf_counter()
        result=agent.consolidate(CheckpointPayload('consolidation_probe',host.store.snapshot(),segments,is_consolidation=True))
        report.update(seconds=time.perf_counter()-begin,ok=result.ok,usage=result.usage,ops=[asdict(x) for x in result.op_results])
    except Exception as exc:
        report['error']=type(exc).__name__
    finally:
        if timer:timer.cancel()
        agent.shutdown()
    report['state']=host.store.snapshot()
    report['wall_seconds']=time.perf_counter()-started
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report.get(k) for k in ('harness','ok','seconds','wall_seconds','error')}))


if __name__=='__main__':main()
