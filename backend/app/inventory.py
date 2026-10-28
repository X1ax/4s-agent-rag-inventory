import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import AuditLog, OpeningDocument, OpeningLine, OperationRequest, Part, StockBalance, StockLedger, Warehouse
from backend.app.schemas import OpeningInput
from backend.app.costing import money


def audit(session, operator_id, action, entity, entity_id):
    session.add(AuditLog(operator_id=operator_id, action=action, entity=entity, entity_id=entity_id))


def post_opening(session: Session, payload: OpeningInput, operator_id: int):
    """Caller commits document, balance, ledger and audit together; no separate stock-edit path."""
    values = payload.model_dump(mode="json", exclude={"request_id"})
    values["lines"] = sorted(values["lines"], key=lambda line: line["part_id"])
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Serialize initialization in a warehouse and synchronize with catalog changes.
    warehouse = session.scalar(select(Warehouse).where(Warehouse.id == payload.warehouse_id).with_for_update())
    if session.get(OperationRequest, str(payload.request_id)):
        raise HTTPException(409, "请求标识已经用于其他业务单据")
    existing = session.scalar(select(OpeningDocument).where(OpeningDocument.request_id == str(payload.request_id)).with_for_update())
    if existing:
        if existing.payload_hash != digest or existing.operator_id != operator_id:
            raise HTTPException(409, "请求标识已用于其他参数或操作人")
        return existing, True
    if warehouse is None or not warehouse.active:
        raise HTTPException(409, "仓库不存在或已停用")
    document = OpeningDocument(request_id=str(payload.request_id), payload_hash=digest,
        warehouse_id=warehouse.id, operator_id=operator_id, business_date=payload.business_date, note=payload.note)
    session.add(document)
    session.flush()
    for item in sorted(payload.lines, key=lambda line: line.part_id):
        part = session.scalar(select(Part).where(Part.id == item.part_id).with_for_update())
        if part is None or not part.active:
            raise HTTPException(409, "备件不存在或已停用")
        balance = session.scalar(select(StockBalance).where(StockBalance.warehouse_id == warehouse.id, StockBalance.part_id == part.id).with_for_update())
        if balance is not None:
            raise HTTPException(409, f"备件 {part.sku} 已有库存记录，不能重复登记期初库存")
        line = OpeningLine(document_id=document.id, part_id=part.id, quantity=item.quantity, unit_cost=item.unit_cost, cost_source=item.cost_source)
        session.add(line)
        session.flush()
        value = money(item.unit_cost * item.quantity)
        session.add(StockBalance(warehouse_id=warehouse.id, part_id=part.id, quantity=item.quantity, stock_value=value))
        session.add(StockLedger(warehouse_id=warehouse.id, part_id=part.id, opening_line_id=line.id, quantity_delta=item.quantity, value_delta=value))
    audit(session, operator_id, "opening.post", "opening_documents", document.id)
    session.flush()
    return document, False
