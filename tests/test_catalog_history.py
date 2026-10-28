from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import Document, DocumentLine, Part, PriceChange, StockBalance, StockLedger, Supplier, SupplierPrice, User, Warehouse
from backend.seed_catalog_history import DAYS, END, START, build_plan, import_history
from test_m1 import opening_body, resources, system


def test_random_plan_is_reproducible_varied_and_supplied_before_consumption():
    parts = [Part(id=i+1, sku=f'CAT-{i+13:03}', name=name, unit='件', category='测试', safety_stock=i+1, reference_price=Decimal('0'))
        for i, name in enumerate(['点火线圈', '起动机', '空气流量传感器'])]
    warehouses = [Warehouse(id=1, code='MAIN', name='主仓'), Warehouse(id=2, code='QUICK', name='快修仓')]
    suppliers = [Supplier(id=1, code='S1', name='合格供应商', active=True, supply_categories=['测试'], lead_time_days=7)]
    first = build_plan(parts, warehouses, suppliers, set(), {})
    assert first == build_plan(parts, warehouses, suppliers, set(), {})
    assert first != build_plan(parts, warehouses, suppliers, set(), {}, seed=7)
    assert len(first) == 6 and DAYS > 30
    assert len({tuple((r['day'], r['quantity']) for r in row['issues']) for row in first}) > 1
    for row in first:
        assert any(issue['day'] >= END-timedelta(days=29) for issue in row['issues'])
        assert all(START <= issue['day'] <= END and type(issue['quantity']) is int and issue['quantity'] > 0 for issue in row['issues'])
        assert sum(b['quantity'] for b in row['batches']) == sum(i['quantity'] for i in row['issues'])+row['closing_quantity']
        events = [(receipt['day'], 0, receipt['quantity']) for batch in row['batches'] for receipt in batch['receipts']]
        events += [(issue['day'], 1, -issue['quantity']) for issue in row['issues']]
        running = 0
        for _, _, delta in sorted(events):
            running += delta
            assert running >= 0
        assert running == row['closing_quantity']
        assert all(b['supplier_id'] == 1 and b['unit_price'] > 0 for b in row['batches'])


@pytest.mark.mysql
def test_import_covers_both_warehouses_balances_and_idempotent_repeat(system):
    client, tokens, engine, prefix, _ = system
    main, records = resources(system, count=3)
    quick, _ = resources(system, count=0)
    response = client.post('/api/suppliers', headers=tokens['admin'], json={'code': prefix+'-RANDOM', 'name': '随机历史合格供应商', 'supply_categories': ['']})
    assert response.status_code == 201
    supplier_id = response.json()['id']
    assert client.post('/api/inventory/openings', headers=tokens['storekeeper'], json=opening_body(main, records)).status_code == 200
    ids, warehouse_ids = [r['id'] for r in records], [main['id'], quick['id']]
    with Session(engine) as session, session.begin():
        operator = session.scalar(select(User.id).where(User.username == prefix.lower()+'_admin'))
        parts = list(session.scalars(select(Part).where(Part.id.in_(ids))))
        warehouses = list(session.scalars(select(Warehouse).where(Warehouse.id.in_(warehouse_ids))))
        suppliers = [session.get(Supplier, supplier_id)]
        parts[0].sale_price, parts[0].sale_price_version = Decimal('77'), 5
        session.add(SupplierPrice(supplier_id=supplier_id, part_id=parts[0].id,
            supply_price=Decimal('99'), version=7, active=True, source='人工核准模拟测试报价'))
        session.flush()
        before = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity).where(StockBalance.warehouse_id.in_(warehouse_ids)))}
        plan = import_history(session, operator, parts, warehouses, suppliers)
        assert len(plan) == 6
    with Session(engine) as session:
        after = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity).where(StockBalance.warehouse_id.in_(warehouse_ids)))}
        assert all(after[k] == v for k, v in before.items()) and len(after) == 6
        expected = {(r['warehouse_id'], r['part_id']): r['closing_quantity'] for r in plan}
        assert after == expected
        assert session.get(Part, ids[0]).sale_price == Decimal('77')
        assert session.get(Part, ids[0]).sale_price_version == 5
        manual = session.scalar(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id, SupplierPrice.part_id == ids[0]))
        assert manual.supply_price == Decimal('99') and manual.version == 7
        assert all(batch['unit_price'] == Decimal('99') for row in plan if row['part_id'] == ids[0] for batch in row['batches'])
        for key, quantity in after.items():
            total = session.scalar(select(func.sum(StockLedger.quantity_delta)).where(StockLedger.warehouse_id == key[0], StockLedger.part_id == key[1]))
            assert quantity == total
            value = session.scalar(select(StockBalance.stock_value).where(StockBalance.warehouse_id == key[0], StockBalance.part_id == key[1]))
            ledger_value = session.scalar(select(func.sum(StockLedger.value_delta)).where(StockLedger.warehouse_id == key[0], StockLedger.part_id == key[1]))
            assert value == ledger_value and value >= 0
        assert session.scalar(select(func.count()).select_from(PriceChange).where(PriceChange.part_id.in_(ids))) > 0
        for document, line in session.execute(select(Document, DocumentLine).join(DocumentLine,
            DocumentLine.document_id == Document.id).where(Document.kind == 'issue', Document.warehouse_id.in_(warehouse_ids))):
            assert document.issue_type == 'paid_repair' and line.sale_unit_price > 0 and line.cost_amount > 0
        counts = [session.scalar(select(func.count()).select_from(model)) for model in (Document, StockLedger)]
    with Session(engine) as session, session.begin():
        parts = list(session.scalars(select(Part).where(Part.id.in_(ids))))
        warehouses = list(session.scalars(select(Warehouse).where(Warehouse.id.in_(warehouse_ids))))
        assert import_history(session, operator, parts, warehouses, [session.get(Supplier, supplier_id)]) == []
    with Session(engine) as session:
        assert counts == [session.scalar(select(func.count()).select_from(model)) for model in (Document, StockLedger)]
