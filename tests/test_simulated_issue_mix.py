"""A user-authorized alternate simulated mix must preserve physical and cost facts."""
from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend import rebalance_simulated_issues as mix
from backend.backfill_pricing import apply_plan as price_history, make_plan as price_plan, snapshot
from backend.app.models import Document, PricingBackfill
from tests.test_pricing_backfill import history


def prepare(history, monkeypatch):
    session, user, part, _, _, _, _, used, _ = history
    price_history(session, price_plan(session), user.id)
    document = session.get(Document, used.document_id)
    document.issue_type = 'warranty'
    document.warranty_reason = '模拟历史质保维修补录，无真实质保凭证'
    used.sale_unit_price = Decimal('0.00')
    session.flush()
    monkeypatch.setattr(mix, 'fixed_index', lambda key, count: 10)
    return session, user, part, document, used


@pytest.mark.parametrize('history', ['sqlite', 'mysql'], indirect=True)
def test_plan_read_only_apply_preserves_costs_and_all_quantity_records(history, monkeypatch):
    session, user, part, document, used = prepare(history, monkeypatch)
    before = snapshot(session)
    plan = mix.make_plan(session)
    assert plan == mix.make_plan(session) and snapshot(session) == before
    assert plan['summary']['converted_documents'] == 1
    assert plan['summary']['converted_lines'] == 1
    mix.apply_plan(session, plan, user.id)
    assert document.issue_type == 'paid_repair' and document.warranty_reason == ''
    assert used.sale_unit_price > 0 and used.cost_amount == Decimal('300.00')
    assert used.pricing_source == document.pricing_source == mix.SOURCE
    after = snapshot(session)
    for table in ['stock_balances', 'stock_ledger', 'opening_lines', 'parts', 'supplier_prices', 'document_reviews']:
        assert after[table] == before[table]
    assert used.unit_price == 0
    for old, new in zip(before['document_lines'], after['document_lines'], strict=True):
        assert {k: v for k, v in old.items() if k not in {'sale_unit_price', 'pricing_source'}} == {
            k: v for k, v in new.items() if k not in {'sale_unit_price', 'pricing_source'}}


def test_preserves_manually_created_or_modified_warranty_and_seed_retained_cases(history, monkeypatch):
    session, user, part, document, used = prepare(history, monkeypatch)
    document.pricing_source = '员工登记'
    session.flush()
    assert mix.make_plan(session)['summary']['converted_documents'] == 0
    document.pricing_source = mix.ORIGINAL_SOURCE
    used.pricing_source = '员工修正'
    session.flush()
    assert mix.make_plan(session)['summary']['converted_documents'] == 0
    used.pricing_source = mix.ORIGINAL_SOURCE
    session.flush()
    monkeypatch.setattr(mix, 'fixed_index', lambda key, count: 2)
    assert mix.make_plan(session)['summary']['converted_documents'] == 0


def test_stale_and_modified_plans_rejected_without_partial_write(history, monkeypatch):
    session, user, part, document, used = prepare(history, monkeypatch)
    plan = mix.make_plan(session)
    changed = deepcopy(plan)
    changed['changes'][0]['after']['issue_type'] = 'invalid'
    with pytest.raises(ValueError, match='digest'):
        mix.apply_plan(session, changed, user.id)
    part.sale_price += Decimal('1')
    session.flush()
    with pytest.raises(ValueError, match='baseline'):
        mix.apply_plan(session, plan, user.id)
    assert document.issue_type == 'warranty' and used.sale_unit_price == 0


def test_same_version_replay_does_not_overwrite_later_manual_changes(history, monkeypatch):
    session, user, part, document, used = prepare(history, monkeypatch)
    plan = mix.make_plan(session)
    summary = mix.apply_plan(session, plan, user.id)
    used.sale_unit_price = Decimal('123.45')
    session.flush()
    assert mix.apply_plan(session, plan, user.id) == summary
    assert used.sale_unit_price == Decimal('123.45')
    assert len(session.scalars(select(PricingBackfill).where(PricingBackfill.version == mix.VERSION)).all()) == 1


def test_failed_execution_rolls_back_every_reclassification(history, monkeypatch):
    session, user, part, document, used = prepare(history, monkeypatch)
    plan = mix.make_plan(session)
    original_flush = session.flush

    def fail_on_record(*args, **kwargs):
        if any(isinstance(row, PricingBackfill) and row.version == mix.VERSION for row in session.new):
            raise RuntimeError('simulated write failure')
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, 'flush', fail_on_record)
    with pytest.raises(RuntimeError, match='write failure'):
        mix.apply_plan(session, plan, user.id)
    monkeypatch.setattr(session, 'flush', original_flush)
    assert document.issue_type == 'warranty' and used.sale_unit_price == 0
    assert session.get(PricingBackfill, mix.VERSION) is None
