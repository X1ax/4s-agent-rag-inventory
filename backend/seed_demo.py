"""Explicit, additive M1 demo import. Never runs on application startup."""
import re
import secrets
from datetime import date
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.inventory import audit, post_opening
from backend.app.models import Document, DocumentLine, OpeningDocument, OpeningLine, OperationRequest, Part, PriceChange, Supplier, SupplierPrice, User, Warehouse, utcnow
from backend.app.schemas import OpeningInput
from backend.app.security import passwords
from backend.backfill_pricing import candidate_price

DEMO_DATE = date(2026, 10, 1)
PARTS = [
    ("机油滤清器", "滤清器", "件", 10, "35.00"),
    ("空气滤清器", "滤清器", "件", 12, "68.00"),
    ("空调滤清器", "滤清器", "件", 8, "59.00"),
    ("合成机油（整桶）", "油液", "件", 15, "260.00"),
    ("制动液（整瓶）", "油液", "件", 6, "85.00"),
    ("冷却液（整桶）", "油液", "件", 8, "95.00"),
    ("前制动片（整套）", "制动件", "件", 4, "320.00"),
    ("雨刮片（整套）", "易损件", "件", 6, "110.00"),
    ("火花塞", "点火件", "件", 8, "75.00"),
    ("密封垫圈", "紧固密封", "件", 10, "8.00"),
    ("蓄电池", "电气件", "件", 3, "580.00"),
    ("灯泡", "电气件", "件", 5, "28.00"),
]
QUANTITIES = ([30, 8, 0, 40, 4, 15, 0, 20, 2, 18, 6, 9], [10, 0, 5, 12, 2, 0, 3, 5, 0, 4, 1, 0])
SIMULATED_PRICE_SOURCE = "显式模拟数据定价 v1"


def register_simulated_prices(session, operator_id, part_id, supplier_id=None, supply_price=None):
    """Explicit seed-only administration. Existing manually maintained prices are preserved."""
    operator = session.get(User, operator_id)
    if operator is None or not operator.active or operator.role != 'admin':
        raise ValueError('Simulated pricing requires an active administrator')
    part = session.scalar(select(Part).where(Part.id == part_id).with_for_update())
    quote = None
    if supplier_id is not None:
        supplier = session.get(Supplier, supplier_id)
        if supplier is None or not supplier.active or part.category not in supplier.supply_categories:
            raise ValueError('Simulation supplier is not qualified for this part')
        quote = session.scalar(select(SupplierPrice).where(SupplierPrice.part_id == part_id,
            SupplierPrice.supplier_id == supplier_id).with_for_update())
        amount = Decimal(supply_price)
        if quote is not None and not quote.active:
            raise ValueError('Existing supplier quotation is disabled; simulation does not reactivate it')
        if quote is None or (quote.source == SIMULATED_PRICE_SOURCE and quote.supply_price != amount):
            old = quote.supply_price if quote else None
            if quote is None:
                quote = SupplierPrice(part_id=part_id, supplier_id=supplier_id, version=0, active=True)
                session.add(quote)
            quote.supply_price, quote.source = amount, SIMULATED_PRICE_SOURCE
            quote.version += 1
            quote.updated_at = utcnow()
            session.add(PriceChange(part_id=part_id, supplier_id=supplier_id, kind='supply',
                before_price=old, after_price=amount, source=SIMULATED_PRICE_SOURCE, operator_id=operator_id))
            session.flush()
            audit(session, operator_id, 'demo.price.supply', 'supplier_prices', quote.id)
    if part.sale_price is None:
        anchor = part.reference_price if part.reference_price > 0 else (quote.supply_price if quote else None)
        if anchor is None or anchor <= 0:
            raise ValueError('A positive explicit simulation price anchor is required')
        part.sale_price = candidate_price(anchor, f'{part.sku}:retail', 125, 160)
        part.sale_price_version += 1
        session.add(PriceChange(part_id=part_id, kind='sale', before_price=None,
            after_price=part.sale_price, source=SIMULATED_PRICE_SOURCE, operator_id=operator_id))
        audit(session, operator_id, 'demo.price.sale', 'parts', part.id)
    session.flush()
    return quote


def existing_simulated_document(session, request_id, operator_id, kind, warehouse_id, day, lines, **extra):
    """Legacy seed retries reuse the frozen document, without rewriting its old request hash."""
    old = session.get(OperationRequest, str(request_id))
    if old is None:
        return None
    document = session.get(Document, old.document_id)
    actual = list(session.scalars(select(DocumentLine).where(DocumentLine.document_id == document.id)))
    expected = sorted((row['part_id'], row['quantity']) for row in lines)
    if (old.operator_id != operator_id or document.operator_id != operator_id or document.kind != kind
        or document.warehouse_id != warehouse_id or document.business_date != day
        or sorted((row.part_id, row.quantity) for row in actual) != expected
        or (extra.get('target_warehouse_id') is not None and document.target_warehouse_id != extra['target_warehouse_id'])
        or any((row.supplier_id or document.supplier_id) != (item.get('supplier_id') or extra.get('supplier_id'))
            for row in actual for item in lines if row.part_id == item['part_id'] and kind == 'purchase')):
        raise ValueError('Existing simulated request belongs to different business facts')
    return document.id


def post_simulated_opening(session, payload, operator_id):
    existing = session.scalar(select(OpeningDocument).where(OpeningDocument.request_id == str(payload.request_id)).with_for_update())
    if existing:
        actual = list(session.scalars(select(OpeningLine).where(OpeningLine.document_id == existing.id)))
        if (existing.operator_id != operator_id or existing.warehouse_id != payload.warehouse_id
            or existing.business_date != payload.business_date or existing.note != payload.note
            or sorted((row.part_id, row.quantity) for row in actual) != sorted((row.part_id, row.quantity) for row in payload.lines)):
            raise ValueError('Existing simulated opening belongs to different business facts')
        return existing, True
    return post_opening(session, payload, operator_id)


def initialize_local_secrets():
    path = ROOT / ".env"
    content = path.read_text(encoding="utf-8")
    values = dotenv_values(path)
    for key in ("JWT_SECRET", "DEMO_ADMIN_PASSWORD", "DEMO_STOREKEEPER_PASSWORD", "DEMO_MANAGER_PASSWORD"):
        if values.get(key):
            continue
        value = secrets.token_urlsafe(36 if key == "JWT_SECRET" else 18)
        pattern = rf"^{key}=.*$"
        if re.search(pattern, content, flags=re.MULTILINE):
            content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip() + f"\n{key}={value}\n"
    path.write_text(content, encoding="utf-8")
    return dotenv_values(path)


def seed():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("Demo import is restricted to fours_agent_dev / fours_dev")
    credentials = initialize_local_secrets()
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            accounts = {}
            for role, label in (("admin", "演示管理员"), ("storekeeper", "演示库管"), ("manager", "演示经理")):
                username = f"demo_{role}"
                user = session.scalar(select(User).where(User.username == username))
                if user is None:
                    user = User(username=username, display_name=label, role=role,
                        password_hash=passwords.hash(credentials[f"DEMO_{role.upper()}_PASSWORD"]))
                    session.add(user)
                    session.flush()
                    audit(session, user.id, "demo.user.create", "users", user.id)
                accounts[role] = user
            operator = accounts["admin"]
            if not operator.active or operator.role != "admin":
                raise RuntimeError("Existing demo administrator has been changed; import stopped without resetting it")

            def ensure(model, key, value, **fields):
                row = session.scalar(select(model).where(getattr(model, key) == value))
                if row is None:
                    row = model(**{key: value}, **fields)
                    session.add(row)
                    session.flush()
                    audit(session, operator.id, "demo.catalog.create", model.__tablename__, row.id)
                return row

            suppliers = [ensure(Supplier, "code", f"DEMO-S{i + 1:02}", name=name, contact="模拟联系人", phone="", lead_time_days=days,
                supply_categories=(["滤清器"] if i == 0 else ["油液"] if i == 1 else ["制动件", "易损件", "点火件", "紧固密封", "电气件"]))
                for i, (name, days) in enumerate((("模拟供应商·保养件", 3), ("模拟供应商·油液", 5), ("模拟供应商·综合件", 7)))]
            warehouses = [ensure(Warehouse, "code", f"DEMO-W{i + 1:02}", name=name, location="模拟门店库区")
                for i, name in enumerate(("模拟主仓", "模拟快修仓"))]
            parts = [ensure(Part, "sku", f"DEMO-{i + 1:03}", name=name, category=category, unit=unit,
                specification={4: "整桶按1件计数，不拆零；容量待核准", 5: "整瓶按1件计数，不拆零；容量待核准",
                    6: "整桶按1件计数，不拆零；容量待核准", 7: "整套按1件计数，不拆零；套内数量待核准",
                    8: "整套按1件计数，不拆零；套内数量待核准"}.get(i+1, ""),
                safety_stock=safety, reference_price=price, default_supplier_id=suppliers[0 if category == "滤清器" else 1 if category == "油液" else 2].id)
                for i, (name, category, unit, safety, price) in enumerate(PARTS)]
            for i, warehouse in enumerate(warehouses):
                payload = OpeningInput(request_id=uuid5(NAMESPACE_URL, f"fours-demo-m1-opening-{i}"),
                    warehouse_id=warehouse.id, business_date=DEMO_DATE, note="M1 固定模拟期初数据",
                    lines=[{"part_id": part.id, "quantity": qty, "unit_cost": PARTS[index][4],
                        "cost_source": "M1 固定模拟期初成本，不代表真实凭证"}
                        for index, (part, qty) in enumerate(zip(parts, QUANTITIES[i])) if qty > 0])
                post_simulated_opening(session, payload, operator.id)
        print("M1 demo imported: 3 accounts, 3 suppliers, 2 warehouses, 12 parts; existing records preserved.")
        print("Passwords are only in .env: DEMO_ADMIN_PASSWORD / DEMO_STOREKEEPER_PASSWORD / DEMO_MANAGER_PASSWORD.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    seed()
