from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from backend.app.api import catalog_view, page_result
from backend.app.documents import create_document, receive_purchase, transition
from backend.app.models import Document, DocumentLine, DocumentReview, Part, Supplier, User, Warehouse
from backend.app.reviews import review_view
from backend.app.queries import balance_statement, ledger_statement
from backend.app.schemas import ActionInput, DocumentInput, ReceiptInput
from backend.app.security import current_user, session_dependency, stock_writer

router = APIRouter(prefix="/api", tags=["库存业务"])
Kind = Literal["purchase", "receipt", "issue", "transfer"]


@router.post("/documents", status_code=201)
def create(payload: DocumentInput, user: User = Depends(stock_writer), session: Session = Depends(session_dependency)):
    result = create_document(session, payload, user.id)
    session.commit()
    return result


@router.get("/documents")
def documents(kind: Kind, status: str = Query(default="", max_length=20), warehouse_id: int | None = Query(default=None, gt=0), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    target = aliased(Warehouse)
    statement = select(Document.id, Document.kind, Document.status, Document.warehouse_id, Warehouse.name.label("warehouse"),
        target.name.label("target_warehouse"), Supplier.name.label("supplier"), Document.purchase_id, Document.business_date, Document.note,
        User.display_name.label("operator"), Document.created_at,
        func.coalesce(DocumentReview.status, case((Document.status == 'draft', 'not_submitted'), else_='legacy')).label('review_status')).join(Warehouse, Warehouse.id == Document.warehouse_id).outerjoin(target,
        target.id == Document.target_warehouse_id).outerjoin(Supplier, Supplier.id == Document.supplier_id).join(User, User.id == Document.operator_id).outerjoin(DocumentReview, DocumentReview.document_id == Document.id).where(Document.kind == kind)
    if status:
        statement = statement.where(Document.status == status)
    if warehouse_id:
        statement = statement.where((Document.warehouse_id == warehouse_id) | (Document.target_warehouse_id == warehouse_id))
    return page_result(session, statement.order_by(Document.id.desc()), page, page_size)


@router.get("/documents/{document_id}")
def detail(document_id: int, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "单据不存在")
    result = catalog_view(document)
    result['review'] = review_view(session, document)
    result["lines"] = [dict(row) for row in session.execute(select(DocumentLine.id, DocumentLine.part_id, Part.sku, Part.name, Part.unit, Part.image_key,
        DocumentLine.quantity, DocumentLine.received_quantity, DocumentLine.unit_price, DocumentLine.purchase_line_id,
        DocumentLine.supplier_id, DocumentLine.sale_unit_price, DocumentLine.cost_amount, DocumentLine.quote_version,
        DocumentLine.sale_price_version, DocumentLine.pricing_source, Supplier.name.label("supplier")).join(Part,
        Part.id == DocumentLine.part_id).outerjoin(Supplier, Supplier.id == DocumentLine.supplier_id).where(DocumentLine.document_id == document_id).order_by(DocumentLine.part_id)).mappings()]
    for line in result["lines"]:
        line["unit_price"] = str(line["unit_price"])
        for key in ('sale_unit_price','cost_amount'):
            if line[key] is not None:
                line[key] = str(line[key])
        price = line['sale_unit_price'] if document.kind == 'issue' else line['unit_price']
        line["subtotal"] = str(Decimal(price) * line["quantity"]) if price is not None else None
    result["total"] = str(sum((Decimal(line["subtotal"]) for line in result["lines"]), Decimal("0.00"))) if all(line['subtotal'] is not None for line in result['lines']) else None
    result["receipts"] = [{"id": row.id, "business_date": row.business_date} for row in session.scalars(select(Document).where(Document.purchase_id == document_id).order_by(Document.id))]
    return result


@router.post("/documents/{document_id}/receipts", status_code=201)
def receipt(document_id: int, payload: ReceiptInput, user: User = Depends(stock_writer), session: Session = Depends(session_dependency)):
    result = receive_purchase(session, document_id, payload, user.id)
    session.commit()
    return result


@router.post("/documents/{document_id}/{verb}")
def act(document_id: int, verb: Literal["confirm", "post", "send", "receive", "cancel"], payload: ActionInput, user: User = Depends(stock_writer), session: Session = Depends(session_dependency)):
    result = transition(session, document_id, verb, payload, user.id)
    session.commit()
    return result


@router.get("/statistics", tags=["库存统计"])
def statistics(unit: Literal["件"] | None = None, warehouse_id: int | None = Query(default=None, gt=0),
    part_id: int | None = Query(default=None, gt=0),
    date_from: date | None = None, date_to: date | None = None, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    end = date_to or date.today()
    start = date_from or end - timedelta(days=29)
    if start > end or end > date.today() or (end - start).days > 365:
        raise HTTPException(422, "统计日期需为不超过366天且不晚于今天的有效区间")
    part = session.get(Part, part_id) if part_id else None
    if part_id and part is None:
        raise HTTPException(404, "备件不存在")
    units = [part.unit] if part else [unit] if unit else list(session.scalars(select(Part.unit).distinct().order_by(Part.unit)))
    groups = []
    for group_unit in units:
        snapshot = balance_statement().subquery()
        conditions = [snapshot.c.unit == group_unit]
        if part_id:
            conditions.append(snapshot.c.part_id == part_id)
        if warehouse_id:
            conditions.append(snapshot.c.warehouse_id == warehouse_id)
        totals = session.execute(select(func.coalesce(func.sum(snapshot.c.quantity), 0).label("on_hand"),
            func.coalesce(func.sum(snapshot.c.purchase_pending), 0).label("purchase_pending"), func.coalesce(func.sum(snapshot.c.in_transit), 0).label("in_transit")).where(*conditions)).mappings().one()
        low = [dict(row) for row in session.execute(select(snapshot).where(*conditions, snapshot.c.part_active == True,
            snapshot.c.warehouse_active == True, snapshot.c.quantity < snapshot.c.safety_stock).order_by(snapshot.c.quantity, snapshot.c.part_id)).mappings()]
        zero = [dict(row) for row in session.execute(select(snapshot).where(*conditions, snapshot.c.part_active == True,
            snapshot.c.warehouse_active == True, snapshot.c.quantity == 0).order_by(snapshot.c.part_id, snapshot.c.warehouse_id)).mappings()]
        ledger = ledger_statement().subquery()
        filters = [ledger.c.unit == group_unit, ledger.c.business_date >= start, ledger.c.business_date <= end]
        if part_id:
            filters.append(ledger.c.part_id == part_id)
        if warehouse_id:
            filters.append(ledger.c.warehouse_id == warehouse_id)
        trend = [dict(row) for row in session.execute(select(ledger.c.business_date, ledger.c.event,
            func.sum(ledger.c.quantity_delta).label("quantity")).where(*filters).group_by(ledger.c.business_date, ledger.c.event).order_by(ledger.c.business_date)).mappings()]
        ranking = [dict(row) for row in session.execute(select(ledger.c.sku, ledger.c.name, ledger.c.image_key,
            (-func.sum(ledger.c.quantity_delta)).label("quantity")).where(*filters, ledger.c.event == "issue").group_by(
            ledger.c.part_id, ledger.c.sku, ledger.c.name, ledger.c.image_key).order_by(func.sum(ledger.c.quantity_delta)).limit(10)).mappings()]
        warehouse_totals = [dict(row) for row in session.execute(select(snapshot.c.warehouse, func.sum(snapshot.c.quantity).label("quantity")).where(*conditions).group_by(snapshot.c.warehouse_id, snapshot.c.warehouse).order_by(snapshot.c.warehouse_id)).mappings()]
        groups.append({"unit": group_unit, "totals": dict(totals), "low_stock": low, "zero_stock": zero,
            "trend": trend, "consumption_ranking": ranking, "warehouses": warehouse_totals})
    ledger = ledger_statement().subquery()
    filters = [ledger.c.business_date >= start, ledger.c.business_date <= end]
    if part_id:
        filters.append(ledger.c.part_id == part_id)
    elif unit:
        filters.append(ledger.c.unit == unit)
    if warehouse_id:
        filters.append(ledger.c.warehouse_id == warehouse_id)
    activity = [dict(row) for row in session.execute(select(ledger.c.business_date, ledger.c.event,
        func.count(func.distinct(ledger.c.document_id)).label("quantity")).where(*filters,
        ledger.c.event.in_(["receipt", "issue"])).group_by(ledger.c.business_date, ledger.c.event)
        .order_by(ledger.c.business_date, ledger.c.event)).mappings()]
    ranking = [dict(row) for row in session.execute(select(ledger.c.sku, ledger.c.name, ledger.c.unit, ledger.c.image_key,
        func.count(func.distinct(ledger.c.document_id)).label("issue_count"),
        (-func.sum(ledger.c.quantity_delta)).label("quantity")).where(*filters, ledger.c.event == "issue")
        .group_by(ledger.c.part_id, ledger.c.sku, ledger.c.name, ledger.c.unit, ledger.c.image_key)
        .order_by(func.count(func.distinct(ledger.c.document_id)).desc(), ledger.c.part_id)).mappings()]
    return {"part_id": part_id, "part_name": part.name if part else None,
        "date_from": start, "date_to": end, "warehouse_id": warehouse_id, "snapshot_date": date.today(),
        "groups": groups, **(groups[0] if len(groups) == 1 else {}),
        "activity_trend": activity, "consumption_ranking": ranking}
