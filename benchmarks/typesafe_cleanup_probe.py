"""Exercise actual production cleanup and verify generated outputs."""
import argparse
import json
import logging
import os
import time
from pathlib import Path
import httpx
from dotenv import dotenv_values
from benchmarks.typesafe_cases import cases
from benchmarks.typesafe_experiments import MODEL, ROOT, QUESTIONS
from benchmarks.typesafe_hybrid import ts_call
from config import config
from services.settings import compose_transcript_cleanup_prompt
from services.transcript_cleanup import TranscriptCleanup


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new output file')
    settings = json.loads((ROOT / 'openwhisper_settings.json').read_text())
    cleaner = TranscriptCleanup(provider=settings['transcript_cleanup_provider'], model=settings['transcript_cleanup_model'], reasoning=settings.get('transcript_cleanup_reasoning', 'off'))
    if cleaner.client:
        cleaner.client = cleaner.client.with_options(max_retries=0)
    selected = [r for r in cases() if r['experiment'] == 'cleanup' and r['split'] == 'holdout']
    report = {'model': cleaner.model, 'provider': cleaner.provider, 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for row in selected:
            state = row['state']
            prompt = compose_transcript_cleanup_prompt(config.TRANSCRIPT_CLEANUP_PROMPT, state['authorized_spelling_rules'])
            begin = time.perf_counter()
            cleaned = cleaner.cleanup(state['raw_transcript'], system_prompt=prompt, timeout_s=45)
            report['results'].append({'id': row['id'], 'raw': state['raw_transcript'], 'cleaned': cleaned, 'rules': state['authorized_spelling_rules'], 'seconds': time.perf_counter() - begin, 'success': cleaner.last_error is None})
            args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    finally:
        if cleaner.client:
            cleaner.client.close()
    states = [{'raw_transcript': r['raw'], 'cleaned_transcript': r['cleaned'], 'authorized_spelling_rules': r['rules']} for r in report['results']]
    questions = {}
    for index, state in enumerate(states):
        q = dict(QUESTIONS['cleanup']['relation'])
        for field in state:
            q['instructions'] = q['instructions'].replace('`'+field+'`', '`cases['+str(index)+'].'+field+'`')
        questions['c'+str(index)] = q
    key = os.environ.get('TYPESAFE_API_KEY') or dotenv_values(ROOT / '.env').get('TYPESAFE_API_KEY')
    if not key:
        parser.error('TypeSafe key unavailable')
    with httpx.Client(headers={'Authorization': 'Bearer '+key}, timeout=30, follow_redirects=False) as client:
        report['verification'] = ts_call(client, {'model': MODEL, 'state': {'cases': states}, 'questions': questions})
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'cases':len(states),'generation_seconds':sum(r['seconds'] for r in report['results']),'verification_seconds':report['verification']['seconds']}))


if __name__ == '__main__':
    main()
