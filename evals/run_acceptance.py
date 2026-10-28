"""Frozen M3.6 baseline. No business writes; never changes prompts from scores."""
import asyncio
import json
import time
from pathlib import Path
from datetime import UTC, datetime

from backend.app.ai import chat_json, query_embedding
from backend.app.config import Settings
from backend.app.knowledge import ROOT, KnowledgeError, digest, verify_sources
from backend.app.knowledge_api import KnowledgeQuery, KnowledgeRuntime, empty_answer, live_data_answer, qualification, finish_qualification
from backend.app.knowledge_answers import answer_messages, bind_answer, citation_pool, part_types, requires_live_data


def coverage(case, suite, retrieval):
    hits = {r['unit_id'] for r in retrieval.get('hits', [])}
    expanded = {u for c in retrieval.get('contexts', []) for u in c['unit_ids']}
    if suite == 'inventory':
        groups = [{r['clause_id'] for r in g['any_of']} for g in case['evidence_groups']]
        hit_versions = {(u['unit_id'], u.get('document_version')) for u in retrieval.get('hits', [])}
        expanded_versions = {(unit, c.get('document_version')) for c in retrieval.get('contexts', []) for unit in c['unit_ids']}
        expected = [{(r['clause_id'], r['document_version']) for r in group['any_of']} for group in case['evidence_groups']]
        return {'expected_groups': [sorted(g) for g in groups],
            'raw_hit': sum(bool(group & hit_versions) for group in expected),
            'expanded_hit': sum(bool(group & expanded_versions) for group in expected)}
    else:
        sections = {s for s in case['expected_sections'] if not s.startswith('DX-RULE-')}
        groups = [sections] if sections else []
    return {'expected_groups': [sorted(g) for g in groups],
        'raw_hit': sum(bool(g & hits) for g in groups), 'expanded_hit': sum(bool(g & expanded) for g in groups)}


def summarize(rows, suite):
    cases = [row for row in rows if row['suite'] == suite]
    targets = [(row, coverage(row['rubric'], suite, {})['expected_groups']) for row in cases]
    targets = [(row, groups) for row, groups in targets if groups]
    result = {'questions': len(cases), 'answers': sum('answer' in row for row in cases),
        'errors': sum('error' in row for row in cases)}
    for mode in ['dense', 'hybrid']:
        # All fixed targets remain in the denominator, including failures before retrieval.
        found = [(row.get('retrieval', {}).get(mode, {}), len(groups)) for row, groups in targets]
        result[mode] = {'target_questions': len(targets), 'expected_groups': sum(count for _, count in found),
            'raw_groups_hit': sum(hit.get('raw_hit', 0) for hit, _ in found),
            'expanded_groups_hit': sum(hit.get('expanded_hit', 0) for hit, _ in found),
            'raw_complete_questions': sum(hit.get('raw_hit', 0) == count for hit, count in found),
            'expanded_complete_questions': sum(hit.get('expanded_hit', 0) == count for hit, count in found)}
    return result


async def main(index_root=ROOT, *, regression=False, answer_mode='default'):
    settings = Settings()
    runtime = KnowledgeRuntime(settings, index_root)
    runtime.start()
    report = {'created_at': datetime.now(UTC).isoformat(), 'purpose': 'exposed_acceptance_regression_not_independent' if regression else 'M3.6 frozen acceptance baseline before M4',
        'model': settings.model_id, 'embedding': settings.embedding_model_id, 'top_k': 5,
        'generation_settings': {'temperature': 0, 'max_tokens': 3000, 'retries': 0},
        'answer_mode': answer_mode, 'retrieval_denominator': 'all_fixed_target_cases_including_errors',
        'input_hashes': {}, 'code_hashes': {}, 'cases': [], 'semantic_review': 'pending_project_ai_review_not_external_expert'}
    output = ROOT / 'evals/runs' / (('m3-quality-regression-' if regression else 'm3-6-acceptance-') + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '.json')
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(output)
    try:
        if runtime.index is None:
            raise runtime.error
        from evals.inventory.check_dataset import main as check_dataset
        check_dataset()
        verify_sources(runtime.index.sources, ROOT)
        report['current_source_lock_checked'] = True
        report.update(generation_id=runtime.index.report['generation_id'], sources_hash=digest(runtime.index.sources),
            index_fingerprint=runtime.index.report['fingerprint'])
        for filename in ['backend/app/knowledge_answers.py', 'backend/app/retrieval.py', 'backend/app/ai.py', 'evals/inventory/corpus-lock.json']:
            report['code_hashes'][filename] = digest((ROOT/filename).read_bytes())
        for suite, kb in [('inventory', 'inventory_policies'), ('diagnostics', 'general_diagnostics')]:
            path = ROOT/f'evals/{suite}/acceptance.json'
            report['input_hashes'][path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
            for case in json.loads(path.read_text(encoding='utf-8'))['cases']:
                row = {'case_id': case['case_id'], 'suite': suite, 'question': case['input'], 'retrieval': {}, 'rubric': case}
                report['cases'].append(row)
                started = time.monotonic()
                query = KnowledgeQuery(question=case['input'], knowledge_base=kb, knowledge_scope=case.get('knowledge_scope', 'draft_preview'),
                    retrieval_mode=None if answer_mode == 'default' else answer_mode)
                row['selected_retrieval_mode'] = query.retrieval_mode
                try:
                    before = runtime.read(qualification, query)
                    vector = None
                    if before['allowed']:
                        vector, usage = await query_embedding(settings, query.question)
                        row['embedding_usage'] = usage
                    for mode in ['dense', 'hybrid']:
                        found = runtime.read(lambda i: i.search(query.question, kb, scope=query.knowledge_scope,
                            mode=mode, top_k=5, query_vector=vector))
                        row['retrieval'][mode] = found | coverage(case, suite, found)
                    selected = row['retrieval'][query.retrieval_mode]
                    if selected['status'] != 'retrieved':
                        result = empty_answer('no_eligible_documents', '当前范围无可用来源')
                    elif requires_live_data(query.question):
                        result = live_data_answer(kb)
                    else:
                        pool, types = runtime.read(lambda i: (citation_pool(i, selected), part_types(i, selected)))
                        messages = answer_messages(query.question, selected, pool, types)
                        payload = json.loads(messages[-1]['content'])
                        # Synthetic fixtures are untrusted data, never promoted to indexed or approved sources.
                        payload['untrusted_test_material'] = case.get('context_injection', [])
                        for fixture in case.get('context_injection', []):
                            if fixture['source_type'] == 'synthetic_untrusted_retrieval_fixture':
                                payload['evidence'].append({'fixture_id': fixture['fixture_id'], 'excerpt': fixture['text'], 'source_type': fixture['source_type']})
                        messages[-1]['content'] = json.dumps(payload, ensure_ascii=False)
                        async with asyncio.timeout(40):
                            raw, usage = await chat_json(settings, messages)
                        row['raw_model_output'] = raw
                        row['chat_usage'] = usage
                        result = bind_answer(raw, pool, types, query.question, kb)
                    runtime.read(finish_qualification, before, query)
                    row['answer'] = result
                    citations = result.get('citations', [])
                    row['structural_checks'] = {'citation_binding_valid': True,
                        'citation_count': len(citations), 'no_business_tools_available': True,
                        'release_filter_pass': selected['status'] == 'no_eligible_documents' if query.knowledge_scope == 'released_only' else None}
                except (KnowledgeError, TimeoutError) as error:
                    row['error'] = {'code': error.code if isinstance(error, KnowledgeError) else 'timeout',
                        'detail': error.detail if isinstance(error, KnowledgeError) else '40秒截止'}
                row['elapsed_seconds'] = round(time.monotonic()-started, 3)
                save()
                print(case['case_id'], row.get('answer', {}).get('answer_status', row.get('error')), flush=True)
        report['summary'] = {suite: summarize(report['cases'], suite) for suite in ['inventory', 'diagnostics']}
        save()
    finally:
        runtime.close()
        save()
        print('report', output.relative_to(ROOT), flush=True)


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--index-root',type=Path,default=ROOT)
    parser.add_argument('--regression',action='store_true')
    parser.add_argument('--answer-mode', choices=['default', 'dense', 'hybrid'], default='default')
    args=parser.parse_args()
    asyncio.run(main(args.index_root,regression=args.regression,answer_mode=args.answer_mode))
