from evals.run_acceptance import coverage, summarize


def case(*clauses):
    return {'evidence_groups': [{'any_of': [{'clause_id': unit, 'document_version': 'v2'}]} for unit in clauses]}


def test_retrieval_failures_remain_in_fixed_denominators():
    rows = [{'suite': 'inventory', 'rubric': case('A', 'B'), 'error': {'code': 'embedding_timeout'}, 'retrieval': {}},
        {'suite': 'inventory', 'rubric': case('C'), 'answer': {}, 'retrieval': {
            'hybrid': {'raw_hit': 1, 'expanded_hit': 1}, 'dense': {'raw_hit': 0, 'expanded_hit': 1}}},
        {'suite': 'inventory', 'rubric': case(), 'answer': {}, 'retrieval': {}}]
    result = summarize(rows, 'inventory')
    assert (result['questions'], result['errors']) == (3, 1)
    assert result['hybrid'] == {'target_questions': 2, 'expected_groups': 3, 'raw_groups_hit': 1,
        'expanded_groups_hit': 1, 'raw_complete_questions': 1, 'expanded_complete_questions': 1}
    assert result['dense']['expected_groups'] == 3 and result['dense']['raw_complete_questions'] == 0


def test_same_clause_id_from_an_old_version_is_not_an_evidence_hit():
    rubric = case('A')
    found = {'hits': [{'unit_id': 'A', 'document_version': 'v1'}],
        'contexts': [{'unit_ids': ['A'], 'document_version': 'v1'}]}
    assert coverage(rubric, 'inventory', found)['raw_hit'] == 0
    assert coverage(rubric, 'inventory', found)['expanded_hit'] == 0
    found['hits'][0]['document_version'] = 'v2'
    found['contexts'][0]['document_version'] = 'v2'
    assert coverage(rubric, 'inventory', found)['raw_hit'] == 1
    assert coverage(rubric, 'inventory', found)['expanded_hit'] == 1


def test_failed_diagnostic_query_retains_symptom_target_but_not_common_rules():
    rows = [{'suite': 'diagnostics', 'rubric': {'expected_sections': ['DX-A-S01', 'DX-RULE-01']},
        'retrieval': {}, 'error': {'code': 'index_query_failed'}}]
    result = summarize(rows, 'diagnostics')
    assert result['dense']['expected_groups'] == result['hybrid']['expected_groups'] == 1
    assert result['dense']['target_questions'] == 1 and result['dense']['raw_groups_hit'] == 0
