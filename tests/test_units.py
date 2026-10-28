from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import AuditLog, Part, StockBalance, StockLedger
from backend.normalize_units import normalize
from test_m1 import resources, system

pytestmark = pytest.mark.mysql


def test_normalization_preserves_package_quantity_prices_and_ledger(system):
    client, tokens, engine, prefix, _ = system
    warehouse, _ = resources(system, count=0)
    with Session(engine) as session, session.begin():
        part = Part(sku=prefix+'-LEGACY', name='测试整桶机油', category='油液', unit='桶',
            specification='测试4L包装', safety_stock=5, reference_price=Decimal('59.20'))
        session.add(part)
        session.flush()
        part_id = part.id
    response = client.post('/api/inventory/openings', headers=tokens['admin'], json={
        'request_id': str(uuid4()), 'warehouse_id': warehouse['id'], 'business_date': '2026-10-01',
        'lines': [{'part_id': part_id, 'quantity': 7, 'unit_cost': '59.20', 'cost_source': '测试旧包装期初凭证'}]})
    assert response.status_code == 200
    operator = client.get('/api/auth/me', headers=tokens['admin']).json()['id']
    with Session(engine) as session, session.begin():
        before = [(r.id, r.quantity_delta) for r in session.scalars(select(StockLedger).where(StockLedger.part_id == part_id))]
        changes = normalize(session, operator)
        assert len(changes) == 1 and changes[0]['old_unit'] == '桶'
        part = session.get(Part, part_id)
        assert part.unit == '件' and part.reference_price == Decimal('59.20') and part.safety_stock == 5
        assert '测试4L包装' in part.specification and '整桶按1件' in part.specification
        assert session.scalar(select(StockBalance.quantity).where(StockBalance.part_id == part_id)) == 7
        assert [(r.id, r.quantity_delta) for r in session.scalars(select(StockLedger).where(StockLedger.part_id == part_id))] == before
        assert normalize(session, operator) == []
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'part.unit.normalize', AuditLog.entity_id == part_id)) == 1


def test_new_parts_use_piece_units_and_store_package_specification(system):
    client, tokens, _, prefix, _ = system
    body = {'sku': prefix+'-PACKAGE', 'name': '测试制动液', 'specification': '整瓶按1件，容量待核准'}
    response = client.post('/api/parts', headers=tokens['admin'], json=body)
    assert response.status_code == 201 and response.json()['unit'] == '件'
    assert response.json()['specification'] == body['specification']
    for unit in ['瓶', '桶', '套', '升', '']:
        assert client.post('/api/parts', headers=tokens['admin'], json=body | {'unit': unit}).status_code == 422
