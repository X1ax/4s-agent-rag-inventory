"""Paired field-terminology regression with shared vectors and unchanged source eligibility."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from backend.app.ai import EmbeddingClient, embedding_contract
from backend.app.config import Settings
from backend.app.knowledge import ROOT, digest, verify_sources
from backend.app.retrieval import open_index


def main(index_root, cache_path):
    settings = Settings()
    path = ROOT/'evals/inventory/field-terminology-regression.json'
    cases = json.loads(path.read_text(encoding='utf-8'))['cases']
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {'contract':embedding_contract(settings),'vectors':{}}
    assert cache['contract'] == embedding_contract(settings)
    client = EmbeddingClient(settings)
    missing = [c['input'] for c in cases if digest(c['input']) not in cache['vectors']]
    if missing:
        for q,v in zip(missing,client.embed(missing),strict=True):
            cache['vectors'][digest(q)] = v
        cache_path.parent.mkdir(parents=True,exist_ok=True)
        cache_path.write_text(json.dumps(cache),encoding='utf-8')
    report = {'scope':'additional exposed development regression, not answer accuracy',
        'input_sha256':digest(path.read_bytes()), 'embedding_usage':client.usage(), 'chat_requests':0,'cases':[]}
    with open_index(settings,index_root) as index:
        report.update(generation_id=index.report['generation_id'],fingerprint=index.report['fingerprint'])
        for case in cases:
            kwargs = {'scope':'draft_preview','mode':'hybrid','query_vector':cache['vectors'][digest(case['input'])]}
            with patch('backend.app.retrieval.field_query_terms',lambda q:[]):
                before = index.search(case['input'],'inventory_policies',**kwargs)
            after = index.search(case['input'],'inventory_policies',**kwargs)
            row = dict(case)
            for stage,result in [('before',before),('after',after)]:
                expanded = {u for c in result['contexts'] for u in c['unit_ids']}
                row[stage] = {'expected_hit':bool(expanded.intersection(case['expected_any'])),
                    'expanded_units':sorted(expanded),'context_characters':result['context_characters'],
                    'query_terms':result['query_terms'],'omitted':result['omitted_contexts']}
            row['raw_hits_preserved'] = before['hits']==after['hits']
            row['original_contexts_preserved'] = {c['context_id'] for c in before['contexts']} <= {c['context_id'] for c in after['contexts']}
            report['cases'].append(row)
        verify_sources(index.sources,index_root)
    report['summary'] = {s:sum(c[s]['expected_hit'] for c in report['cases']) for s in ['before','after']}
    report['summary']['questions'] = len(cases)
    report['checks'] = {k:all(c[k] for c in report['cases']) for k in ['raw_hits_preserved','original_contexts_preserved']}
    output = ROOT/'evals/runs'/('field-terminology-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(report['summary'],report['checks'],client.usage())
    for c in report['cases']:
        print(c['case_id'],c['before']['expected_hit'],c['after']['expected_hit'])
    print(output)
    assert all(report['checks'].values())


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--index-root',type=Path,required=True)
    parser.add_argument('--vectors',type=Path,required=True)
    args=parser.parse_args()
    main(args.index_root,args.vectors)
