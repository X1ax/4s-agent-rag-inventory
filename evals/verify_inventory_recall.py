"""Replay fixed queries with shared cached real vectors; never modify inputs or call chat."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from backend.app.ai import embedding_contract
from backend.app.config import Settings
from backend.app.knowledge import ROOT, digest, verify_sources
from backend.app.retrieval import RETRIEVAL_VERSION, open_index
from evals.run_acceptance import coverage


def main(index_root, baseline_path, vectors_path):
    settings = Settings()
    before = json.loads(baseline_path.read_text(encoding='utf-8'))
    cache = json.loads(vectors_path.read_text(encoding='utf-8'))
    assert cache['contract'] == embedding_contract(settings)
    report = {'created_at': datetime.now(UTC).isoformat(), 'scope': 'exposed fixed retrieval regression; not answer accuracy',
        'baseline_sha256': digest(baseline_path.read_bytes()), 'query_vectors_sha256': digest(vectors_path.read_bytes()),
        'retrieval_version': RETRIEVAL_VERSION, 'embedding_requests': 0, 'chat_requests': 0, 'top_k': 5,
        'context_budget': 12000, 'code_hashes': {name: digest((ROOT/name).read_bytes())
            for name in ['backend/app/retrieval.py', 'backend/app/lexical.py', 'evals/verify_inventory_recall.py']},
        'cases': [], 'summary': {}}
    with open_index(settings, index_root) as index:
        assert index.report['generation_id'] == before['generation_id']
        verify_sources(index.sources, ROOT)
        report.update(generation_id=index.report['generation_id'], source_fingerprint=index.report['fingerprint'])
        for old in before['cases']:
            case, suite = old['rubric'], old['suite']
            kb = 'inventory_policies' if suite == 'inventory' else 'general_diagnostics'
            row = {'case_id': case['case_id'], 'split': old['split'], 'suite': suite, 'question': case['input'],
                'before': {key: old['baseline'][key] for key in ['raw_hit', 'expanded_hit', 'expected_groups', 'context_characters']}}
            vector = cache['vectors'][digest(case['input'])]
            found = index.search(case['input'], kb, scope=case.get('knowledge_scope', 'draft_preview'),
                mode='hybrid', top_k=5, query_vector=vector)
            expanded = {u for c in found['contexts'] for u in c['unit_ids']}
            row['after'] = coverage(case, suite, found) | {'context_characters': found['context_characters'],
                'hit_ids': [h['unit_id'] for h in found['hits']], 'context_ids': [c['context_id'] for c in found['contexts']],
                'expanded_unit_ids': sorted(expanded), 'omitted_contexts': found['omitted_contexts']}
            row['original_hits_preserved'] = [h['chunk_id'] for h in found['hits']] == [h['chunk_id'] for h in old['baseline']['hits']]
            row['original_contexts_preserved'] = {c['context_id'] for c in old['baseline']['contexts']} <= set(row['after']['context_ids'])
            row['budget_respected'] = found['context_characters'] <= 12000
            groups = row['after']['expected_groups']
            row['remaining_groups'] = [g for g in groups if not set(g) & expanded]
            report['cases'].append(row)
        verify_sources(index.sources, ROOT)
    for suite in ['inventory', 'diagnostics']:
        for split in ['development', 'acceptance']:
            rows = [r for r in report['cases'] if r['suite'] == suite and r['split'] == split]
            report['summary'][suite+'_'+split] = {stage: {
                'raw': sum(r[stage]['raw_hit'] for r in rows), 'expanded': sum(r[stage]['expanded_hit'] for r in rows),
                'expected': sum(len(r[stage]['expected_groups']) for r in rows),
                'expanded_complete_questions': sum(r[stage]['expanded_hit'] == len(r[stage]['expected_groups']) for r in rows if r[stage]['expected_groups']),
                'mean_context_characters': round(sum(r[stage]['context_characters'] for r in rows)/len(rows), 1)}
                for stage in ['before', 'after']}
    report['checks'] = {name: all(r[name] for r in report['cases'])
        for name in ['original_hits_preserved', 'original_contexts_preserved', 'budget_respected']}
    output = ROOT/'evals/runs'/('inventory-recall-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2)); print(report['checks']); print(output)
    assert all(report['checks'].values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--index-root', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--vectors', type=Path, required=True)
    args = parser.parse_args()
    main(args.index_root, args.baseline, args.vectors)
