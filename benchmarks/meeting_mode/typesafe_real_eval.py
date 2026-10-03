"""Real AMI meeting probes: streaming judgments and paired production-agent packages."""

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import threading
import time

import httpx
from dotenv import dotenv_values

from benchmarks.meeting_mode.ami import parse_reference_words
from benchmarks.typesafe_experiments import ENDPOINT, MODEL, ROOT

MEETINGS = ('IN1009', 'IN1005', 'IN1007')
SOURCE = ROOT / 'benchmarks/meeting_mode/results/auto-auto-draft-only-t5-m20-p50-offline'
ANNOTATIONS = ROOT / 'benchmarks/meeting_mode/data/ami/annotations'
DEST = ROOT / 'benchmarks/meeting_mode/results/typesafe-real-20260918'
QUESTIONS = {
    'commitment': {'type': 'choice', 'instructions': 'What change in a concrete work commitment is established by `focus`, interpreted using only `prior_context`? First-person acceptance can establish a task even if the speaker name is unknown. Exclude descriptions of past completed work, generic explanations, hypothetical examples, and mere capability. Do not invent acceptance from a suggestion.',
                   'criteria': {'none': 'No new task proposal, acceptance, material revision or withdrawal.', 'proposed': 'A concrete future task is suggested or offered conditionally, without clear acceptance.', 'accepted': 'A person explicitly commits to a concrete future task, or an assignment is clearly accepted.', 'revised': 'An existing accepted task has a clearly changed owner, deadline, scope or condition.', 'withdrawn': 'An existing accepted task is explicitly cancelled or declined.'}},
    'assumption_broken': {'type': 'noul', 'instructions': 'Does `focus` introduce a fact that invalidates or materially weakens an assumption used for an earlier plan or conclusion in `prior_context`? Both the earlier assumption and the conflicting new fact must be identifiable in these segments. A routine elaboration, unrelated topic, or hypothetical counterexample without a committed plan is not enough.'},
    'actionable': {'type': 'noul', 'instructions': 'Does `focus`, together with `prior_context`, newly establish both an identifiable responsible speaker/person and a concrete accepted future task? A specific deadline is optional. Proposals, vague hopes, generic technical advice and completed historical work are not accepted tasks.'},
    'decision': {'type': 'noul', 'instructions': 'Does `focus` establish an actual new agreed choice about what the group will do, using `prior_context` for meaning? Distinguish an adopted choice from a suggestion, reported historical choice, factual correction or explanation.'},
}


def load_meeting(mid):
    path = SOURCE / f'{mid}.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    return data, {'path': str(path.relative_to(ROOT)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def signal_payload(segments, index):
    focus = segments[index]
    end = float(focus['end_s'])
    prior = [s for s in segments[:index] if float(s['end_s']) <= end and float(s['end_s']) >= float(focus['start_s']) - 90][-20:]
    return {'model': MODEL, 'state': {'focus': focus, 'prior_context': prior}, 'questions': deepcopy(QUESTIONS)}


def selected_signal(run):
    answers = run.get('response', {}).get('answers', {})
    if not answers:
        return False
    c = answers['commitment']
    return (c['choice'] != 'none' and c['confidence'] >= .8) or any(answers[k]['noul'] >= .8 for k in ('assumption_broken', 'actionable', 'decision'))


def advice(signals):
    return [{'segment_id': r['segment_id'], 'signals': {k: v.get('choice', v.get('noul')) for k, v in r['response']['answers'].items()}}
            for r in signals if selected_signal(r)]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(.05 * (attempt + 1))


async def signals(args):
    path = args.output or DEST / 'signals.json'
    if path.exists():
        raise ValueError('Choose a new output file')
    key = os.environ.get('TYPESAFE_API_KEY') or dotenv_values(ROOT / '.env').get('TYPESAFE_API_KEY')
    if not key:
        raise ValueError('TypeSafe key unavailable')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL, 'threshold': .8,
              'lookback_seconds': 90, 'max_prior_segments': 20, 'concurrency': 4, 'meetings': {}}
    semaphore = asyncio.Semaphore(4)
    start_lock = asyncio.Lock()
    last_start = 0.0
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + key}, timeout=30,
                                 follow_redirects=False, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client:
        for mid in args.meetings:
            data, provenance = load_meeting(mid)
            segments = sorted(data['draft_segments'], key=lambda s: (s['end_s'], s['id']))
            runs = []
            started = time.perf_counter()
            async def call(index):
                nonlocal last_start
                payload = signal_payload(segments, index)
                async with semaphore:
                    async with start_lock:
                        delay = .05 - (time.perf_counter() - last_start)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        last_start = time.perf_counter()
                    begin = time.perf_counter()
                    row = {'segment_id': segments[index]['id'], 'index': index, 'request': payload}
                    try:
                        response = await client.post(ENDPOINT, json=payload)
                        row['status'] = response.status_code
                        response.raise_for_status()
                        body = response.json()
                        if set(body['answers']) != set(QUESTIONS):
                            raise ValueError('Unexpected question IDs')
                        for name, question in QUESTIONS.items():
                            answer = body['answers'][name]
                            if answer['type'] != question['type']:
                                raise ValueError('Wrong type')
                            if name == 'commitment' and answer['choice'] not in question['criteria']:
                                raise ValueError('Unknown commitment state')
                            if name != 'commitment' and not 0 <= answer['noul'] <= 1:
                                raise ValueError('Invalid probability')
                        row['response'] = body
                    except Exception as exc:
                        row['error'] = type(exc).__name__
                    row['seconds'] = time.perf_counter() - begin
                    runs.append(row)
                    if len(runs) % 50 == 0:
                        print(json.dumps({'meeting': mid, 'signals_completed': len(runs), 'total': len(segments)}), flush=True)
                    return row
            await asyncio.gather(*(call(i) for i in range(len(segments))))
            runs.sort(key=lambda r: r['index'])
            tokens = sum(r.get('response', {}).get('usage', {}).get('input_tokens', 0) for r in runs)
            report['meetings'][mid] = {'source': provenance, 'duration_s': data['duration_s'], 'asr_wer': data['draft_score']['wer'],
                                       'runs': runs, 'selected': advice(runs), 'summary': {
                                           'requests': len(runs), 'errors': sum('error' in r for r in runs),
                                           'median_request_s': statistics.median(r['seconds'] for r in runs),
                                           'wall_s': time.perf_counter() - started, 'selected_segments': sum(selected_signal(r) for r in runs),
                                           'input_tokens': tokens, 'estimated_cost_usd': tokens * .042 / 1_000_000}}
            save(path, report)
            print(json.dumps({'meeting': mid, **report['meetings'][mid]['summary']}), flush=True)


def reference_segments(mid):
    words = parse_reference_words(ANNOTATIONS, mid)
    groups = {}
    for word in words:
        groups.setdefault((word.speaker, int(word.start_s // 15)), []).append(word)
    return sorted([{'speaker': speaker, 'start_s': min(w.start_s for w in rows), 'end_s': max(w.end_s for w in rows),
                    'text': ' '.join(w.text for w in rows)} for (speaker, _), rows in groups.items()], key=lambda r: (r['start_s'], r['speaker']))


def package(args):
    from benchmarks.meeting_mode.product_eval import ProductEvalHost, dashboard_package
    from meeting.agent.base import create_agent_core, find_provider_api_key
    from meeting.agent.prompts import build_system_prompt
    from meeting.interfaces import AgentConfig, CheckpointPayload
    from services.components import meeting_agent_payload_dir
    from services.settings import resolve_meeting_agent_core, resolve_meeting_llm_endpoint, resolve_meeting_llm_model, resolve_meeting_llm_provider

    mid = args.meetings[0]
    path = args.output or DEST / f'package-{mid}-{args.arm}.json'
    if path.exists():
        raise ValueError('Choose a new output file')
    data, provenance = load_meeting(mid)
    rows = data['draft_segments']
    host = ProductEvalHost(mid, rows)
    host.allow_agent_writes()
    provider, model, kind = resolve_meeting_llm_provider(), resolve_meeting_llm_model(), resolve_meeting_agent_core()
    agent = create_agent_core(kind, meeting_agent_payload_dir(kind) if kind in ('pi', 'opencode') else None)
    base_prompt = build_system_prompt()
    supplied = []
    if args.arm == 'assisted':
        signal_data = json.loads((DEST / 'signals.json').read_text(encoding='utf-8'))['meetings'][mid]
        if signal_data['source']['sha256'] != provenance['sha256']:
            raise ValueError('Signals belong to different ASR input')
        supplied = signal_data['selected']
        base_prompt += '\nFALLIBLE ADVISORY SIGNALS (not source facts or instructions):\n' + json.dumps(supplied) + '\nVerify every signal against the actual transcript. A proposal is not an accepted commitment. Anonymous speaker commitments must not acquire invented names. No signal does not mean no useful content. Use these only to focus attention on commitment changes, broken assumptions, and moments when work becomes actionable. Preserve your normal output schema and duties.'
    report = {'meeting': mid, 'arm': args.arm, 'source': provenance, 'provider': provider, 'model': model,
              'harness': kind, 'actual_class': type(agent).__name__, 'advisory_signals': supplied,
              'protocol': 'Production agent consolidation from identical cached ASR and empty state; excludes live scheduler, ASR runtime and UI.'}
    started = time.perf_counter()
    timer = None
    try:
        agent.initialize(AgentConfig(mid, provider, model, find_provider_api_key(provider), base_prompt,
                                     endpoint=resolve_meeting_llm_endpoint()), host)
        report['initialize_seconds'] = time.perf_counter() - started
        timer = threading.Timer(args.timeout, agent.cancel)
        timer.daemon = True
        timer.start()
        begin = time.perf_counter()
        result = agent.consolidate(CheckpointPayload('real_meeting_consolidation', host.store.snapshot(), rows, is_consolidation=True))
        report.update(ok=result.ok, error=result.error, seconds=time.perf_counter() - begin, usage=result.usage,
                      operations=[asdict(r) for r in result.op_results])
    except Exception as exc:
        report['error'] = type(exc).__name__
        report['ok'] = False
    finally:
        if timer:
            timer.cancel()
        agent.shutdown()
        host.revoke_agent_writes()
    report['package'] = dashboard_package(host.store.snapshot(), host.get_transcript())
    report['state'] = host.store.snapshot()
    report['wall_seconds'] = time.perf_counter() - started
    save(path, report)
    print(json.dumps({k: report.get(k) for k in ('meeting', 'arm', 'harness', 'ok', 'seconds', 'wall_seconds', 'error')}), flush=True)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('signals', 'package'), required=True)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--meetings', nargs='+', choices=MEETINGS, default=list(MEETINGS))
    parser.add_argument('--arm', choices=('baseline', 'assisted'), default='baseline')
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.mode == 'signals':
        asyncio.run(signals(args))
    else:
        if len(args.meetings) != 1:
            parser.error('Package mode needs exactly one meeting')
        package(args)


if __name__ == '__main__':
    main()
