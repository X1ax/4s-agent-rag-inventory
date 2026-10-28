"""Shared business transactions for manual pages and future Agent tools."""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.inventory import audit
from backend.app.models import Document, DocumentLine, OpeningDocument, OperationRequest, Part, StockBalance, StockLedger, Supplier, Warehouse
from backend.app.reviews import require_review
from backend.app.costing import allocated_cost, money
from backend.app.models import SupplierPrice


def request_digest(payload, action):
    values = payload.model_dump(mode="json", exclude={"request_id"})
    for key in ('issue_type', 'warranty_reason', 'work_order'):
        if not values.get(key):
            values.pop(key, None)
    if "lines" in values:
        for line in values["lines"]:
            if line.get('quote_version') is None:
                line.pop('quote_version', None)
            if line.get("supplier_id") is None:
                line.pop("supplier_id", None)  # Preserve hashes of legacy single-supplier requests.
        values["lines"] = sorted(values["lines"], key=lambda line: line.get("part_id", line.get("purchase_line_id")))
    return hashlib.sha256(json.dumps({"action": action, "payload": values}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(session, payload, action, operator_id):
    digest = request_digest(payload, action)
    old = session.get(OperationRequest, str(payload.request_id), populate_existing=True)
    if session.scalar(select(OpeningDocument.id).where(OpeningDocument.request_id == str(payload.request_id))):
        raise HTTPException(409, "请求标识已经用于期初登记")
    if old:
        if old.payload_hash != digest or old.operator_id != operator_id:
            raise HTTPException(409, "请求标识已用于其他参数、动作或操作人")
        return {"id": old.document_id, "status": old.result_status, "replayed": True}


def finish(session, document, payload, action, operator_id):
    session.add(OperationRequest(request_id=str(payload.request_id), payload_hash=request_digest(payload, action),
        operator_id=operator_id, document_id=document.id, result_status=document.status))
    audit(session, operator_id, action, "documents", document.id)
    session.flush()
    return {"id": document.id, "status": document.status, "replayed": False}


def lock_catalogs(session, warehouse_ids, part_ids, supplier_ids=()):
    # ponytail: serialize operations per warehouse; consider narrower balance locks only if throughput requires it.
    for warehouse_id in sorted(set(warehouse_ids)):
        row = session.scalar(select(Warehouse).where(Warehouse.id == warehouse_id).with_for_update())
        if row is None or not row.active:
            raise HTTPException(409, "仓库不存在或已停用")
    for supplier_id in sorted(set(supplier_ids)):
        row = session.scalar(select(Supplier).where(Supplier.id == supplier_id).with_for_update(read=True))
        if row is None or not row.active:
            raise HTTPException(409, "供应商不存在或已停用")
    for part_id in sorted(set(part_ids)):
        row = session.scalar(select(Part).where(Part.id == part_id).with_for_update())
        if row is None or not row.active:
            raise HTTPException(409, "备件不存在或已停用")


def check_supply(session, part_id, supplier_id):
    part, supplier = session.get(Part, part_id), session.get(Supplier, supplier_id)
    if supplier is None or part is None or part.category not in supplier.supply_categories:
        raise HTTPException(409, "所选供应商不供应该备件类别，请维护供货范围或重新选择")


def priced_lines(session, payload):
    """Resolve server-owned prices once; callers may inspect but cannot override them."""
    result = []
    for item in sorted(payload.lines, key=lambda line: line.part_id):
        values = item.model_dump()
        if payload.kind == 'purchase':
            supplier_id = item.supplier_id or payload.supplier_id
            check_supply(session, item.part_id, supplier_id)
            quote = session.scalar(select(SupplierPrice).where(SupplierPrice.part_id==item.part_id,
                SupplierPrice.supplier_id==supplier_id).with_for_update(read=True))
            if quote is None or not quote.active:
                raise HTTPException(409, '供应商尚无该备件的有效供货报价，请管理员维护后再采购')
            if item.quote_version is not None and item.quote_version != quote.version:
                raise HTTPException(409, '供应商报价已变化，请重新查询并确认')
            if 'unit_price' in item.model_fields_set and item.unit_price != quote.supply_price:
                raise HTTPException(409, '采购单价必须使用所选供应商报价，不能由员工或助手修改')
            values.update(supplier_id=supplier_id, unit_price=quote.supply_price, quote_version=quote.version, pricing_source='supplier_quote')
        elif payload.kind == 'issue':
            part = session.get(Part, item.part_id)
            if payload.issue_type == 'paid_repair' and (part.sale_price is None or part.sale_price <= 0):
                raise HTTPException(409, '收费维修备件尚未设置门店定价，请管理员或经理维护')
            values.update(sale_unit_price=part.sale_price if payload.issue_type == 'paid_repair' else money(0),
                sale_price_version=part.sale_price_version if payload.issue_type == 'paid_repair' else None,
                pricing_source='store_price' if payload.issue_type == 'paid_repair' else 'warranty')
        result.append(values)
    return result


def change_stock(session, line, warehouse_id, delta, event, business_date, operator_id):
    row = session.scalar(select(StockBalance).where(StockBalance.warehouse_id == warehouse_id, StockBalance.part_id == line.part_id).with_for_update())
    quantity = (row.quantity if row else 0) + delta
    if quantity < 0:
        raise HTTPException(409, f"备件 #{line.part_id} 库存不足，当前可用 {row.quantity if row else 0}")
    if quantity > 1_000_000_000:
        raise HTTPException(409, "库存数量超过首版支持上限")
    old_quantity = row.quantity if row else 0
    old_value = row.stock_value if row else money(0)
    if old_quantity == 0:
        old_value = money(0)
    if old_value is None:
        raise HTTPException(409, '库存成本尚未初始化，请先核对成本资料')
    if delta < 0:
        value_delta = -allocated_cost(old_value, old_quantity, -delta)
        line.cost_amount = -value_delta
        line.pricing_source = line.pricing_source or 'actual_weighted_cost'
    elif event == 'receipt':
        value_delta = money(line.unit_price * delta)
        line.cost_amount = value_delta
        line.pricing_source = 'actual_purchase_receipt'
    elif event == 'transfer_receive':
        if line.cost_amount is None:
            raise HTTPException(409, '调拨发出成本缺失，不能按当前报价替代')
        value_delta = line.cost_amount
    else:
        raise HTTPException(409, '不支持该库存计价动作')
    stock_value = money(old_value + value_delta)
    if row is None:
        row = StockBalance(warehouse_id=warehouse_id, part_id=line.part_id, quantity=quantity, stock_value=stock_value)
        session.add(row)
    else:
        row.quantity = quantity
        row.stock_value = stock_value
    session.add(StockLedger(warehouse_id=warehouse_id, part_id=line.part_id, document_line_id=line.id,
        quantity_delta=delta, value_delta=value_delta, event=event, business_date=business_date, operator_id=operator_id))


def create_document(session, payload, operator_id):
    action = f"{payload.kind}.create"
    old = replay(session, payload, action, operator_id)
    if old:
        return old
    warehouses = [payload.warehouse_id] + ([payload.target_warehouse_id] if payload.target_warehouse_id else [])
    supplier_ids = {line.supplier_id or payload.supplier_id for line in payload.lines} if payload.kind == "purchase" else set()
    lock_catalogs(session, warehouses, [line.part_id for line in payload.lines], supplier_ids)
    if payload.kind == "purchase":
        for item in payload.lines:
            check_supply(session, item.part_id, item.supplier_id or payload.supplier_id)
    old = replay(session, payload, action, operator_id)
    if old:
        return old
    resolved = priced_lines(session, payload)
    document = Document(kind=payload.kind, status="draft", warehouse_id=payload.warehouse_id,
        target_warehouse_id=payload.target_warehouse_id, supplier_id=next(iter(supplier_ids)) if len(supplier_ids) == 1 else None,
        operator_id=operator_id, business_date=payload.business_date, note=payload.note, issue_type=payload.issue_type,
        warranty_reason=payload.warranty_reason, work_order=payload.work_order, pricing_source='live_transaction')
    session.add(document)
    session.flush()
    for values in resolved:
        session.add(DocumentLine(document_id=document.id, **values))
    return finish(session, document, payload, action, operator_id)


def document_for_update(session, document_id):
    document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if document is None:
        raise HTTPException(404, "单据不存在")
    return document


def transition(session, document_id, verb, payload, operator_id):
    document = document_for_update(session, document_id)
    action = f"{document.kind}.{verb}:{document_id}"
    old = replay(session, payload, action, operator_id)
    if old:
        return old
    lines = list(session.scalars(select(DocumentLine).where(DocumentLine.document_id == document.id).order_by(DocumentLine.part_id)))
    if verb == "cancel":
        valid = document.status == "draft" or (document.kind == "purchase" and document.status in {"confirmed", "partial"})
        if not valid or document.kind == "receipt":
            raise HTTPException(409, "当前状态不能取消；已生效的库存变化不能撤销")
        document.status = "cancelled"
    else:
        expected = {("purchase", "confirm"): ("draft", "confirmed"), ("issue", "post"): ("draft", "posted"),
            ("transfer", "send"): ("draft", "sent"), ("transfer", "receive"): ("sent", "received")}
        states = expected.get((document.kind, verb))
        if not states or document.status != states[0]:
            raise HTTPException(409, "当前单据类型或状态不能执行该动作")
        if verb in {'confirm', 'send'}:
            require_review(session, document)
        if verb == 'post' and (document.issue_type not in {'paid_repair','warranty'} or any(line.sale_unit_price is None for line in lines)):
            raise HTTPException(409, '出库用途或结算价格缺失，请取消旧草稿并重新创建')
        if payload.business_date < document.business_date:
            raise HTTPException(409, "操作日期不能早于单据业务日期")
        warehouses = [document.warehouse_id] + ([document.target_warehouse_id] if document.target_warehouse_id else [])
        lock_catalogs(session, warehouses, [line.part_id for line in lines], [line.supplier_id for line in lines if line.supplier_id])
        if verb == "confirm":
            for line in lines:
                check_supply(session, line.part_id, line.supplier_id)
        if verb == "receive":
            sent_date = session.scalar(select(StockLedger.business_date).where(StockLedger.document_line_id == lines[0].id, StockLedger.event == "transfer_send"))
            if sent_date and payload.business_date < sent_date:
                raise HTTPException(409, "收货日期不能早于发出日期")
        for line in lines:
            if verb == "post":
                change_stock(session, line, document.warehouse_id, -line.quantity, "issue", payload.business_date, operator_id)
            elif verb == "send":
                change_stock(session, line, document.warehouse_id, -line.quantity, "transfer_send", payload.business_date, operator_id)
            elif verb == "receive":
                change_stock(session, line, document.target_warehouse_id, line.quantity, "transfer_receive", payload.business_date, operator_id)
        document.status = states[1]
    return finish(session, document, payload, action, operator_id)


def receive_purchase(session, purchase_id, payload, operator_id):
    purchase = document_for_update(session, purchase_id)
    action = f"purchase.receipt:{purchase_id}"
    old = replay(session, payload, action, operator_id)
    if old:
        return old
    if purchase.kind != "purchase" or purchase.status not in {"confirmed", "partial"}:
        raise HTTPException(409, "仅已确认或部分收货的采购单可以入库")
    if payload.business_date < purchase.business_date:
        raise HTTPException(409, "收货日期不能早于采购单业务日期")
    lines = list(session.scalars(select(DocumentLine).where(DocumentLine.document_id == purchase.id).order_by(DocumentLine.part_id).with_for_update()))
    by_id = {line.id: line for line in lines}
    for item in payload.lines:
        line = by_id.get(item.purchase_line_id)
        if line is None or line.received_quantity + item.quantity > line.quantity:
            raise HTTPException(409, "收货明细不属于本单，或累计收货超过采购数量")
    supplier_ids = {by_id[item.purchase_line_id].supplier_id for item in payload.lines}
    lock_catalogs(session, [purchase.warehouse_id], [by_id[item.purchase_line_id].part_id for item in payload.lines], [s for s in supplier_ids if s])
    receipt = Document(kind="receipt", status="posted", warehouse_id=purchase.warehouse_id, purchase_id=purchase.id,
        supplier_id=next(iter(supplier_ids)) if len(supplier_ids) == 1 else None, operator_id=operator_id, business_date=payload.business_date, note=payload.note)
    session.add(receipt)
    session.flush()
    for item in sorted(payload.lines, key=lambda item: by_id[item.purchase_line_id].part_id):
        source = by_id[item.purchase_line_id]
        line = DocumentLine(document_id=receipt.id, part_id=source.part_id, quantity=item.quantity,
            unit_price=source.unit_price, purchase_line_id=source.id, supplier_id=source.supplier_id)
        source.received_quantity += item.quantity
        session.add(line)
        session.flush()
        change_stock(session, line, purchase.warehouse_id, item.quantity, "receipt", payload.business_date, operator_id)
    purchase.status = "completed" if all(line.received_quantity == line.quantity for line in lines) else "partial"
    audit(session, operator_id, "purchase.receive", "documents", purchase.id)
    return finish(session, receipt, payload, action, operator_id)
