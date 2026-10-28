from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_m1 import opening_body, resources, system
from backend.app.models import AuditLog, Document, DocumentLine, OpeningDocument, OpeningLine, OperationRequest, PriceChange, StockBalance, StockLedger, SupplierPrice, User
from backend.app.schemas import OpeningInput
from backend.seed_demo import existing_simulated_document, post_simulated_opening
from backend.seed_history import HISTORY_DAYS, HISTORY_END, HISTORY_START, import_history

pytestmark = pytest.mark.mysql


def test_history_is_complete_balanced_and_replayable(system):
    client, tokens, engine, prefix, _ = system
    main, parts = resources(system, count=12)
    quick, _ = resources(system, count=0)
    supplier_ids = []
    for index in range(3):
        response = client.post('/api/suppliers', headers=tokens['admin'], json={
            'code': f'{prefix}-H{index}', 'name': '历史测试供应商', 'supply_categories': ['']})
        assert response.status_code == 201
        supplier_ids.append(response.json()['id'])
    response = client.post('/api/inventory/openings', headers=tokens['storekeeper'],
        json=opening_body(main, parts) | {'business_date': '2026-10-01'})
    assert response.status_code == 200
    warehouse_ids = [main['id'], quick['id']]
    part_ids = [part['id'] for part in parts]

    with Session(engine) as session, session.begin():
        operator_id = session.scalar(select(User.id).where(User.username == f'{prefix.lower()}_admin'))
        before = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id,
            StockBalance.part_id, StockBalance.quantity).where(StockBalance.warehouse_id.in_(warehouse_ids)))}
        import_history(session, operator_id, warehouse_ids, part_ids, supplier_ids)

    with Session(engine) as session:
        balances = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id,
            StockBalance.part_id, StockBalance.quantity).where(StockBalance.warehouse_id.in_(warehouse_ids)))}
        assert all(quantity == before.get(key, 0) for key, quantity in balances.items())
        assert len(balances) == 24
        ledger = {(w, p): q for w, p, q in session.execute(select(StockLedger.warehouse_id,
            StockLedger.part_id, func.sum(StockLedger.quantity_delta)).where(StockLedger.warehouse_id.in_(warehouse_ids)).group_by(
                StockLedger.warehouse_id, StockLedger.part_id))}
        assert balances == ledger
        days = set(session.scalars(select(StockLedger.business_date).where(
            StockLedger.warehouse_id.in_(warehouse_ids), StockLedger.event == 'issue')))
        assert days == {HISTORY_START + timedelta(days=offset) for offset in range(HISTORY_DAYS)}
        assert HISTORY_END == date(2026, 10, 21) and HISTORY_DAYS > 30
        # Rebuilding by business date must also avoid negative historical stock.
        running = {}
        for entry in session.scalars(select(StockLedger).where(StockLedger.warehouse_id.in_(warehouse_ids)).order_by(
            func.coalesce(StockLedger.business_date, date(2026, 10, 1)), StockLedger.id)):
            key = (entry.warehouse_id, entry.part_id)
            running[key] = running.get(key, 0) + entry.quantity_delta
            assert running[key] >= 0
        counts = [session.scalar(select(func.count()).select_from(model)) for model in
            (Document, DocumentLine, OperationRequest, StockBalance, StockLedger, AuditLog)]
        original_dates = list(session.execute(select(Document.id, Document.business_date).where(
            Document.warehouse_id.in_(warehouse_ids))).all())

    with Session(engine) as session, session.begin():
        import_history(session, operator_id, warehouse_ids, part_ids, supplier_ids)
    with Session(engine) as session:
        assert counts == [session.scalar(select(func.count()).select_from(model)) for model in
            (Document, DocumentLine, OperationRequest, StockBalance, StockLedger, AuditLog)]
        assert original_dates == list(session.execute(select(Document.id, Document.business_date).where(
            Document.warehouse_id.in_(warehouse_ids))).all())
        quotes = list(session.scalars(select(SupplierPrice).where(SupplierPrice.part_id.in_(part_ids))))
        assert len(quotes) == 12 and all('模拟' in row.source for row in quotes)
        assert session.scalar(select(func.count()).select_from(PriceChange).where(PriceChange.part_id.in_(part_ids))) == 24
        for doc, line in session.execute(select(Document, DocumentLine).join(DocumentLine,
            DocumentLine.document_id == Document.id).where(Document.kind == 'issue', Document.warehouse_id.in_(warehouse_ids))):
            assert doc.issue_type == 'paid_repair' and line.sale_unit_price > 0 and line.cost_amount > 0

    response = client.get('/api/statistics', headers=tokens['manager'], params={
        'warehouse_id': main['id'], 'date_from': str(HISTORY_START), 'date_to': str(HISTORY_END), 'unit': '件'})
    assert response.status_code == 200
    issue_days = {row['business_date'] for row in response.json()['trend'] if row['event'] == 'issue'}
    assert len(issue_days) == HISTORY_DAYS
    assert response.json()['consumption_ranking']


def test_legacy_seed_retries_preserve_hashes_missing_new_fields_and_reject_changed_facts(system):
    client, tokens, engine, prefix, _ = system
    warehouse, parts = resources(system, count=1)
    body = opening_body(warehouse, parts)
    with Session(engine) as session:
        operator = session.scalar(select(User.id).where(User.username == prefix.lower()+'_admin'))
        opening = OpeningDocument(request_id=body['request_id'], payload_hash='a'*64,
            warehouse_id=warehouse['id'], business_date=date.today(), operator_id=operator, note='')
        session.add(opening); session.flush()
        old_line = OpeningLine(document_id=opening.id, part_id=parts[0]['id'], quantity=3)
        session.add(old_line); session.flush()
        result, replayed = post_simulated_opening(session, OpeningInput(**body), operator)
        assert result.id == opening.id and replayed
        assert opening.payload_hash == 'a'*64 and old_line.unit_cost is None
        changed = OpeningInput(**(body | {'lines': [body['lines'][0] | {'quantity': 4}]}))
        with pytest.raises(ValueError, match='different business facts'):
            post_simulated_opening(session, changed, operator)
        issue = Document(kind='issue', status='posted', warehouse_id=warehouse['id'], business_date=date.today(), operator_id=operator)
        session.add(issue); session.flush()
        session.add(DocumentLine(document_id=issue.id, part_id=parts[0]['id'], quantity=1, unit_price=0))
        request = str(uuid4())
        operation = OperationRequest(request_id=request, payload_hash='b'*64, operator_id=operator,
            document_id=issue.id, result_status='draft')
        session.add(operation); session.flush()
        items = [{'part_id': parts[0]['id'], 'quantity': 1}]
        assert existing_simulated_document(session, request, operator, 'issue', warehouse['id'], date.today(), items) == issue.id
        assert issue.issue_type is None and operation.payload_hash == 'b'*64
        with pytest.raises(ValueError, match='different business facts'):
            existing_simulated_document(session, request, operator, 'issue', warehouse['id'], date.today(),
                [{'part_id': parts[0]['id'], 'quantity': 2}])
        session.rollback()
