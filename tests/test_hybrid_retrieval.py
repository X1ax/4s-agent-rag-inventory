import json
from datetime import date, timedelta

import pytest

from backend.app.knowledge import KnowledgeError, digest
from backend.app.lexical import UNIT_CODE, fuse, lexical_candidates, tokens
from backend.app.retrieval import activate_generation, build_index, open_index
from test_retrieval import FakeEmbedding, project, settings


def edit_document(project, doc_id, change):
    kb = 'inventory' if doc_id.startswith('INV') else 'diagnostics'
    path = project / f'data/knowledge/{kb}/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    row = next(d for d in manifest['documents'] if d['document_id'] == doc_id)
    source = path.parent / row['path']
    text, values = change(source.read_text(encoding='utf-8'))
    source.write_text(text, encoding='utf-8')
    row.update(values, sha256=digest(source.read_bytes()))
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')


def test_character_tokens_keep_complete_codes_and_do_not_equate_part_numbers():
    assert tokens('库存查询') == {'库存': 1, '存查': 1, '查询': 1}
    assert set(tokens('AB-12 AB12 AB_12 ab-12')) == {'ab-12', 'ab12', 'ab_12'}
    assert tokens('AB-12')['ab-12'] == 1 and 'ab12' not in tokens('AB-12')
    assert UNIT_CODE.findall('查inv-mdm-002-c005，不是INV-MDM-002-C0050') == ['inv-mdm-002-c005']
    rows = [{'chunk_id': 'a', 'embedding_text': '库存 AB-12'}, {'chunk_id': 'b', 'embedding_text': '别的 AB12'}]
    assert lexical_candidates('AB-12', rows)[0][0] == 'a'
    assert lexical_candidates('unrelated', rows) == []
    ordered, scores = fuse([('a', .1), ('b', .2)], [('b', .8), ('a', .4)], ['c'])
    assert ordered == ['c', 'a', 'b'] and scores['a']['rrf_score'] == 1/61 + 1/62


@pytest.mark.parametrize('mode,exact,budgeted', [('hybrid', False, False), ('dense', False, False),
    ('hybrid', True, False), ('hybrid', False, True)])
def test_inventory_branch_context_recovers_ranked_parent_without_replacing_hits(settings, project, monkeypatch, mode, exact, budgeted):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        parents = {}
        for unit in index.units.values():
            if unit['knowledge_base'] == 'inventory_policies':
                parents.setdefault(unit['parent_id'], []).append(unit)
        primary = next(units[:5] for units in parents.values() if len(units) >= 5)
        secondary = next(units[0] for parent, units in parents.items() if parent != primary[0]['parent_id']
            and units[0]['document_id'] == primary[0]['document_id'])
        ranked = primary + [secondary]
        class Collection:
            def query(self, **kwargs):
                return {'ids': [[u['chunk_id'] for u in ranked]], 'distances': [[.1+i/100 for i in range(len(ranked))]]}
        monkeypatch.setattr(index.client, 'get_collection', lambda *a, **kw: Collection())
        monkeypatch.setattr('backend.app.retrieval.lexical_candidates', lambda *a, **kw:
            [(u['chunk_id'], 1-i/100) for i, u in enumerate(ranked)])
        if budgeted:
            monkeypatch.setattr('backend.app.retrieval.CONTEXT_BUDGET', len(index.contexts[primary[0]['parent_id']]['context_text']))
        question = primary[0]['unit_id'] if exact else '如何核对库存业务记录？'
        found = index.search(question, 'inventory_policies', scope='draft_preview', mode=mode,
            top_k=1 if exact else 5, document_ids=[primary[0]['document_id']])
        assert [u['unit_id'] for u in found['hits']] == [u['unit_id'] for u in primary[:1 if exact else 5]]
        contexts = {c['context_id']: c for c in found['contexts']}
        if mode == 'hybrid' and not exact and not budgeted:
            assert secondary['parent_id'] in contexts
            assert contexts[secondary['parent_id']]['expansion_reason'] == 'branch_candidate_section'
        else:
            assert secondary['parent_id'] not in contexts
        assert all(c['document_id'] == primary[0]['document_id'] for c in contexts.values())
        assert found['context_characters'] <= found['context_budget']


def test_exact_codes_filters_and_dense_baseline_are_separate(settings, project):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        hit = index.search('查 inv-mdm-002-c005 的完整条件', 'inventory_policies', scope='draft_preview', mode='hybrid', top_k=1)
        assert hit['hits'][0]['unit_id'] == 'INV-MDM-002-C005'
        assert hit['hits'][0]['exact_match'] and hit['hits'][0]['rrf_score'] >= 0
        assert all(c['knowledge_base'] == 'inventory_policies' for c in hit['contexts'])
        dense = index.search('INV-MDM-002-C005', 'inventory_policies', scope='draft_preview', mode='dense')
        assert all('exact_match' not in u and 'rrf_score' not in u for u in dense['hits'])
        for mode in ['dense', 'hybrid']:
            filtered = index.search('库存', 'inventory_policies', scope='draft_preview', mode=mode,
                document_ids=['INV-MDM-002'], document_versions={'INV-MDM-002': '1.0-draft.3'})
            assert filtered['hits'] and all(u['document_id'] == 'INV-MDM-002' and u['document_version'] == '1.0-draft.3'
                for u in filtered['hits'] + filtered['contexts'])
        calls = embedder.calls
        for question, options, status in [
            ('INV-MDM-002-C999', {}, 'exact_not_found'),
            ('INV-MDM-002-C005', {'document_versions': {'INV-MDM-002': 'obsolete'}}, 'no_eligible_documents'),
            ('INV-MDM-002-C005 INV-MDM-002-C006', {'top_k': 1}, 'exact_limit_exceeded'),
        ]:
            result = index.search(question, 'inventory_policies', scope='draft_preview', mode='hybrid', **options)
            assert result['status'] == status and not result['hits'] and result['embedding_requests'] == 0
        assert embedder.calls == calls
        with pytest.raises(KnowledgeError):
            index.search('库存', 'inventory_policies', mode='unknown')


@pytest.mark.parametrize('change', ['raw_change', 'delete_file', 'remove_registration', 'withdrawn', 'superseded', 'unrecognized', 'new_version'])
def test_changed_source_is_excluded_everywhere_but_other_documents_remain(settings, project, change):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        source = project / 'data/knowledge/inventory/policies/INV-ADJ-008.md'
        if change == 'raw_change':
            source.write_bytes(source.read_bytes() + b'\nchanged')
        elif change == 'delete_file':
            source.unlink()
        elif change == 'remove_registration':
            path = source.parent.parent / 'manifest.json'
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['documents'] = [d for d in manifest['documents'] if d['document_id'] != 'INV-ADJ-008']
            path.write_text(json.dumps(manifest), encoding='utf-8')
        elif change in {'withdrawn', 'superseded', 'unrecognized'}:
            edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('pending_approval', change), {'approval_status': change}))
        else:
            edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('version: 1.0-draft.1', 'version: 2.0'), {'version': '2.0'}))
        for mode in ['dense', 'hybrid']:
            result = index.search('退库与盘点差异', 'inventory_policies', scope='draft_preview', mode=mode)
            assert result['hits'] and any(d['document_id'] == 'INV-ADJ-008' for d in result['unavailable_documents'])
            assert all(u['document_id'] != 'INV-ADJ-008' for u in result['hits'] + result['contexts'])
        calls = embedder.calls
        exact = index.search('INV-ADJ-008-C001', 'inventory_policies', scope='draft_preview', mode='hybrid')
        assert exact['status'] == 'exact_not_found' and not exact['hits'] and embedder.calls == calls
    # Opening the old generation after restart must apply the same live exclusions.
    with open_index(settings, project, embedder=embedder) as index:
        result = index.search('盘点', 'inventory_policies', scope='draft_preview')
        assert all(u['document_id'] != 'INV-ADJ-008' for u in result['hits'] + result['contexts'])


@pytest.mark.parametrize('change', ['common_file', 'mapping', 'invalid_manifest'])
def test_diagnostic_common_or_auxiliary_failure_blocks_only_its_domain(settings, project, change):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    if change == 'common_file':
        (project / 'data/knowledge/diagnostics/manuals/DX-GEN-001.md').unlink()
    elif change == 'mapping':
        path = project / 'data/knowledge/diagnostics/part-type-map.json'
        path.write_bytes(path.read_bytes() + b'\n')
    else:
        (project / 'data/knowledge/diagnostics/manifest.json').write_text('{}', encoding='utf-8')
    with open_index(settings, project, embedder=embedder) as index:
        calls = embedder.calls
        for mode in ['dense', 'hybrid']:
            result = index.search('不能启动', 'general_diagnostics', scope='draft_preview', mode=mode)
            assert result['status'] == 'no_eligible_documents' and not result['contexts'] and not result['hits']
        assert embedder.calls == calls
        assert index.search('入库', 'inventory_policies', scope='draft_preview')['hits']


def test_change_during_hybrid_query_discards_all_results(settings, project):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    original = embedder.embed
    def changing(texts, **kwargs):
        path = project / 'data/knowledge/inventory/policies/INV-ADJ-008.md'
        path.write_bytes(path.read_bytes()+b'\n')
        return original(texts, **kwargs)
    embedder.embed = changing
    with open_index(settings, project, embedder=embedder) as index:
        with pytest.raises(KnowledgeError, match='结果已丢弃'):
            index.search('入库', 'inventory_policies', scope='draft_preview', mode='hybrid')


def test_parent_budget_and_required_common_rules_never_truncate_text(settings, project, monkeypatch):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        result = index.search('INV-MDM-002-C005', 'inventory_policies', scope='draft_preview', mode='hybrid', top_k=1)
        assert result['context_characters'] <= 12000
        assert all(c['context_text'] == index.contexts[c['context_id']]['context_text'] for c in result['contexts'])
        assert any(c['expansion_reason'] == 'explicit_reference' for c in result['contexts'])
        monkeypatch.setattr('backend.app.retrieval.CONTEXT_BUDGET', 1)
        result = index.search('INV-MDM-002-C005', 'inventory_policies', scope='draft_preview', mode='hybrid')
        assert result['status'] == 'insufficient_context' and result['omitted_contexts'] and not result['hits']
        with pytest.raises(KnowledgeError, match='公共规则超出'):
            index.search('不能启动', 'general_diagnostics', scope='draft_preview', mode='hybrid')


def test_recovery_and_historical_version_reuse_are_checked(settings, project):
    embedder = FakeEmbedding(settings)
    first = build_index(settings, project, embedder=embedder)
    assert activate_generation(settings, first['generation_id'], project)['embedding_requests'] == 0
    # A failed unpublished directory left before its first report must not block retry.
    (project / ('data/chroma/rag/generations/' + 'a'*32)).mkdir()
    edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('version: 1.0-draft.1', 'version: 2.0'), {'version': '2.0'}))
    second = build_index(settings, project, embedder=embedder)
    pointer = project / 'data/chroma/rag/active.json'
    previous = pointer.read_bytes()
    with pytest.raises(KnowledgeError):
        activate_generation(settings, first['generation_id'], project)
    assert pointer.read_bytes() == previous
    edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('version: 2.0', 'version: 1.0-draft.1')+'\n不同正文', {'version': '1.0-draft.1'}))
    with pytest.raises(KnowledgeError) as error:
        build_index(settings, project, embedder=embedder)
    assert error.value.code == 'document_version_reused' and pointer.read_bytes() == previous
    assert second['generation_id'] != first['generation_id']


def test_release_expiry_is_rechecked_without_an_index_rebuild(settings, project, monkeypatch):
    today = date.today()
    edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('pending_approval', 'approved')
        .replace('effective_from: null', f'effective_from: {today-timedelta(days=1)}'), {
            'approval_status': 'approved', 'effective_from': str(today-timedelta(days=1)),
            'effective_until': str(today), 'default_retrieval_enabled': True,
            'approved_by': 'isolated-test-reviewer', 'approval_reference': 'isolated-test-only'}))
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        result = index.search('退库', 'inventory_policies', mode='hybrid')
        assert result['hits'] and {u['document_id'] for u in result['hits'] + result['contexts']} == {'INV-ADJ-008'}
        class NextDay(date):
            @classmethod
            def today(cls):
                return today + timedelta(days=1)
        monkeypatch.setattr('backend.app.knowledge.date', NextDay)
        calls = embedder.calls
        result = index.search('退库', 'inventory_policies', mode='hybrid')
        assert result['status'] == 'no_eligible_documents' and embedder.calls == calls
        assert {'document_id': 'INV-ADJ-008', 'reason': 'not_effective'} in result['unavailable_documents']


def test_failed_document_update_keeps_unaffected_old_sources_then_replaces_old_units(settings, project):
    embedder = FakeEmbedding(settings)
    first = build_index(settings, project, embedder=embedder)
    pointer = project / 'data/chroma/rag/active.json'
    before = pointer.read_bytes()
    with open_index(settings, project) as index:
        old_chunks = {u['chunk_id'] for u in index.units.values() if u['document_id'] == 'INV-ADJ-008'}
    edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('version: 1.0-draft.1', 'version: 2.0'), {'version': '2.0'}))
    with pytest.raises(KnowledgeError):
        build_index(settings, project, embedder=FakeEmbedding(settings, fail=True))
    assert pointer.read_bytes() == before
    with open_index(settings, project, embedder=embedder) as index:
        result = index.search('入库', 'inventory_policies', mode='hybrid', scope='draft_preview')
        assert result['hits'] and all(h['document_id'] != 'INV-ADJ-008' for h in result['hits'] + result['contexts'])
    second = build_index(settings, project, embedder=embedder)
    assert second['generation_id'] != first['generation_id']
    with open_index(settings, project, embedder=embedder) as index:
        assert not old_chunks.intersection(index.units)
        result = index.search('INV-ADJ-008-C001', 'inventory_policies', scope='draft_preview', mode='hybrid')
        assert result['hits'][0]['document_version'] == '2.0'
        assert index.search('退库', 'inventory_policies', scope='draft_preview', mode='hybrid',
            document_versions={'INV-ADJ-008': '1.0-draft.1'})['status'] == 'no_eligible_documents'


def test_failure_during_pointer_publication_preserves_previous_generation(settings, project, monkeypatch):
    build_index(settings, project, embedder=FakeEmbedding(settings))
    pointer = project / 'data/chroma/rag/active.json'
    before = pointer.read_bytes()
    edit_document(project, 'INV-ADJ-008', lambda t: (t.replace('version: 1.0-draft.1', 'version: 2.0'), {'version': '2.0'}))
    import os
    replace = os.replace
    def fail_publish(source, target):
        if str(target).endswith('active.json'):
            raise OSError('isolated simulated cutover failure')
        replace(source, target)
    monkeypatch.setattr('backend.app.retrieval.os.replace', fail_publish)
    with pytest.raises(KnowledgeError) as error:
        build_index(settings, project, embedder=FakeEmbedding(settings))
    assert error.value.code == 'index_build_failed' and pointer.read_bytes() == before


def test_vector_store_failure_is_explicit_and_does_not_expose_paths_or_fake_hybrid_results(settings, project, monkeypatch):
    from chromadb.errors import InternalError
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        def failed(*args, **kwargs):
            raise InternalError('sensitive-local-path-sentinel')
        monkeypatch.setattr(index.client, 'get_collection', failed)
        for mode in ['dense', 'hybrid']:
            with pytest.raises(KnowledgeError) as error:
                index.search('入库', 'inventory_policies', scope='draft_preview', mode=mode)
            assert error.value.code == 'index_query_failed' and 'sentinel' not in str(error.value)


def test_removed_registration_is_absent_from_the_new_generation_and_rebuild_is_idempotent(settings, project):
    embedder = FakeEmbedding(settings)
    first = build_index(settings, project, embedder=embedder)
    path = project / 'data/knowledge/inventory/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    removed = next(d for d in manifest['documents'] if d['document_id'] == 'INV-ADJ-008')
    manifest['documents'].remove(removed)
    path.write_text(json.dumps(manifest), encoding='utf-8')
    (path.parent / removed['path']).unlink()
    second = build_index(settings, project, embedder=embedder)
    assert second['unit_count'] == first['unit_count'] - len(removed['clause_ids'])
    calls = embedder.calls
    assert build_index(settings, project, embedder=embedder)['reused'] and embedder.calls == calls
    with open_index(settings, project, embedder=embedder) as index:
        assert all(u['document_id'] != 'INV-ADJ-008' for u in index.units.values())
        result = index.search('INV-ADJ-008-C001', 'inventory_policies', scope='draft_preview', mode='hybrid')
        assert result['status'] == 'exact_not_found' and not result['hits']
