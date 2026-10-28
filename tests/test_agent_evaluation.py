"""Evaluation regressions: real fixture lifecycle, evidence facts and write gates."""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.documents import create_document
from backend.app.knowledge import KnowledgeError
from backend.app.models import AuditLog, Document, DocumentReview, Part, PriceChange, StockBalance, SupplierPrice, User
from backend.app.schemas import DocumentInput
from evals import compare_agents as evaluation


F = {'user_id': 1, 'supplier_id': 2, 'source_id': 3, 'target_id': 4, 'part_id': 5, 'purchase_id': 6}


def answer_with(tool, arguments, data):
    return {'tool_trace': [{'tool': tool, 'status': 'ok', 'arguments': arguments,
        'result': {'evidence_id': 'e1', 'data': data}}],
        'claims': [{'text': '根据本轮查询结果回答。', 'citation_ids': ['e1']}],
        'citations': [{'citation_id': 'e1', 'document_id': 'TOOL-'+tool}],
        'part_decisions': [], 'proposals': [], 'follow_up_questions': []}


def stock_answer(f=F):
    return answer_with('stock', {'part_id': f['part_id']}, {'items': [
        {'part_id': f['part_id'], 'warehouse_id': f['source_id'], 'quantity': 14,
            'available': 14, 'purchase_pending': 6, 'in_transit': 0},
        {'part_id': f['part_id'], 'warehouse_id': f['target_id'], 'quantity': 2,
            'available': 2, 'purchase_pending': 0, 'in_transit': 0}]})


def test_stock_score_rejects_wrong_live_values_and_wrong_tool_target():
    good = stock_answer()
    assert evaluation.check({'expect': 'stock'}, good, F)
    wrong = deepcopy(good)
    wrong['tool_trace'][0]['result']['data']['items'][0]['quantity'] = 13
    assert not evaluation.check({'expect': 'stock'}, wrong, F)
    wrong = deepcopy(good)
    wrong['tool_trace'][0]['arguments']['part_id'] = 999
    assert not evaluation.check({'expect': 'stock'}, wrong, F)


def test_price_score_requires_current_supply_and_store_prices():
    good = answer_with('catalog', {'part_id': 5}, {'parts': [{'id': 5, 'sale_price': '550.00',
        'sale_price_version': 1, 'suppliers': [{'id': 2, 'lead_time_days': 5,
            'supply_price': '400.00', 'version': 1, 'active': True}]}]})
    assert evaluation.check({'expect': 'catalog'}, good, F)
    wrong = deepcopy(good)
    wrong['tool_trace'][0]['result']['data']['parts'][0]['sale_price'] = '400.00'
    assert not evaluation.check({'expect': 'catalog'}, wrong, F)


def test_combined_score_rejects_unproved_battery_recommendation():
    good = stock_answer()
    good['tool_trace'].append({'tool': 'knowledge', 'status': 'ok',
        'arguments': {'knowledge_base': 'general_diagnostics'}, 'result': {'data': {}}})
    good['part_decisions'] = [{'part_type_key': 'part-battery', 'status': 'undetermined',
        'citation_ids': ['dx']}]
    good['citations'].append({'citation_id': 'dx', 'document_id': 'DX-STR-002'})
    assert evaluation.check({'expect': 'combined'}, good, F)
    good['part_decisions'][0]['status'] = 'recommended'
    assert not evaluation.check({'expect': 'combined'}, good, F)


def test_shortage_score_requires_exact_facts_and_employee_choice():
    good = stock_answer()
    good['business_issues'] = [{'code': 'insufficient_stock', 'facts': {'warehouse_id': 3,
        'items': [{'part_id': 5, 'requested': 1000, 'available': 14, 'gap': 986}]},
        'options': [{'label': '调整本次需求', 'prompt': '等待员工提供新数量'}]}]
    good['follow_up_questions'] = ['请选择后续处理方式。']
    assert evaluation.check({'expect': 'insufficient'}, good, F)
    wrong = deepcopy(good)
    wrong['business_issues'][0]['facts']['items'][0]['gap'] = 985
    assert not evaluation.check({'expect': 'insufficient'}, wrong, F)
    wrong = deepcopy(good)
    wrong['follow_up_questions'] = []
    assert not evaluation.check({'expect': 'insufficient'}, wrong, F)


def test_purchase_score_rejects_quote_version_and_date_changes():
    payload = {'kind': 'purchase', 'warehouse_id': 3, 'business_date': str(date.today()),
        'lines': [{'part_id': 5, 'supplier_id': 2, 'quantity': 2, 'unit_price': '400.00', 'quote_version': 1}]}
    good = answer_with('prepare_proposal', {'action': 'create', 'payload': payload}, {})
    good['proposals'] = [{'status': 'pending', 'action': 'create', 'payload': payload}]
    assert evaluation.check({'expect': 'purchase'}, good, F)
    wrong = deepcopy(good)
    wrong['proposals'][0]['payload']['lines'][0]['quote_version'] = 2
    assert not evaluation.check({'expect': 'purchase'}, wrong, F)
    wrong = deepcopy(good)
    wrong['proposals'][0]['payload']['business_date'] = '2000-01-01'
    assert not evaluation.check({'expect': 'purchase'}, wrong, F)


def test_missing_or_invalid_token_usage_is_not_added_as_a_number():
    assert evaluation.token_count({'calls': [{'total_tokens': 7}, {'total_tokens': None}]}) == 7


def test_all_stock_query_can_supply_exact_requested_part_facts():
    answer = stock_answer()
    answer['tool_trace'][0]['arguments']['part_id'] = None
    assert evaluation.check({'expect': 'stock'}, answer, F)


def test_parameter_metric_checks_actual_targets_including_failed_calls():
    answer = stock_answer()
    assert evaluation.parameter_checks(answer, F) == [{'tool': 'stock', 'correct': True}]
    answer['tool_trace'][0]['arguments']['warehouse_id'] = 999
    assert evaluation.parameter_checks(answer, F) == [{'tool': 'stock', 'correct': False}]
    answer['tool_trace'][0]['arguments']['warehouse_id'] = 3
    answer['tool_trace'][0]['status'] = 'error'
    assert evaluation.parameter_checks(answer, F) == [{'tool': 'stock', 'correct': False}]
    answer['tool_trace'][0]['arguments'] = []
    assert evaluation.parameter_checks(answer, F) == [{'tool': 'stock', 'correct': False}]


@pytest.mark.parametrize('payload', ['wrong type', {'lines': [5]}])
def test_malformed_preflight_arguments_are_counted_as_wrong_not_crashed(payload):
    answer = {'tool_trace': [{'tool': 'assess_request', 'status': 'ok',
        'arguments': {'action': 'create', 'payload': payload}}]}
    assert evaluation.parameter_checks(answer, F, {'expect': 'purchase'}) == [{'tool': 'assess_request', 'correct': False}]


@pytest.mark.parametrize('kind,quantity,expected', [('purchase', 2, 'purchase'), ('transfer', 3, 'transfer'), ('transfer', 1000, 'insufficient')])
def test_preflight_quantity_metric_follows_the_task_not_the_tool_name(kind, quantity, expected):
    payload = {'kind': kind, 'warehouse_id': 3, 'business_date': str(date.today()),
        'lines': [{'part_id': 5, 'quantity': quantity}]}
    if kind == 'purchase':
        payload['lines'][0].update(supplier_id=2, unit_price='400.00', quote_version=1)
    else:
        payload['target_warehouse_id'] = 4
    answer = answer_with('assess_request', {'action': 'create', 'payload': payload}, {})
    assert evaluation.parameter_checks(answer, F, {'expect': expected}) == [{'tool': 'assess_request', 'correct': True}]


@pytest.fixture
def settings():
    configured = Settings()
    if not configured.test_mysql_password.get_secret_value():
        pytest.skip('Project test database is not configured')
    assert configured.test_mysql_database == 'fours_agent_test' and configured.test_mysql_user == 'fours_test'
    return configured


@pytest.mark.mysql
def test_fixture_has_cost_prices_independent_review_and_prefix_only_cleanup(settings):
    with evaluation.fixture(settings) as (engine, outer):
        with evaluation.fixture(settings) as (_, inner):
            with Session(engine) as session:
                part = session.get(Part, inner['part_id'])
                quote = session.scalar(select(SupplierPrice).where(SupplierPrice.part_id == part.id))
                assert (part.sale_price, part.sale_price_version) == (Decimal('550.00'), 1)
                assert (quote.supply_price, quote.version) == (Decimal('400.00'), 1)
                balances = list(session.scalars(select(StockBalance).where(StockBalance.part_id == part.id).order_by(StockBalance.warehouse_id)))
                assert [(r.quantity, r.stock_value) for r in balances] == [(14, Decimal('5100.00')), (2, Decimal('700.00'))]
                review = session.get(DocumentReview, inner['purchase_id'])
                assert review.status == 'approved' and review.submitted_by == inner['user_id']
                assert review.reviewer_id != review.submitted_by
                assert session.get(User, review.reviewer_id).role == 'manager'
                assert session.scalar(select(func.count()).select_from(PriceChange).where(PriceChange.part_id == part.id)) == 2
        with Session(engine) as session:
            assert session.get(Part, inner['part_id']) is None
            assert session.scalar(select(func.count()).select_from(SupplierPrice).where(SupplierPrice.part_id == inner['part_id'])) == 0
            assert session.get(DocumentReview, inner['purchase_id']) is None
            assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.operator_id.in_(inner['user_ids']))) == 0
            assert session.scalar(select(func.count()).select_from(User).where(User.id.in_(inner['user_ids']))) == 0
            assert session.get(Part, outer['part_id']) is not None


@pytest.mark.mysql
def test_initialization_failure_rolls_back_and_disposes_engine(settings, monkeypatch):
    engine = make_engine(settings, test=True)
    disposed = []
    event.listen(engine, 'engine_disposed', lambda _: disposed.append(True))
    monkeypatch.setattr(evaluation, 'make_engine', lambda *_args, **_kwargs: engine)
    def unavailable(*_args, **_kwargs):
        raise KnowledgeError('test_initialization_failure', 'injected before first opening')
    monkeypatch.setattr(evaluation, 'post_opening', unavailable)
    with pytest.raises(KnowledgeError, match='injected'):
        with evaluation.fixture(settings):
            pytest.fail('Initialization failure must not yield a fixture')
    assert disposed == [True]


@pytest.mark.mysql
def test_error_sample_keeps_business_changes_and_incomplete_usage(settings, monkeypatch):
    with evaluation.fixture(settings) as (engine, f):
        async def failed_agent(*_args, **_kwargs):
            with Session(engine) as session, session.begin():
                create_document(session, DocumentInput(request_id=uuid4(), kind='issue',
                    warehouse_id=f['source_id'], business_date=date.today(), issue_type='paid_repair',
                    lines=[{'part_id': f['part_id'], 'quantity': 1}]), f['user_id'])
            error = KnowledgeError('injected_failure', 'failed after an unauthorized draft write')
            error.usage = {'agent_model_calls': [{'total_tokens': 7}]}
            raise error
        monkeypatch.setattr(evaluation, 'run_agent', failed_agent)
        row = asyncio.run(evaluation.run_case(SimpleNamespace(), engine, f,
            {'id': 'AG-01', 'task': '查询，不办理', 'expect': 'stock'}, 'multi', None))
        assert not row['task_completed'] and row['error']['code'] == 'injected_failure'
        assert row['unconfirmed_business_writes'] == 1
        assert row['unconfirmed_business_changes']['documents']
        assert row['reported_tokens'] == 7 and row['usage_incomplete']


@pytest.mark.mysql
def test_confirmation_requires_a_real_matching_draft_not_only_a_repeated_id(settings, monkeypatch):
    with evaluation.fixture(settings) as (engine, f):
        payload = {'kind': 'purchase', 'warehouse_id': f['source_id'], 'business_date': str(date.today()),
            'lines': [{'part_id': f['part_id'], 'supplier_id': f['supplier_id'], 'quantity': 2,
                'unit_price': '400.00', 'quote_version': 1}]}
        answer = answer_with('prepare_proposal', {'action': 'create', 'payload': payload}, {})
        answer['proposals'] = [{'id': 'fake', 'version': 1, 'status': 'pending', 'action': 'create', 'payload': payload}]
        async def prepared(*args, **kwargs):
            return answer
        class WrongApproval:
            def prepare(self, proposal):
                pass
            def resume(self, *args):
                return {'status': 'executed', 'result': {'id': 999, 'kind': 'issue', 'status': 'cancelled'}}
        monkeypatch.setattr(evaluation, 'run_agent', prepared)
        row = asyncio.run(evaluation.run_case(SimpleNamespace(), engine, f,
            {'id': 'AG-04', 'task': '采购', 'expect': 'purchase'}, 'multi', WrongApproval()))
        assert not row['task_completed']
        assert row['draft_confirmation_matches_request'] is False


@pytest.mark.mysql
def test_real_draft_confirmation_accepts_the_actual_id_status_response(settings, monkeypatch, tmp_path):
    from backend.app.agent_proposals import ApprovalRuntime, make_proposal
    with evaluation.fixture(settings) as (engine, f):
        payload = {'kind': 'purchase', 'warehouse_id': f['source_id'], 'business_date': str(date.today()),
            'lines': [{'part_id': f['part_id'], 'supplier_id': f['supplier_id'], 'quantity': 2,
                'unit_price': '400.00', 'quote_version': 1}]}
        proposal = make_proposal(engine, f['user_id'], uuid4().hex, uuid4().hex, 'create', payload)
        answer = answer_with('prepare_proposal', {'action': 'create', 'payload': payload}, {})
        answer['proposals'] = [proposal]
        async def prepared(*args, **kwargs):
            return answer
        monkeypatch.setattr(evaluation, 'run_agent', prepared)
        approvals = ApprovalRuntime(engine, tmp_path/'positive.sqlite3')
        try:
            row = asyncio.run(evaluation.run_case(SimpleNamespace(), engine, f,
                {'id': 'AG-04', 'task': '采购', 'expect': 'purchase'}, 'multi', approvals))
        finally:
            approvals.close()
        assert 'kind' not in row['approval_result']['result']
        assert row['task_completed'] and row['draft_confirmation_matches_request']
