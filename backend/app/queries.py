from sqlalchemy import func, literal, select, true

from backend.app.models import Document, DocumentLine, OpeningDocument, OpeningLine, Part, StockBalance, StockLedger, User, Warehouse


def balance_statement():
    pending = select(Document.warehouse_id.label("warehouse_id"), DocumentLine.part_id.label("part_id"),
        func.sum(DocumentLine.quantity - DocumentLine.received_quantity).label("quantity")).join(DocumentLine, DocumentLine.document_id == Document.id).where(
        Document.kind == "purchase", Document.status.in_(["confirmed", "partial"])).group_by(Document.warehouse_id, DocumentLine.part_id).subquery()
    transit = select(Document.target_warehouse_id.label("warehouse_id"), DocumentLine.part_id.label("part_id"),
        func.sum(DocumentLine.quantity).label("quantity")).join(DocumentLine, DocumentLine.document_id == Document.id).where(
        Document.kind == "transfer", Document.status == "sent").group_by(Document.target_warehouse_id, DocumentLine.part_id).subquery()
    quantity = func.coalesce(StockBalance.quantity, 0)
    return select(Warehouse.id.label("warehouse_id"), Warehouse.name.label("warehouse"), Part.id.label("part_id"), Part.sku, Part.name, Part.unit, Part.image_key,
        Part.safety_stock, Part.active.label("part_active"), Warehouse.active.label("warehouse_active"), StockBalance.stock_value, quantity.label("quantity"),
        quantity.label("available"), func.coalesce(pending.c.quantity, 0).label("purchase_pending"), func.coalesce(transit.c.quantity, 0).label("in_transit"),
        StockBalance.id.is_not(None).label("initialized")).select_from(Warehouse).join(Part, true()).outerjoin(StockBalance,
        (StockBalance.warehouse_id == Warehouse.id) & (StockBalance.part_id == Part.id)).outerjoin(pending,
        (pending.c.warehouse_id == Warehouse.id) & (pending.c.part_id == Part.id)).outerjoin(transit,
        (transit.c.warehouse_id == Warehouse.id) & (transit.c.part_id == Part.id))


def ledger_statement():
    return select(StockLedger.id, func.coalesce(Document.id, OpeningDocument.id).label("document_id"),
        func.coalesce(Document.kind, literal("opening")).label("document_kind"), OpeningDocument.request_id,
        func.coalesce(StockLedger.business_date, OpeningDocument.business_date).label("business_date"),
        func.coalesce(Document.note, OpeningDocument.note).label("note"), StockLedger.event,
        Warehouse.id.label("warehouse_id"), Warehouse.name.label("warehouse"), Part.id.label("part_id"), Part.sku, Part.name, Part.unit, Part.image_key,
        StockLedger.quantity_delta, User.display_name.label("operator"), StockLedger.created_at).select_from(StockLedger).outerjoin(OpeningLine,
        OpeningLine.id == StockLedger.opening_line_id).outerjoin(OpeningDocument, OpeningDocument.id == OpeningLine.document_id).outerjoin(DocumentLine,
        DocumentLine.id == StockLedger.document_line_id).outerjoin(Document, Document.id == DocumentLine.document_id).join(Warehouse,
        Warehouse.id == StockLedger.warehouse_id).join(Part, Part.id == StockLedger.part_id).join(User,
        User.id == func.coalesce(StockLedger.operator_id, OpeningDocument.operator_id))
