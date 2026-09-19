"""LLM query expansion, production-style FTS5 retrieval, TypeSafe selection."""
import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path
import httpx
from dotenv import dotenv_values
from benchmarks.typesafe_comparison_cases import comparison_cases
from benchmarks.typesafe_experiments import MODEL, ROOT
from benchmarks.typesafe_hybrid import chat_call, parse_chat, ts_call
from services.text_llm import create_openai_client, get_profile


def match_expression(query):
    return ' '.join('"'+term.replace('"','""')+'"' for term in query.split())


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new output file')
    rows=[r for r in comparison_cases() if r['family']=='retrieval']
    corpus={f"{r['id']}_{sid}": text for r in rows for sid,text in r['state']['candidates'].items()}
    db=sqlite3.connect(':memory:')
    db.execute('CREATE VIRTUAL TABLE passages USING fts5(id UNINDEXED,text)')
    db.executemany('INSERT INTO passages(id,text) VALUES(?,?)',corpus.items())
    settings=json.loads((ROOT/'openwhisper_settings.json').read_text())
    profile=get_profile(settings['transcript_cleanup_provider'],settings)
    with create_openai_client(profile,timeout=60).with_options(max_retries=0) as llm:
        expansion=chat_call(llm,profile,settings['transcript_cleanup_model'],settings.get('transcript_cleanup_reasoning','off'),[
            {'role':'system','content':'Expand each search query into up to eight short lexical searches for transcript passages. Include synonyms and paraphrases, preferring distinctive single words when phrases would be too restrictive. You cannot see the documents. Return only JSON {"expansions":{"query_id":["search term"]}}. Use the actual supplied query IDs.'},
            {'role':'user','content':json.dumps({r['id']:r['state']['query'] for r in rows})}])
    expansions=parse_chat(expansion['text'])['expansions']
    key=os.environ.get('TYPESAFE_API_KEY') or dotenv_values(ROOT/'.env').get('TYPESAFE_API_KEY')
    if not key:parser.error('TypeSafe key unavailable')
    report={'expansion':expansion,'corpus':corpus,'results':[]}
    with httpx.Client(headers={'Authorization':'Bearer '+key},timeout=30,follow_redirects=False) as ts:
        for row in rows:
            query=row['state']['query']
            def retrieve(q):
                expr=match_expression(q)
                return [r[0] for r in db.execute('SELECT id FROM passages WHERE passages MATCH ? ORDER BY rowid DESC LIMIT 20',(expr,))] if expr else []
            original=retrieve(query)
            variants=expansions.get(row['id'],[])
            assert isinstance(variants,list) and all(isinstance(x,str) for x in variants)
            hits=list(dict.fromkeys(sid for q in [query,*variants[:8]] for sid in retrieve(q)))[:20]
            expected=row['expected']['answer']
            gold='none' if expected=='none' else row['id']+'_'+expected
            result={'id':row['id'],'query':query,'expected':gold,'original_hits':original,'expanded_hits':hits,'expansions':variants[:8]}
            if hits:
                criteria={sid:corpus[sid] for sid in hits}
                criteria['none']='No passage supplies the requested substantive information.'
                q={'type':'choice','instructions':'Which passage contains the most useful substantive answer about query? Prefer actual policy, resolution, owner or established fact over repeated keywords, agendas or unresolved questions. Choose none when no passage supplies the requested information.','criteria':criteria}
                run=ts_call(ts,{'model':MODEL,'state':{'query':query},'questions':{'answer':q}})
                result['selection']=run
                result['actual']=run['response']['answers']['answer']['choice']
            else:result['actual']='none'
            report['results'].append(result)
    db.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'positive_queries':sum(r['expected']!='none' for r in report['results']),'original_recall':sum(r['expected'] in r['original_hits'] for r in report['results']),'expanded_recall':sum(r['expected'] in r['expanded_hits'] for r in report['results']),'correct':sum(r['expected']==r['actual'] for r in report['results'])}))


if __name__=='__main__':main()
