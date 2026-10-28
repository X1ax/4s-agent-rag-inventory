"""Historical pricing plans use original ledger order and never rewrite quantity facts."""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.backfill_pricing import make_plan, apply_plan
from backend.app.db import Base
from backend.app.models import (AgentProposal, Document, DocumentLine, OpeningDocument,
    OpeningLine, Part, PricingBackfill, StockBalance, StockLedger, Supplier, SupplierPrice,
    User, Warehouse)


@pytest.fixture
def history(request):
    # Plan/replay tests use SQLite only; MySQL locking is validated separately.
    use_mysql = getattr(request, "param", "sqlite") == "mysql"
    if use_mysql:
        from backend.app.config import Settings
        from backend.app.db import make_engine
        settings = Settings()
        assert settings.test_mysql_database == "fours_agent_test" and settings.test_mysql_user == "fours_test"
        engine = make_engine(settings, test=True)
    else:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
    prefix = "BF" + uuid4().hex[:10]
    with Session(engine) as session:
        transaction = session.begin()
        user = User(username=prefix, display_name="补录测试", role="admin", password_hash="unused")
        supplier = Supplier(code=prefix+"S1", name="默认", supply_categories=["测试"])
        other = Supplier(code=prefix+"S2", name="备用", supply_categories=["测试"])
        a, b = Warehouse(code=prefix+"W1", name="一仓"), Warehouse(code=prefix+"W2", name="二仓")
        session.add_all([user, supplier, other, a, b]); session.flush()
        part = Part(sku=prefix+"P", name="机油滤清器", category="测试", unit="件",
            reference_price=Decimal("35"), default_supplier_id=supplier.id)
        session.add(part); session.flush()
        opening = OpeningDocument(request_id=prefix, payload_hash="x"*64, warehouse_id=a.id,
            operator_id=user.id, business_date=date(2026, 10, 20))
        session.add(opening); session.flush()
        initial = OpeningLine(document_id=opening.id, part_id=part.id, quantity=10)
        session.add(initial); session.flush()
        session.add(StockLedger(warehouse_id=a.id, part_id=part.id, opening_line_id=initial.id,
            event="opening", quantity_delta=10, business_date=date(2026, 10, 20), operator_id=user.id))
        purchase = Document(kind="purchase", status="completed", warehouse_id=a.id,
            supplier_id=supplier.id, operator_id=user.id, business_date=date(2026, 8, 1))
        session.add(purchase); session.flush()
        ordered = DocumentLine(document_id=purchase.id, part_id=part.id, supplier_id=supplier.id,
            quantity=10, received_quantity=10, unit_price=Decimal("100"))
        session.add(ordered); session.flush()
        receipt = Document(kind="receipt", status="posted", warehouse_id=a.id, purchase_id=purchase.id,
            operator_id=user.id, business_date=date(2026, 8, 2))
        session.add(receipt); session.flush()
        received = DocumentLine(document_id=receipt.id, part_id=part.id, quantity=10,
            unit_price=Decimal("100"), purchase_line_id=ordered.id, supplier_id=supplier.id)
        session.add(received); session.flush()
        session.add(StockLedger(warehouse_id=a.id, part_id=part.id, document_line_id=received.id,
            event="receipt", quantity_delta=10, business_date=date(2026, 8, 2), operator_id=user.id))
        issue = Document(kind="issue", status="posted", warehouse_id=a.id,
            operator_id=user.id, business_date=date(2026, 7, 1))
        session.add(issue); session.flush()
        used = DocumentLine(document_id=issue.id, part_id=part.id, quantity=3, unit_price=0)
        session.add(used); session.flush()
        session.add(StockLedger(warehouse_id=a.id, part_id=part.id, document_line_id=used.id,
            event="issue", quantity_delta=-3, business_date=date(2026, 7, 1), operator_id=user.id))
        transfer = Document(kind="transfer", status="sent", warehouse_id=a.id, target_warehouse_id=b.id,
            operator_id=user.id, business_date=date(2026, 10, 21))
        session.add(transfer); session.flush()
        sent = DocumentLine(document_id=transfer.id, part_id=part.id, quantity=5, unit_price=0)
        session.add(sent); session.flush()
        session.add(StockLedger(warehouse_id=a.id, part_id=part.id, document_line_id=sent.id,
            event="transfer_send", quantity_delta=-5, business_date=date(2026, 10, 21), operator_id=user.id))
        session.add_all([StockBalance(warehouse_id=a.id, part_id=part.id, quantity=12),
            StockBalance(warehouse_id=b.id, part_id=part.id, quantity=0)])
        session.add(AgentProposal(id=prefix, user_id=user.id, thread_id=prefix, turn_id=prefix,
            action="purchase", payload={}, snapshot_hash="x"*64, summary={}, status="pending",
            expires_at=datetime(2027, 1, 1)))
        session.flush()
        try:
            yield session, user, part, supplier, initial, ordered, received, used, sent
        finally:
            transaction.rollback()  # No test history or version record is committed, including on MySQL.
    engine.dispose()


def test_plan_replays_ledger_id_preserves_old_prices_and_values(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    plan = make_plan(session)
    assert plan == make_plan(session)
    assert plan["summary"]["quantity"] == 12
    assert plan["summary"]["in_transit_quantity"] == 5
    assert plan["summary"]["stock_value"] == "1200.00"
    assert plan["summary"]["in_transit_value"] == "500.00"
    assert initial.unit_cost is None  # planning is read only
    result = apply_plan(session, plan, user.id)
    from backend.backfill_pricing import quantity_fingerprint, snapshot
    assert quantity_fingerprint(snapshot(session)) == plan["quantity_fingerprint"]
    assert result["quantity"] == 12
    assert initial.unit_cost == Decimal("100")
    assert "模拟" in initial.cost_source
    assert received.unit_price == ordered.unit_price == Decimal("100")
    assert "原成交价保留" in received.pricing_source
    assert "原成交价保留" in ordered.pricing_source
    assert used.unit_price == 0
    assert used.cost_amount == Decimal("300")
    assert sent.cost_amount == Decimal("500")
    assert session.scalar(select(AgentProposal).where(AgentProposal.user_id == user.id)).status == "superseded"
    assert session.get(PricingBackfill, plan["version"]) is not None
    assert apply_plan(session, plan, user.id) == result
    part.sale_price = Decimal("999.00")
    session.flush()
    assert apply_plan(session, plan, user.id) == result
    assert part.sale_price == Decimal("999.00")


def test_plan_refuses_changed_baseline_tampered_plan_and_wrong_balance(history):
    session, user, part, *_ = history
    plan = make_plan(session)
    part.reference_price = Decimal("36")
    session.flush()
    with pytest.raises(ValueError, match="baseline"):
        apply_plan(session, plan, user.id)
    part.reference_price = Decimal("35"); session.flush()
    plan["summary"]["quantity"] += 1
    with pytest.raises(ValueError, match="digest"):
        apply_plan(session, plan, user.id)
    balance = session.scalar(select(StockBalance).where(StockBalance.quantity > 0))
    balance.quantity += 1; session.flush()
    with pytest.raises(ValueError, match="quantity"):
        make_plan(session)


def test_manual_price_is_preserved_and_unpriced_opening_uses_quote(history):
    session, user, part, supplier, initial, *_ = history
    part.sale_price, part.sale_price_version = Decimal("88"), 4
    session.add(SupplierPrice(part_id=part.id, supplier_id=supplier.id, supply_price=Decimal("31"),
        version=7, active=True, source="人工核准")); session.flush()
    plan = make_plan(session)
    apply_plan(session, plan, user.id)
    assert part.sale_price == Decimal("88") and part.sale_price_version == 4
    quote = session.scalar(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier.id))
    assert quote.supply_price == Decimal("31") and quote.version == 7


def test_opening_without_receipts_uses_default_quote_with_explicit_source(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    receipt_doc = session.get(Document, received.document_id)
    receipt_event = session.scalar(select(StockLedger).where(StockLedger.document_line_id == received.id))
    session.delete(receipt_event); session.flush()
    session.delete(received); session.flush()
    session.delete(receipt_doc)
    ordered.received_quantity = 0
    session.get(Document, ordered.document_id).status = "confirmed"
    balance = session.scalar(select(StockBalance).where(StockBalance.quantity > 0))
    balance.quantity -= 10
    session.flush()
    plan = make_plan(session)
    apply_plan(session, plan, user.id)
    assert initial.unit_cost == Decimal("35")
    assert "默认供应商模拟报价" in initial.cost_source
    assert used.cost_amount == Decimal("105") and sent.cost_amount == Decimal("175")
    assert balance.stock_value == Decimal("70")


def test_candidate_prices_match_all_48_approved_rows():
    from backend.backfill_pricing import candidate_price
    text = (Path(__file__).parents[1]/"docs/superpowers/specs/2026-10-26-pricing-and-gross-profit-design.md").read_text(encoding="utf-8")
    rows = re.findall(r"\| ((?:CAT|DEMO)-\d{3}) \| [^|]+ \| ([^|]+) \| ([\d.]+) \|", text)
    assert len(rows) == 48
    for sku, quotes, sale in rows:
        default = Decimal(re.search(r"([\d.]+)", quotes).group(1))
        assert candidate_price(default, f"{sku}:retail", 125, 160) == Decimal(sale)
        amounts = re.findall(r"([\d.]+)", quotes)
        if len(amounts) == 2:
            assert candidate_price(default, f"{sku}:DEMO-S03", 95, 105) == Decimal(amounts[1])


def test_historical_month_price_is_consistent_and_warranty_has_no_revenue(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    warehouse_id = session.get(Document, used.document_id).warehouse_id
    # Additional stock and issues have backdated business dates but later ledger IDs.
    initial.quantity += 100
    opening_event = session.scalar(select(StockLedger).where(StockLedger.opening_line_id == initial.id))
    opening_event.quantity_delta += 100
    balance = session.scalar(select(StockBalance).where(StockBalance.quantity > 0))
    for index in range(100):
        doc = Document(kind="issue", status="posted", warehouse_id=warehouse_id, operator_id=user.id,
            business_date=date(2026, 7 if index % 2 else 8, 1))
        session.add(doc); session.flush()
        line = DocumentLine(document_id=doc.id, part_id=part.id, quantity=1, unit_price=0)
        session.add(line); session.flush()
        session.add(StockLedger(warehouse_id=warehouse_id, part_id=part.id, document_line_id=line.id,
            event="issue", quantity_delta=-1, business_date=doc.business_date, operator_id=user.id))
    session.flush()
    plan = make_plan(session)
    assert 0 < plan["summary"]["warranty_documents"] < 40
    apply_plan(session, plan, user.id)
    monthly = {}
    for doc, line in session.execute(select(Document, DocumentLine).join(DocumentLine,
            DocumentLine.document_id == Document.id).where(Document.kind == "issue")):
        if doc.issue_type == "warranty":
            assert line.sale_unit_price == 0 and doc.warranty_reason
        else:
            month = doc.business_date.strftime("%Y-%m")
            assert line.sale_unit_price == monthly.setdefault(month, line.sale_unit_price)
        assert line.unit_price == 0 and line.cost_amount > 0


def test_transferred_cost_enters_target_with_same_value_and_zero_in_transit(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    doc = session.get(Document, sent.document_id)
    doc.status = "received"
    target = session.scalar(select(StockBalance).where(StockBalance.warehouse_id == doc.target_warehouse_id))
    target.quantity = 5
    session.add(StockLedger(warehouse_id=doc.target_warehouse_id, part_id=part.id, document_line_id=sent.id,
        event="transfer_receive", quantity_delta=5, business_date=doc.business_date, operator_id=user.id))
    session.flush()
    plan = make_plan(session)
    assert plan["summary"]["in_transit_value"] == "0.00"
    assert plan["summary"]["stock_value"] == "1700.00"
    apply_plan(session, plan, user.id)
    assert target.stock_value == sent.cost_amount == Decimal("500")


def test_missing_ledger_for_posted_document_is_rejected(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    used_event = session.scalar(select(StockLedger).where(StockLedger.document_line_id == used.id))
    session.delete(used_event)
    balance = session.scalar(select(StockBalance).where(StockBalance.quantity > 0))
    balance.quantity += 3
    session.flush()
    with pytest.raises(ValueError, match="ledger"):
        make_plan(session)


@pytest.mark.mysql
@pytest.mark.parametrize("history", ["mysql"], indirect=True)
def test_mysql_backfill_is_transactional_replayable_and_preserves_prices(history):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    plan = make_plan(session)
    result = apply_plan(session, plan, user.id)
    assert initial.unit_cost == received.unit_price == ordered.unit_price == Decimal("100")
    assert used.unit_price == 0 and used.cost_amount == Decimal("300")
    assert sent.cost_amount == Decimal("500")
    assert result["stock_value"] == "1200.00"
    assert apply_plan(session, plan, user.id) == result


def test_failed_apply_rolls_back_all_target_fields(history, monkeypatch):
    session, user, part, supplier, initial, ordered, received, used, sent = history
    plan = make_plan(session)
    original_flush = session.flush

    def fail_when_writes_start(*args, **kwargs):
        if any(isinstance(row, PricingBackfill) for row in session.new):
            raise RuntimeError("simulated database write failure")
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_when_writes_start)
    with pytest.raises(RuntimeError, match="write failure"):
        apply_plan(session, plan, user.id)
    monkeypatch.setattr(session, "flush", original_flush)
    assert initial.unit_cost is None and part.sale_price is None
    assert used.cost_amount is None and sent.cost_amount is None
    assert session.scalar(select(SupplierPrice)) is None
    assert session.get(PricingBackfill, plan["version"]) is None
    assert make_plan(session) == plan
