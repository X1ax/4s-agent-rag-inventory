from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import DocumentLine, StockBalance, StockLedger
from test_m1 import resources, system
from test_m2 import action, call, create, detail

pytestmark = pytest.mark.mysql


def quote(system, supplier, part, price, version=0):
    client, tokens, *_ = system
    response = client.put(f"/api/pricing/suppliers/{supplier['id']}/parts/{part['id']}", headers=tokens['admin'],
        json={'supply_price': price, 'source': '测试供应商报价', 'active': True, 'expected_version': version})
    assert response.status_code == 200, response.text
    return response.json()


def sale(system, part, price='150.00', version=0):
    response = system[0].patch(f"/api/pricing/parts/{part['id']}/sale-price", headers=system[1]['manager'],
        json={'sale_price': price, 'source': '测试门店定价', 'expected_version': version})
    assert response.status_code == 200, response.text


def setup_stock(system):
    warehouse, parts = resources(system, 1)
    part = parts[0]
    supplier = call(system, '/api/suppliers', {'code': system[3]+'-'+uuid4().hex[:6].upper(), 'name': '报价供应商'}, role='admin', expected=201)
    quote(system, supplier, part, '120.00')
    sale(system, part)
    call(system, '/api/inventory/openings', {'request_id': str(uuid4()), 'warehouse_id': warehouse['id'],
        'business_date': str(date.today()), 'lines': [{'part_id': part['id'], 'quantity': 10, 'unit_cost': '100.00', 'cost_source': '测试期初凭证'}]})
    return warehouse, part, supplier


def receive(system, warehouse, part, supplier, quantity, price):
    order = create(system, 'purchase', warehouse, [part], lines=[{'part_id': part['id'], 'supplier_id': supplier['id'], 'quantity': quantity, 'unit_price': price}])
    action(system, order['id'], 'confirm')
    line = detail(system, order['id'])['lines'][0]
    call(system, f"/api/documents/{order['id']}/receipts", {'request_id': str(uuid4()), 'business_date': str(date.today()),
        'lines': [{'purchase_line_id': line['id'], 'quantity': quantity}]}, expected=201)
    return order


def test_actual_weighted_cost_paid_and_warranty_snapshots_and_replay(system):
    warehouse, part, supplier = setup_stock(system)
    receive(system, warehouse, part, supplier, 10, '120.00')
    order = create(system, 'issue', warehouse, [part], issue_type='paid_repair', lines=[{'part_id': part['id'], 'quantity': 3}])
    payload = {'request_id': str(uuid4()), 'business_date': str(date.today())}
    call(system, f"/api/documents/{order['id']}/post", payload)
    assert call(system, f"/api/documents/{order['id']}/post", payload)['replayed']
    line = detail(system, order['id'])['lines'][0]
    assert Decimal(line['sale_unit_price']) == Decimal('150.00')
    assert Decimal(line['cost_amount']) == Decimal('330.00')
    sale(system, part, '180.00', 1)
    assert Decimal(detail(system, order['id'])['lines'][0]['sale_unit_price']) == Decimal('150.00')
    warranty = create(system, 'issue', warehouse, [part], issue_type='warranty', warranty_reason='质保更换', lines=[{'part_id': part['id'], 'quantity': 2}])
    action(system, warranty['id'], 'post')
    wline = detail(system, warranty['id'])['lines'][0]
    assert Decimal(wline['sale_unit_price']) == 0
    assert Decimal(wline['cost_amount']) == Decimal('220.00')
    with Session(system[2]) as session:
        balance = session.scalar(select(StockBalance).where(StockBalance.warehouse_id==warehouse['id'], StockBalance.part_id==part['id']))
        assert (balance.quantity, balance.stock_value) == (15, Decimal('1650.00'))
        assert len(list(session.scalars(select(StockLedger).where(StockLedger.document_line_id==line['id'], StockLedger.event=='issue')))) == 1


def test_server_rejects_forged_price_and_missing_purpose(system):
    warehouse, part, supplier = setup_stock(system)
    body = {'request_id': str(uuid4()), 'kind':'purchase', 'warehouse_id':warehouse['id'], 'business_date':str(date.today()),
        'lines':[{'part_id':part['id'],'supplier_id':supplier['id'],'quantity':2,'unit_price':'0.01'}]}
    call(system, '/api/documents', body, expected=409)
    body.update(kind='issue', lines=[{'part_id':part['id'],'quantity':1}])
    body['request_id']=str(uuid4())
    call(system, '/api/documents', body, expected=422)
    call(system, '/api/documents', body|{'request_id':str(uuid4()),'issue_type':'warranty'}, expected=422)
    body['lines'][0]['sale_unit_price']='1.00'
    call(system, '/api/documents', body|{'request_id':str(uuid4()),'issue_type':'paid_repair'}, expected=422)


def test_transfer_value_is_frozen_at_send_and_conserved(system):
    warehouse, part, supplier = setup_stock(system)
    target, _ = resources(system, 0)
    transfer = create(system, 'transfer', warehouse, [part], target_warehouse_id=target['id'], lines=[{'part_id':part['id'],'quantity':4}])
    action(system, transfer['id'], 'send')
    assert Decimal(detail(system, transfer['id'])['lines'][0]['cost_amount']) == Decimal('400.00')
    quote(system, supplier, part, '500.00', 1)
    receive(system, target, part, supplier, 1, '500.00')
    action(system, transfer['id'], 'receive')
    with Session(system[2]) as session:
        balances=list(session.scalars(select(StockBalance).where(StockBalance.part_id==part['id'])))
        assert sum(b.quantity for b in balances)==11
        assert sum(b.stock_value for b in balances)==Decimal('1500.00')
        assert next(b for b in balances if b.warehouse_id==target['id']).stock_value==Decimal('900.00')


def test_new_opening_requires_cost_and_cannot_assume_zero(system):
    warehouse, parts=resources(system, 1)
    body={'request_id':str(uuid4()),'warehouse_id':warehouse['id'],'business_date':str(date.today()),
        'lines':[{'part_id':parts[0]['id'],'quantity':1}]}
    call(system,'/api/inventory/openings',body,expected=422)
    body['lines'][0]['unit_cost']='0.00'
    call(system,'/api/inventory/openings',body|{'request_id':str(uuid4())},expected=422)
