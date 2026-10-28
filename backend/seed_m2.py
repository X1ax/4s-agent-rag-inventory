"""Explicit, additive demonstration events; existing M1 stock and user operations are preserved."""
from datetime import date
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.documents import create_document, lock_catalogs, receive_purchase, transition
from backend.app.inventory import audit
from backend.app.models import DocumentLine, Part, Supplier, User, Warehouse
from backend.app.schemas import ActionInput, DocumentInput, ReceiptInput
from backend.seed_demo import PARTS, existing_simulated_document, register_simulated_prices
from backend.seed_reviews import review_simulated_document

PART_IMAGE_KEYS = ["part-oil-filter", "part-air-filter", "part-cabin-filter", "part-engine-oil", "part-brake-fluid", "part-coolant", "part-brake-pads", "part-wipers", "part-spark-plug", "part-washer", "part-battery", "part-bulb"]


def request_id(name):
    return uuid5(NAMESPACE_URL, "fours-demo-m2-" + name)


def seed():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("M2 demo import is restricted to fours_agent_dev / fours_dev")
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == "demo_admin"))
            if operator is None or not operator.active or operator.role != "admin":
                raise RuntimeError("Run the M1 import first; an active demo administrator is required")
            parts = {row.sku: row for row in session.scalars(select(Part).where(Part.sku.like("DEMO-%")))}
            warehouses = {row.code: row for row in session.scalars(select(Warehouse).where(Warehouse.code.like("DEMO-%")))}
            suppliers = {row.code: row for row in session.scalars(select(Supplier).where(Supplier.code.like("DEMO-%")))}
            key_by_sku = {f"DEMO-{index+1:03}": key for index, key in enumerate(PART_IMAGE_KEYS)}
            for row in parts.values():
                if row.sku in key_by_sku and row.image_key in {"", "parts-filter", "parts-fluids", "parts-service"}:
                    row.image_key = key_by_sku[row.sku]
                    audit(session, operator.id, "catalog.image", "parts", row.id)
            for code, key in (("DEMO-W01", "warehouse-main"), ("DEMO-W02", "warehouse-service")):
                if not warehouses[code].image_key:
                    warehouses[code].image_key = key
                    audit(session, operator.id, "catalog.image", "warehouses", warehouses[code].id)
            for code, key in (("DEMO-S01", "supplier-maintenance"), ("DEMO-S02", "supplier-fluids"), ("DEMO-S03", "supplier-general")):
                if not suppliers[code].image_key:
                    suppliers[code].image_key = key
                    audit(session, operator.id, "catalog.image", "suppliers", suppliers[code].id)
            main, quick = warehouses["DEMO-W01"].id, warehouses["DEMO-W02"].id
            lock_catalogs(session, [main, quick], [row.id for row in parts.values()], [row.id for row in suppliers.values()])

            def create(name, kind, day, items, **extra):
                lines = [{"part_id": parts[f"DEMO-{number:03}"].id, "quantity": qty,
                    "unit_price": PARTS[number - 1][4] if kind == "purchase" else "0.00"} for number, qty in items]
                old = existing_simulated_document(session, request_id(name + "-create"), operator.id,
                    kind, main, date(2026, 10, day), lines, **extra)
                if old is not None:
                    return old
                if kind == 'purchase':
                    for line in lines:
                        quote = register_simulated_prices(session, operator.id, line['part_id'], extra['supplier_id'], line['unit_price'])
                        line['unit_price'] = quote.supply_price
                elif kind == 'issue':
                    for line in lines:
                        register_simulated_prices(session, operator.id, line['part_id'])
                payload = DocumentInput(request_id=request_id(name + "-create"), kind=kind, warehouse_id=main,
                    business_date=date(2026, 10, day), note="M2 固定模拟场景：" + name,
                    lines=lines, issue_type='paid_repair' if kind == 'issue' else None, **extra)
                return create_document(session, payload, operator.id)["id"]

            def act(name, document_id, verb, day):
                if verb in {'confirm','send'}:
                    review_simulated_document(session, document_id, operator.id)
                return transition(session, document_id, verb, ActionInput(request_id=request_id(name), business_date=date(2026, 10, day)), operator.id)

            def receive(name, document_id, day, quantity):
                line = session.scalar(select(DocumentLine).where(DocumentLine.document_id == document_id).order_by(DocumentLine.id))
                return receive_purchase(session, document_id, ReceiptInput(request_id=request_id(name), business_date=date(2026, 10, day),
                    note="M2 模拟采购实际收货", lines=[{"purchase_line_id": line.id, "quantity": quantity}]), operator.id)

            order = create("采购完成", "purchase", 2, [(1, 10)], supplier_id=suppliers["DEMO-S01"].id)
            act("采购完成-确认", order, "confirm", 2); receive("采购完成-入库", order, 3, 10)
            order = create("采购部分收货", "purchase", 4, [(2, 20)], supplier_id=suppliers["DEMO-S01"].id)
            act("采购部分收货-确认", order, "confirm", 4); receive("采购部分收货-入库", order, 5, 8)
            for name, day, items in (("常规保养领用", 6, [(1, 6), (2, 3)]), ("滤清器领用", 10, [(1, 2)]),
                ("制动液领用", 15, [(5, 2)]), ("雨刮片领用", 19, [(8, 2)]), ("电气件领用", 21, [(11, 1)])):
                order = create(name, "issue", day, items); act(name + "-过账", order, "post", day)
            order = create("快修仓补充在途", "transfer", 21, [(1, 5)], target_warehouse_id=quick)
            act("快修仓补充在途-发出", order, "send", 21)
            order = create("垫圈调拨完成", "transfer", 12, [(10, 4)], target_warehouse_id=quick)
            act("垫圈调拨完成-发出", order, "send", 12); act("垫圈调拨完成-收货", order, "receive", 13)
            order = create("采购取消演示", "purchase", 16, [(4, 12)], supplier_id=suppliers["DEMO-S02"].id)
            act("采购取消演示-确认", order, "confirm", 16); act("采购取消演示-取消", order, "cancel", 17)
            create("待核对领用草稿", "issue", 22, [(9, 1)])
        print("M2 demo events imported without resetting records; blank or superseded demo images updated.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    seed()
