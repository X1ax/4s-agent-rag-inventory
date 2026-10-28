from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.main import create_app
from backend.app.models import AuditLog, Document, DocumentLine, Part, StockLedger, User
from backend.app.security import session_dependency
from tests.test_m1 import resources, system


def test_price_and_profit_queries_require_authentication():
    app = create_app()
    app.dependency_overrides[session_dependency] = lambda: None
    client = TestClient(app)
    assert client.get('/api/pricing/parts').status_code == 401
    assert client.get('/api/profits').status_code == 401


def supplier(system, category=''):
    client, tokens, _, prefix, _ = system
    response = client.post('/api/suppliers', headers=tokens['admin'], json={
        'code': prefix + '-' + uuid4().hex[:6].upper(), 'name': '测试报价供应商',
        'supply_categories': [category]})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.mysql
def test_independent_supplier_quotes_versions_and_price_permissions(system):
    from backend.app.models import PriceChange, SupplierPrice

    client, tokens, engine, _, _ = system
    _, parts = resources(system, count=1)
    part = parts[0]
    first, second = supplier(system), supplier(system)
    url = f"/api/pricing/suppliers/{first['id']}/parts/{part['id']}"
    payload = {'supply_price': '10.20', 'source': '报价单Q1', 'active': True, 'expected_version': 0}
    for role in ('storekeeper', 'manager'):
        assert client.put(url, headers=tokens[role], json=payload).status_code == 403
    response = client.put(url, headers=tokens['admin'], json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['supply_price'] == '10.20' and response.json()['version'] == 1
    assert client.put(url, headers=tokens['admin'], json=payload).status_code == 409
    other_url = f"/api/pricing/suppliers/{second['id']}/parts/{part['id']}"
    assert client.put(other_url, headers=tokens['admin'], json=payload | {'supply_price': '0.00', 'source': ''}).status_code == 422
    assert client.put(other_url, headers=tokens['admin'], json=payload | {'supply_price': '0.00', 'source': '供应商赠送'}).status_code == 200
    sale_url = f"/api/pricing/parts/{part['id']}/sale-price"
    sale = {'sale_price': '15.50', 'source': '门店定价', 'expected_version': 0}
    assert client.patch(sale_url, headers=tokens['storekeeper'], json=sale).status_code == 403
    assert client.patch(sale_url, headers=tokens['manager'], json=sale | {'sale_price': '0'}).status_code == 422
    assert client.patch(sale_url, headers=tokens['manager'], json=sale | {'name': '越权'}).status_code == 422
    response = client.patch(sale_url, headers=tokens['manager'], json=sale)
    assert response.status_code == 200, response.text
    assert response.json()['sale_price'] == '15.50' and response.json()['sale_price_version'] == 1
    assert client.patch(sale_url, headers=tokens['admin'], json=sale).status_code == 409
    response = client.get('/api/pricing/parts', headers=tokens['storekeeper'], params={'part_id': part['id']})
    prices = {row['id']: row for row in response.json()['items'][0]['suppliers']}
    assert prices[first['id']]['supply_price'] == '10.20'
    assert prices[second['id']]['supply_price'] == '0.00'
    with Session(engine) as session:
        changes = list(session.scalars(select(PriceChange).where(PriceChange.part_id == part['id']).order_by(PriceChange.id)))
        assert [(row.kind, row.before_price, row.after_price) for row in changes] == [
            ('supply', None, Decimal('10.20')), ('supply', None, Decimal('0.00')), ('sale', None, Decimal('15.50'))]
        assert all(row.source and row.operator_id and row.created_at for row in changes)
        price = session.scalar(select(SupplierPrice).where(SupplierPrice.part_id == part['id'], SupplierPrice.supplier_id == first['id']))
        assert session.scalar(select(AuditLog.id).where(AuditLog.entity == 'supplier_prices', AuditLog.entity_id == price.id))
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == 'price.sale', AuditLog.entity_id == part['id']))


@pytest.mark.mysql
def test_missing_quote_and_supply_scope_are_visible_and_concurrent_edits_conflict(system):
    client, tokens, _, _, _ = system
    _, parts = resources(system, count=1)
    part = parts[0]
    eligible, excluded = supplier(system), supplier(system, '其他类别')
    rows = client.get('/api/pricing/parts', headers=tokens['admin'], params={'part_id': part['id']}).json()['items'][0]['suppliers']
    assert next(row for row in rows if row['id'] == eligible['id'])['supply_price'] is None
    assert excluded['id'] not in {row['id'] for row in rows}
    payload = {'supply_price': '2.00', 'source': '报价', 'active': True, 'expected_version': 0}
    assert client.put(f"/api/pricing/suppliers/{excluded['id']}/parts/{part['id']}", headers=tokens['admin'], json=payload).status_code == 409
    url = f"/api/pricing/suppliers/{eligible['id']}/parts/{part['id']}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.put(url, headers=tokens['admin'], json=payload), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]


@pytest.mark.mysql
def test_gross_profit_uses_posting_dates_separates_warranty_and_exposes_gaps(system):
    client, tokens, engine, prefix, _ = system
    warehouse, parts = resources(system, count=3)
    target, _ = resources(system, count=0)
    today = date.today()
    yesterday = today - timedelta(days=1)
    with Session(engine) as session, session.begin():
        operator = session.scalar(select(User.id).where(User.username == prefix.lower() + '_admin'))
        for part in parts:
            session.get(Part, part['id']).category = prefix
        cases = [
            (parts[0], 'paid_repair', 2, '10.00', '12.00', 'posted', yesterday),
            (parts[0], 'warranty', 1, '0.00', '4.00', 'posted', today),
            (parts[1], 'paid_repair', 1, '2.00', '5.00', 'posted', today),
            (parts[1], 'paid_repair', 3, '10.00', None, 'posted', today),
            (parts[1], None, 4, None, '9.00', 'posted', today),
            (parts[2], 'warranty', 2, '0.00', '6.00', 'posted', today),
            (parts[2], 'paid_repair', 100, '10.00', '1.00', 'draft', today),
        ]
        for part, purpose, quantity, price, cost, status, posting_date in cases:
            document = Document(kind='issue', status=status, warehouse_id=warehouse['id'], operator_id=operator,
                business_date=today - timedelta(days=10), issue_type=purpose, pricing_source='模拟补录' if purpose is None else '测试成交')
            session.add(document)
            session.flush()
            line = DocumentLine(document_id=document.id, part_id=part['id'], quantity=quantity,
                sale_unit_price=Decimal(price) if price is not None else None, cost_amount=Decimal(cost) if cost is not None else None)
            session.add(line)
            session.flush()
            session.add(StockLedger(warehouse_id=warehouse['id'], part_id=part['id'], document_line_id=line.id,
                operator_id=operator, event='issue', business_date=posting_date, quantity_delta=-quantity))
        transfer = Document(kind='transfer', status='sent', warehouse_id=warehouse['id'],
            target_warehouse_id=target['id'], operator_id=operator, business_date=today)
        session.add(transfer)
        session.flush()
        transfer_line = DocumentLine(document_id=transfer.id, part_id=parts[0]['id'], quantity=50,
            sale_unit_price=Decimal('100.00'), cost_amount=Decimal('500.00'))
        session.add(transfer_line)
        session.flush()
        session.add(StockLedger(warehouse_id=warehouse['id'], part_id=parts[0]['id'], document_line_id=transfer_line.id,
            operator_id=operator, event='transfer_send', business_date=today, quantity_delta=-50))
    params = {'warehouse_id': warehouse['id'], 'category': prefix, 'date_from': str(yesterday), 'date_to': str(today), 'page_size': 1}
    response = client.get('/api/profits', headers=tokens['manager'], params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    totals = result['totals']
    assert {key: totals[key] for key in ('revenue', 'paid_cost', 'gross_profit', 'warranty_cost', 'contribution')} == {
        'revenue': '52.00', 'paid_cost': '17.00', 'gross_profit': '5.00', 'warranty_cost': '10.00', 'contribution': '-5.00'}
    assert totals['paid_quantity'] == 6 and totals['warranty_quantity'] == 3
    assert totals['quantity'] == 13
    assert totals['missing_lines'] == 2 and totals['missing_quantity'] == 7
    assert totals['uncovered_revenue'] == '30.00' and totals['gross_margin_rate'] is None
    assert result['ranking_total'] == 3 and len(result['ranking']) == 1 and result['ranking'][0]['part_id'] == parts[0]['id']
    assert result['details_total'] == 6 and len(result['details']) == 1
    day = client.get('/api/profits', headers=tokens['manager'], params=params | {'date_from': str(today)}).json()
    assert day['totals']['gross_profit'] == '-3.00' and day['totals']['warranty_cost'] == '10.00'
    paid = client.get('/api/profits', headers=tokens['manager'], params=params | {'part_id': parts[0]['id']}).json()
    assert paid['totals']['gross_margin_rate'] == '0.400000'
    warranty = client.get('/api/profits', headers=tokens['manager'], params=params | {'part_id': parts[2]['id']}).json()
    assert warranty['totals']['revenue'] == '0.00' and warranty['totals']['gross_margin_rate'] is None
    page = client.get('/api/profits', headers=tokens['manager'], params=params | {'page': 2, 'details_page': 2}).json()
    assert page['ranking'][0]['part_id'] == parts[2]['id']
    assert page['details'][0]['document_id'] != result['details'][0]['document_id']
    by_quantity = client.get('/api/profits', headers=tokens['manager'], params=params | {'sort_by': 'quantity'}).json()
    assert by_quantity['ranking'][0]['part_id'] == parts[1]['id'] and by_quantity['ranking'][0]['quantity'] == 8
    by_rate = client.get('/api/profits', headers=tokens['manager'], params=params | {'sort_by': 'margin_rate'}).json()
    assert by_rate['ranking'][0]['part_id'] == parts[0]['id']
    assert client.get('/api/profits', headers=tokens['manager'], params=params | {'date_from': str(today + timedelta(days=1))}).status_code == 422
