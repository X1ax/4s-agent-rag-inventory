"""Add fixed demo history through the M2 services without resetting existing records."""
import json
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.documents import create_document, lock_catalogs, receive_purchase, transition
from backend.seed_reviews import review_simulated_document
from backend.app.models import Document, DocumentLine, Part, StockBalance, Supplier, User, Warehouse
from backend.app.schemas import ActionInput, DocumentInput, ReceiptInput
from backend.seed_demo import PARTS, existing_simulated_document, register_simulated_prices

HISTORY_START = date(2026, 8, 23)
HISTORY_DAYS = 60
HISTORY_END = HISTORY_START + timedelta(days=HISTORY_DAYS - 1)
HISTORY_NOTE = "M2 历史演示 v1"
INTERVALS = (1, 2, 3, 1, 5, 4, 6, 3, 2, 1, 12, 4)
BASE_QUANTITIES = (3, 2, 1, 2, 1, 1, 1, 1, 2, 4, 1, 1)
LEAD_DAYS = (3, 5, 7)


def import_history(session, operator_id, warehouse_ids, part_ids, supplier_ids):
    """The caller commits the entire import; retries reuse stable request identifiers."""
    def request_id(name):
        return uuid5(NAMESPACE_URL, "fours-demo-history-v1:" + name)

    def create(kind, warehouse_id, day, lines, name, supplier_id=None):
        old = existing_simulated_document(session, request_id(name + ":create"), operator_id,
            kind, warehouse_id, day, lines, supplier_id=supplier_id)
        if old is not None:
            return old
        if kind == 'purchase':
            for line in lines:
                quote = register_simulated_prices(session, operator_id, line['part_id'], supplier_id, line['unit_price'])
                line['unit_price'] = quote.supply_price
        else:
            for line in lines:
                register_simulated_prices(session, operator_id, line['part_id'])
        payload = DocumentInput(request_id=request_id(name + ":create"), kind=kind,
            warehouse_id=warehouse_id, supplier_id=supplier_id, business_date=day,
            note=f"{HISTORY_NOTE}：{name}", lines=lines,
            issue_type='paid_repair' if kind == 'issue' else None)
        return create_document(session, payload, operator_id)["id"]

    lock_catalogs(session, warehouse_ids, part_ids, supplier_ids)
    for batch in range(0, HISTORY_DAYS, 7):
        first_day = HISTORY_START + timedelta(days=batch)
        for warehouse_index, warehouse_id in enumerate(warehouse_ids):
            daily = []
            for offset in range(batch, min(batch + 7, HISTORY_DAYS)):
                lines = []
                for index, part_id in enumerate(part_ids):
                    if (offset + index + warehouse_index) % INTERVALS[index] == 0:
                        quantity = BASE_QUANTITIES[index] + int((offset + warehouse_index) % 3 == 0)
                        if warehouse_index == 1:
                            quantity = max(1, quantity - 1)
                        lines.append({"part_id": part_id, "quantity": quantity})
                daily.append((HISTORY_START + timedelta(days=offset), lines))

            # Each batch supplies exactly its scheduled consumption. The extra history
            # ends at zero net stock change, preserving the existing low-stock scenarios.
            for supplier_index, supplier_id in enumerate(supplier_ids):
                lines = []
                for index, part_id in enumerate(part_ids):
                    group = 0 if index < 3 else 1 if index < 6 else 2
                    if group != supplier_index:
                        continue
                    quantity = sum(item["quantity"] for _, items in daily for item in items if item["part_id"] == part_id)
                    if quantity:
                        lines.append({"part_id": part_id, "quantity": quantity, "unit_price": PARTS[index][4]})
                name = f"{warehouse_id}:{first_day}:供应组{supplier_index}"
                ordered_day = first_day - timedelta(days=LEAD_DAYS[supplier_index])
                purchase_id = create("purchase", warehouse_id, ordered_day, lines, name, supplier_id)
                review_simulated_document(session, purchase_id, operator_id)
                transition(session, purchase_id, "confirm", ActionInput(
                    request_id=request_id(name + ":confirm"), business_date=ordered_day), operator_id)
                purchase_lines = list(session.scalars(select(DocumentLine).where(
                    DocumentLine.document_id == purchase_id).order_by(DocumentLine.part_id)))
                receive_purchase(session, purchase_id, ReceiptInput(request_id=request_id(name + ":receipt"),
                    business_date=first_day, note=f"{HISTORY_NOTE}：{name}:实收",
                    lines=[{"purchase_line_id": line.id, "quantity": line.quantity} for line in purchase_lines]), operator_id)

            for day, lines in daily:
                name = f"{warehouse_id}:{day}:维修领用"
                document_id = create("issue", warehouse_id, day, lines, name)
                transition(session, document_id, "post", ActionInput(
                    request_id=request_id(name + ":post"), business_date=day), operator_id)


def seed():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("History import is restricted to fours_agent_dev / fours_dev")
    if date.today() < HISTORY_END:
        raise RuntimeError("The fixed history includes future business dates")
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == "demo_admin"))
            warehouses = [session.scalar(select(Warehouse).where(Warehouse.code == f"DEMO-W{i+1:02}")) for i in range(2)]
            suppliers = [session.scalar(select(Supplier).where(Supplier.code == f"DEMO-S{i+1:02}")) for i in range(3)]
            parts = [session.scalar(select(Part).where(Part.sku == f"DEMO-{i+1:03}")) for i in range(12)]
            if operator is None or not operator.active or operator.role != "admin":
                raise RuntimeError("An active demo administrator is required; run the M1 import first")
            if any(row is None or not row.active for row in warehouses + suppliers + parts):
                raise RuntimeError("Required demo catalogs are missing or disabled; existing records were not changed")
            if any(row.unit != PARTS[index][2] for index, row in enumerate(parts)):
                raise RuntimeError("Demo part units have changed; history import stopped")
            before = dict(session.execute(select(StockBalance.id, StockBalance.quantity)).all())
            import_history(session, operator.id, [row.id for row in warehouses],
                [row.id for row in parts], [row.id for row in suppliers])
            after = dict(session.execute(select(StockBalance.id, StockBalance.quantity)).all())
            if any(after.get(row_id) != quantity for row_id, quantity in before.items()):
                raise RuntimeError("Existing balances changed; the entire history import will be rolled back")
            summary = {"source": "fixed_simulation_v1", "consumption_from": str(HISTORY_START),
                "consumption_to": str(HISTORY_END), "complete_days": HISTORY_DAYS,
                "history_documents": session.scalar(select(func.count()).select_from(Document).where(Document.note.startswith(HISTORY_NOTE))),
                "existing_balances_preserved": True}
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    seed()
