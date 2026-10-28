from datetime import date, timedelta
from uuid import uuid4

import pytest

from test_m1 import opening_body, resources, system
from test_m2 import action, call, create, detail

pytestmark = pytest.mark.mysql


def test_all_parts_statistics_uses_piece_units_and_respects_warehouse(system):
    client, tokens, _, prefix, _ = system
    source, _ = resources(system, count=0)
    target, _ = resources(system, count=0)
    parts = [call(system, '/api/parts', {'sku': f'{prefix}-ALL-{i}', 'name': f'汇总备件{i}',
        'unit': unit, 'safety_stock': 10}, role='admin', expected=201)
        for i, unit in enumerate(['件'] * 5)]
    call(system, '/api/inventory/openings', opening_body(source, parts))
    call(system, '/api/inventory/openings', opening_body(target, parts))
    supplier = call(system, '/api/suppliers', {'code': prefix+'-ALL', 'name': '汇总供应商'}, role='admin', expected=201)
    purchase = create(system, 'purchase', target, parts, supplier_id=supplier['id'])
    action(system, purchase['id'], 'confirm')
    issue = create(system, 'issue', source, parts, lines=[{'part_id': p['id'], 'quantity': 1} for p in parts])
    action(system, issue['id'], 'post')
    second_issue = create(system, 'issue', source, [parts[2]],
        lines=[{'part_id': parts[2]['id'], 'quantity': 1}])
    action(system, second_issue['id'], 'post')
    call(system, f"/api/documents/{purchase['id']}/receipts", {
        'request_id': str(uuid4()), 'business_date': str(date.today()),
        'lines': [{'purchase_line_id': line['id'], 'quantity': 1}
            for line in detail(system, purchase['id'])['lines']]}, expected=201)
    transfer = create(system, 'transfer', source, [parts[0]], target_warehouse_id=target['id'],
        lines=[{'part_id': parts[0]['id'], 'quantity': 1}])
    action(system, transfer['id'], 'send')
    # Draft purchases and unsent transfers do not count as incoming stock.
    create(system, 'purchase', source, parts, supplier_id=supplier['id'])
    create(system, 'transfer', target, [parts[0]], target_warehouse_id=source['id'])

    def query(**params):
        response = client.get('/api/statistics', headers=tokens['manager'], params=params)
        assert response.status_code == 200, response.text
        return response.json()

    all_parts = query()
    assert all_parts['part_id'] is None and all_parts['warehouse_id'] is None
    groups = {g['unit']: g for g in all_parts['groups']}
    assert set(groups) == {'件'}
    assert groups['件']['totals'] == {'on_hand': 48, 'purchase_pending': 20, 'in_transit': 1}
    assert {r['part_id'] for r in groups['件']['low_stock']} == {p['id'] for p in parts}
    low_rows = {(row['warehouse_id'], row['part_id']): row for row in groups['件']['low_stock']}
    for item in parts:
        source_row = low_rows[(source['id'], item['id'])]
        target_row = low_rows[(target['id'], item['id'])]
        assert source_row['purchase_pending'] == 0 and source_row['in_transit'] == 0
        assert target_row['purchase_pending'] == 4  # Five ordered minus one actually received.
        assert target_row['in_transit'] == (1 if item['id'] == parts[0]['id'] else 0)
    assert {r['sku'] for r in groups['件']['consumption_ranking']} == {p['sku'] for p in parts}
    assert next(r['quantity'] for r in groups['件']['trend'] if r['event'] == 'issue') == -6
    assert all_parts['unit'] == '件'
    assert {r['event']: r['quantity'] for r in all_parts['trend'] if r['event'] in {'receipt', 'issue'}} == {
        'receipt': 5, 'issue': -6}

    assert {r['event']: r['quantity'] for r in all_parts['activity_trend']} == {'issue': 2, 'receipt': 1}
    assert len(all_parts['consumption_ranking']) == len(parts)
    assert all_parts['consumption_ranking'][0]['sku'] == parts[2]['sku']
    assert all_parts['consumption_ranking'][0]['issue_count'] == 2
    assert all_parts['consumption_ranking'][0]['quantity'] == 2
    assert all_parts['consumption_ranking'][0]['unit'] == '件'

    target_groups = {g['unit']: g for g in query(warehouse_id=target['id'])['groups']}
    assert target_groups['件']['totals'] == {'on_hand': 30, 'purchase_pending': 20, 'in_transit': 1}
    assert all(not any(row['event'] == 'issue' for row in g['trend'])
        and not g['consumption_ranking'] for g in target_groups.values())
    assert query(warehouse_id=target['id'])['activity_trend'] == [
        {'business_date': str(date.today()), 'event': 'receipt', 'quantity': 1}]
    assert [r['quantity'] for r in query(warehouse_id=target['id'])['trend'] if r['event'] == 'receipt'] == [5]
    single = query(part_id=parts[2]['id'], warehouse_id=source['id'])
    assert single['unit'] == '件' and single['totals']['on_hand'] == 3
    assert single['activity_trend'] == [{'business_date': str(date.today()), 'event': 'issue', 'quantity': 2}]
    assert [r['quantity'] for r in single['trend'] if r['event'] == 'issue'] == [-2]
    assert len(single['groups']) == 1
    assert query(unit='件')['totals'] == groups['件']['totals']
    assert client.get('/api/parts', headers=tokens['manager']).status_code == 200
    assert client.get('/api/statistics', headers=tokens['manager'], params={'unit': '桶'}).status_code == 422
    yesterday = str(date.today() - timedelta(days=1))
    empty = query(date_from=yesterday, date_to=yesterday)
    assert empty['activity_trend'] == [] and empty['consumption_ranking'] == []
    assert not [r for r in empty['trend'] if r['event'] in {'receipt', 'issue'}]
    assert {g['unit']: g['totals'] for g in empty['groups']} == {u: g['totals'] for u, g in groups.items()}
    assert client.get('/api/statistics', headers=tokens['manager'], params={'date_from': str(date.today()), 'date_to': '2000-01-01'}).status_code == 422


def test_zero_stock_includes_new_catalog_without_threshold_or_opening(system):
    client, tokens, _, prefix, _ = system
    warehouse, _ = resources(system, count=0)
    part = call(system, '/api/parts', {'sku': prefix+'-ZERO-NEW', 'name': '新增未建账备件', 'unit': '件', 'safety_stock': 0}, role='admin', expected=201)
    params = {'warehouse_id': warehouse['id'], 'part_id': part['id']}
    response = client.get('/api/statistics', headers=tokens['manager'], params=params)
    assert response.status_code == 200
    data = response.json()
    assert data['low_stock'] == [] and len(data['zero_stock']) == 1
    assert data['zero_stock'][0]['quantity'] == 0 and data['zero_stock'][0]['initialized'] is False
    assert data['totals'] == {'on_hand': 0, 'purchase_pending': 0, 'in_transit': 0}
    from backend.app.schemas import PartInput
    payload = PartInput.model_validate({k: v for k, v in part.items() if k in PartInput.model_fields}).model_dump(mode='json')
    response = client.put(f"/api/parts/{part['id']}", headers=tokens['admin'], json={**payload, 'active': False})
    assert response.status_code == 200, response.text
    assert client.get('/api/statistics', headers=tokens['manager'], params=params).json()['zero_stock'] == []


def test_safety_stock_configuration_preserves_nonzero_thresholds_prices_and_stock(system):
    from decimal import Decimal
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from backend.app.models import AuditLog, Part, StockBalance, StockLedger, User
    from backend.configure_safety_stock import fill_missing
    _, _, engine, prefix, _ = system
    with Session(engine) as session:
        operator = session.scalar(select(User).where(User.username == prefix.lower()+'_admin'))
        before = [session.scalar(select(func.count()).select_from(model)) for model in (StockBalance, StockLedger)]
        empty = Part(sku='CAT-'+prefix+'-A', name='测试总成', unit='件', safety_stock=0, reference_price=Decimal('123.45'))
        maintained = Part(sku='CAT-'+prefix+'-B', name='人工阈值', unit='件', safety_stock=9, reference_price=Decimal('678.90'))
        session.add_all([empty, maintained]); session.flush()
        rows = [{'sku': empty.sku, 'safety_stock': 1}, {'sku': maintained.sku, 'safety_stock': 2}]
        result = fill_missing(session, operator.id, rows)
        assert len(result['changes']) == 1 and empty.safety_stock == 1 and maintained.safety_stock == 9
        assert (empty.reference_price, maintained.reference_price) == (Decimal('123.45'), Decimal('678.90'))
        assert not fill_missing(session, operator.id, rows)['changes']
        assert [session.scalar(select(func.count()).select_from(model)) for model in (StockBalance, StockLedger)] == before
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == empty.id, AuditLog.action == 'part.safety_stock.configure')) == 1
        session.rollback()  # This isolated test commits neither catalog rows nor audits.


def test_issue_frequency_returns_all_parts_for_frontend_pagination(system):
    client, tokens, _, _, _ = system
    warehouse, parts = resources(system, count=12)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    issue = create(system, 'issue', warehouse, parts, lines=[{'part_id': part['id'], 'quantity': 1} for part in parts])
    action(system, issue['id'], 'post')
    response = client.get('/api/statistics', headers=tokens['manager'], params={'warehouse_id': warehouse['id']})
    assert response.status_code == 200, response.text
    ranking = response.json()['consumption_ranking']
    assert len(ranking) == 12  # A top-ten API truncation would hide the final page.
    assert [row['sku'] for row in ranking] == [part['sku'] for part in parts]
    assert all(row['issue_count'] == 1 and row['quantity'] == 1 and row['unit'] == '件' for row in ranking)
