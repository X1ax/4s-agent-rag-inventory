"""Trace exposed retrieval cases through an isolated index; no chat or business writes."""
import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from backend.app.ai import embedding_contract
from backend.app.config import Settings
from backend.app.knowledge import ROOT, digest, parse_corpora, verify_sources
from backend.app.knowledge_answers import answer_messages, citation_pool
from backend.app.knowledge_lifecycle import qualify_sources
from backend.app.lexical import fuse, lexical_candidates
from backend.app.retrieval import CANDIDATE_LIMIT, CONTEXT_BUDGET, RETRIEVAL_VERSION, open_index
from evals.run_acceptance import coverage


def audit(index_root, vectors_path):
    settings = Settings()
    cache = json.loads(vectors_path.read_text(encoding='utf-8'))
    assert cache['contract'] == embedding_contract(settings)
    report = {'created_at': datetime.now(UTC).isoformat(), 'retrieval_version': RETRIEVAL_VERSION,
        'scope': 'exposed fixed regression; not independent or answer accuracy',
        'embedding_requests': 0, 'chat_requests': 0, 'business_writes': 0,
        'input_hashes': {}, 'code_hashes': {}, 'cases': [], 'summary': {}}
    for name in ['backend/app/retrieval.py', 'backend/app/lexical.py', 'backend/app/knowledge.py',
            'backend/app/knowledge_api.py', 'backend/app/knowledge_answers.py']:
        report['code_hashes'][name] = digest((ROOT/name).read_bytes())
    with open_index(settings, index_root) as index:
        verify_sources(index.sources, index_root)
        parsed = parse_corpora(index_root)
        report.update(generation_id=index.report['generation_id'], fingerprint=index.report['fingerprint'],
            parsed_documents=len(parsed['documents']), parsed_units=len(parsed['units']),
            indexed_units=len(index.units), parser_matches_index={u['chunk_id'] for u in parsed['units']} == set(index.units),
            current_project_matches_index={u['chunk_id'] for u in parse_corpora(ROOT)['units']} == set(index.units),
            domains={})
        for suite, kb in [('inventory', 'inventory_policies'), ('diagnostics', 'general_diagnostics')]:
            units = [u for u in index.units.values() if u['knowledge_base'] == kb]
            collection = index.client.get_collection(kb, embedding_function=None)
            stored = collection.get(include=['embeddings', 'documents'])
            vectors = dict(zip(stored['ids'], stored['embeddings'], strict=True))
            report['domains'][kb] = {
                'preview_documents': sorted(qualify_sources(index.sources, index_root, kb, 'draft_preview')['allowed']),
                'released_documents': sorted(qualify_sources(index.sources, index_root, kb, 'released_only')['allowed']),
                'ids_match': set(stored['ids']) == {u['chunk_id'] for u in units},
                'stored_text_matches': dict(zip(stored['ids'], stored['documents'], strict=True)) == {u['chunk_id']:u['embedding_text'] for u in units},
                'dimensions': sorted({len(v) for v in vectors.values()}),
                'nonfinite_vectors': sum(not all(math.isfinite(x) for x in v) for v in vectors.values()),
                'zero_vectors': sum(not any(v) for v in vectors.values()),
            }
            for split in ['development', 'acceptance']:
                path = ROOT/f'evals/{suite}/{split}.json'
                report['input_hashes'][path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
                for case in json.loads(path.read_text(encoding='utf-8'))['cases']:
                    q = case['input']
                    vector = cache['vectors'][digest(q)]
                    result = index.search(q, kb, scope=case.get('knowledge_scope', 'draft_preview'), mode='hybrid', query_vector=vector)
                    stats = coverage(case, suite, result)
                    dense_result = index.search(q, kb, scope=case.get('knowledge_scope', 'draft_preview'), mode='dense', query_vector=vector)
                    row = {'case_id': case['case_id'], 'suite': suite, 'split': split, 'question': q,
                        'status': result['status'], **stats, 'context_characters': result['context_characters'],
                        'dense_baseline':coverage(case, suite, dense_result),
                        'omitted_contexts': result['omitted_contexts'], 'missing_groups': []}
                    report['cases'].append(row)
                    pool = citation_pool(index, result)
                    prompt = json.loads(answer_messages(q, result, pool, {})[-1]['content'])
                    delivered = {u for c in prompt['evidence'] for u in c['unit_ids']}
                    row['context_reaches_prompt'] = delivered == {u for c in result['contexts'] for u in c['unit_ids']}
                    row['prompt_characters'] = len(json.dumps(prompt, ensure_ascii=False))
                    row['budget_respected'] = result['context_characters'] <= CONTEXT_BUDGET
                    if not result['hits']:
                        continue
                    query = collection.query(query_embeddings=[vector], n_results=len(units), include=['distances'])
                    dense = sorted(zip(query['ids'][0], query['distances'][0]), key=lambda t: (t[1], t[0]))
                    lexical = lexical_candidates(q, units, len(units))
                    fused, _ = fuse(dense[:CANDIDATE_LIMIT], lexical[:CANDIDATE_LIMIT], [])
                    vnorm = math.sqrt(sum(x*x for x in vector))
                    exact = sorted(((key, 1-sum(a*b for a,b in zip(vector, values, strict=True))/(vnorm*math.sqrt(sum(x*x for x in values))))
                        for key,values in vectors.items()), key=lambda t:(t[1],t[0]))
                    row['dense_top20_agrees_exact'] = {k for k,_ in dense[:20]} == {k for k,_ in exact[:20]}
                    application = collection.query(query_embeddings=[vector], n_results=min(CANDIDATE_LIMIT,len(units)), include=[])
                    row['application_top20_agrees_exact'] = set(application['ids'][0]) == {k for k,_ in exact[:CANDIDATE_LIMIT]}
                    for group in stats['expected_groups']:
                        if set(group) & delivered:
                            continue
                        alternatives = []
                        for unit_id in group:
                            unit = next((u for u in units if u['unit_id'] == unit_id), None)
                            ranks = {}
                            if unit:
                                for name, ranked in [('dense',dense),('lexical',lexical),('exact_cosine',exact)]:
                                    ranks[name] = next((i for i,(key,_) in enumerate(ranked,1) if key==unit['chunk_id']), None)
                                ranks['rrf'] = fused.index(unit['chunk_id'])+1 if unit['chunk_id'] in fused else None
                            stage = 'unit_missing' if not unit else 'candidate_pool' if not ranks['rrf'] else 'context_selection'
                            if unit and any(c.get('context_id') == unit['parent_id'] for c in result['omitted_contexts']):
                                stage = 'context_budget'
                            alternatives.append({'unit_id':unit_id,'stage':stage,'ranks':ranks,
                                'source_format':unit['native_format'] if unit and 'native_format' in unit else
                                    next((d['native_format'] for d in index.sources['documents'] if unit and d['document_id']==unit['document_id']),None)})
                        row['missing_groups'].append(alternatives)
        verify_sources(index.sources, index_root)
    for suite in ['inventory','diagnostics']:
        for split in ['development','acceptance']:
            rows = [r for r in report['cases'] if r['suite']==suite and r['split']==split]
            report['summary'][suite+'_'+split] = {'questions':len(rows),
                'expected_groups':sum(len(r['expected_groups']) for r in rows),
                'raw_hit':sum(r['raw_hit'] for r in rows), 'expanded_hit':sum(r['expanded_hit'] for r in rows),
                'dense_raw_hit':sum(r['dense_baseline']['raw_hit'] for r in rows),
                'dense_expanded_hit':sum(r['dense_baseline']['expanded_hit'] for r in rows),
                'mean_context_characters':round(sum(r['context_characters'] for r in rows)/len(rows),1),
                'all_context_reaches_prompt':all(r['context_reaches_prompt'] for r in rows),
                'all_budget_respected':all(r['budget_respected'] for r in rows)}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--index-root', type=Path, required=True)
    parser.add_argument('--vectors', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.index_root, args.vectors)
    output = ROOT/'evals/runs'/('rag-chain-audit-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(result, ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['summary'],ensure_ascii=False))
    for row in result['cases']:
        if row['missing_groups']:
            print(row['case_id'],json.dumps(row['missing_groups'],ensure_ascii=False))
    print(output)
