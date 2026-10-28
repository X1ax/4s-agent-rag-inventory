"""Paired dense/hybrid retrieval on development questions; never reads acceptance questions."""
import json
import time
from datetime import datetime, timezone

from backend.app.ai import EmbeddingClient
from backend.app.config import get_settings
from backend.app.knowledge import ROOT, KnowledgeError, digest
from backend.app.retrieval import CANDIDATE_LIMIT, CONTEXT_BUDGET, RETRIEVAL_VERSION, open_index


def main(index_root=ROOT):
    settings = get_settings()
    embedder = EmbeddingClient(settings)
    request_embedding = embedder.embed
    vectors = {}

    def shared_query_embedding(texts, *, build=False):
        if build:
            raise ValueError('This evaluation does not build an index')
        key = tuple(texts)
        if key not in vectors:
            vectors[key] = request_embedding(texts)
        return vectors[key]

    embedder.embed = shared_query_embedding
    report = {'run_type': 'paired_dense_hybrid_development', 'acceptance_run': False,
        'chat_model_called': False, 'answer_quality_evaluated': False, 'top_k': 5,
        'candidate_limit': CANDIDATE_LIMIT, 'rrf_constant': 60, 'context_budget': CONTEXT_BUDGET,
        'retrieval_version': RETRIEVAL_VERSION, 'query_vectors_shared': True,
        'timing_note': 'dense先生成查询向量，hybrid复用同一向量；各模式elapsed不可用于端到端速度比较',
        'input_files': {}, 'cases': [], 'created_at': datetime.now(timezone.utc).isoformat()}
    try:
        with open_index(settings, index_root, embedder=embedder) as index:
            report.update(generation_id=index.report['generation_id'], fingerprint=index.report['fingerprint'],
                embedding=index.report['embedding'])
            report['default_scope_check'] = {}
            for mode in ['dense', 'hybrid']:
                found = index.search('库存制度现在可以执行吗？', 'inventory_policies', mode=mode)
                report['default_scope_check'][mode] = found['status']
                if found['status'] != 'no_eligible_documents' or embedder.requests:
                    raise KnowledgeError('unexpected_release_state', '当前开发语料应全部待核准，且默认查询不调用Embedding')
            for suite, kb in [('inventory', 'inventory_policies'), ('diagnostics', 'general_diagnostics')]:
                path = ROOT / f'evals/{suite}/development.json'
                raw = path.read_bytes()
                report['input_files'][path.relative_to(ROOT).as_posix()] = digest(raw)
                for case in json.loads(raw)['cases']:
                    groups = ([{r['clause_id'] for r in group['any_of']} for group in case['evidence_groups']]
                        if suite == 'inventory' else [{s} for s in case['expected_sections'] if not s.startswith('DX-RULE-')])
                    record = {'case_id': case['case_id'], 'knowledge_base': kb, 'question': case['input'],
                        'expected_groups': [sorted(group) for group in groups], 'modes': {}}
                    report['cases'].append(record)
                    for mode in ['dense', 'hybrid']:
                        started = time.monotonic()
                        found = index.search(case['input'], kb, scope='draft_preview', mode=mode, top_k=5)
                        hits = {h['unit_id'] for h in found['hits']}
                        expanded = {u for c in found['contexts'] for u in c['unit_ids']}
                        record['modes'][mode] = {'status': found['status'], 'hits': found['hits'],
                            'contexts': [{k: c[k] for k in ('context_id', 'document_id', 'document_version', 'unit_ids', 'expansion_reason')}
                                for c in found['contexts']], 'context_characters': found['context_characters'],
                            'omitted_contexts': found['omitted_contexts'], 'raw_groups_hit': sum(bool(g & hits) for g in groups),
                            'expanded_groups_hit': sum(bool(g & expanded) for g in groups),
                            'elapsed_seconds': round(time.monotonic()-started, 3)}
                    print(json.dumps({'case_id': case['case_id'], **{m: record['modes'][m]['raw_groups_hit']
                        for m in ['dense', 'hybrid']}, 'expected': len(groups)}, ensure_ascii=False), flush=True)
            report['usage'] = embedder.usage()
        report['summary'] = {}
        for kb in ['inventory_policies', 'general_diagnostics']:
            cases = [c for c in report['cases'] if c['knowledge_base'] == kb]
            report['summary'][kb] = {'questions': len(cases), 'expected_groups': sum(len(c['expected_groups']) for c in cases)}
            for mode in ['dense', 'hybrid']:
                report['summary'][kb][mode] = {
                    'raw_groups_hit': sum(c['modes'][mode]['raw_groups_hit'] for c in cases),
                    'raw_complete_queries': sum(c['modes'][mode]['raw_groups_hit'] == len(c['expected_groups']) for c in cases),
                    'expanded_complete_queries': sum(c['modes'][mode]['expanded_groups_hit'] == len(c['expected_groups']) for c in cases)}
        report['regressions'] = [c['case_id'] for c in report['cases']
            if c['modes']['hybrid']['raw_groups_hit'] < c['modes']['dense']['raw_groups_hit']]
        report['improvements'] = [c['case_id'] for c in report['cases']
            if c['modes']['hybrid']['raw_groups_hit'] > c['modes']['dense']['raw_groups_hit']]
        directory = ROOT / 'evals/runs'
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / ('paired-development-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        with output.open('x', encoding='utf-8', newline='\n') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        print(json.dumps({'output': output.relative_to(ROOT).as_posix(), 'summary': report['summary'],
            'improvements': report['improvements'], 'regressions': report['regressions'], 'usage': report['usage']}, ensure_ascii=False, indent=2))
        return 0
    except KnowledgeError as error:
        print(json.dumps({'status': 'failed', 'error_code': error.code, 'detail': error.detail}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
