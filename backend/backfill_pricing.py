"""Explicit, reproducible simulated historical pricing; never imported by app startup.

Functions participate in the caller's transaction. Stop business writes before CLI apply.
The JSON plan contains the original fields as a complete local backup.
"""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.costing import allocated_cost, money
from backend.app.db import make_engine
from backend.app.models import (AgentProposal, AuditLog, Document, DocumentLine, DocumentReview,
    OpeningDocument, OpeningLine, Part, PriceChange, PricingBackfill, StockBalance,
    StockLedger, Supplier, SupplierPrice, User, Warehouse)

SEED = 20261026
VERSION = "simulated_pricing_20261026_v1"
SOURCE = "模拟定价及历史成本补录20261026-v1"
TABLES = (Warehouse, Supplier, Part, OpeningDocument, OpeningLine, Document,
    DocumentLine, DocumentReview, StockBalance, StockLedger, SupplierPrice, AgentProposal)
MODEL_BY_TABLE = {model.__tablename__: model for model in TABLES}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def fixed_index(key, count):
    return int.from_bytes(hashlib.sha256(f"{SEED}:{key}".encode()).digest()[:8], "big") % count


def candidate_price(anchor, key, low, high):
    with localcontext() as context:
        context.prec = 40
        return money(Decimal(anchor) * Decimal(low + fixed_index(key, high-low+1)) / 100)


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def snapshot(session):
    # Include immutable historical fields too: quantity/price/date/operator changes invalidate a plan.
    return {model.__tablename__: [{column.key: json_value(getattr(row, column.key))
        for column in model.__table__.columns} for row in session.scalars(
            select(model).order_by(*model.__table__.primary_key).execution_options(populate_existing=True))]
        for model in TABLES}


def quantity_fingerprint(before):
    added_fields = {"opening_documents": (), "opening_lines": ("unit_cost", "cost_source"),
        "documents": ("issue_type", "warranty_reason", "work_order", "pricing_source"),
        "document_lines": ("sale_unit_price", "cost_amount", "quote_version", "sale_price_version", "pricing_source"),
        "document_reviews": (), "stock_balances": ("stock_value",), "stock_ledger": ("value_delta",)}
    return digest({table: [{key: value for key, value in row.items() if key not in fields}
        for row in before[table]] for table, fields in added_fields.items()})


def make_plan(session):
    """Read only. Return every proposed new-field change plus the immutable input snapshot."""
    with localcontext() as context:
        context.prec = 40
        return _make_plan(session)


def _make_plan(session):
    before = snapshot(session)
    changes, inserts = [], []

    def change(table, row, values):
        values = {key: json_value(value) for key, value in values.items()
            if row[key] != json_value(value)}
        if values:
            changes.append({"table": table, "id": row["id"],
                "before": {key: row[key] for key in values}, "after": values})

    parts = {row["id"]: row for row in before["parts"]}
    suppliers = {row["id"]: row for row in before["suppliers"]}
    documents = {row["id"]: row for row in before["documents"]}
    lines = {row["id"]: row for row in before["document_lines"]}
    openings = {row["id"]: row for row in before["opening_lines"]}
    opening_documents = {row["id"]: row for row in before["opening_documents"]}
    quotes = {(row["supplier_id"], row["part_id"]): row for row in before["supplier_prices"]}
    expected_events = set()
    for line in lines.values():
        doc = documents[line["document_id"]]
        events = (["receipt"] if doc["kind"] == "receipt" else
            ["issue"] if doc["kind"] == "issue" and doc["status"] == "posted" else
            ["transfer_send", "transfer_receive"] if doc["kind"] == "transfer" and doc["status"] == "received" else
            ["transfer_send"] if doc["kind"] == "transfer" and doc["status"] == "sent" else [])
        expected_events.update((line["id"], event) for event in events)
    actual_events = {(row["document_line_id"], row["event"]) for row in before["stock_ledger"]
        if row["document_line_id"] is not None}
    actual_openings = {row["opening_line_id"] for row in before["stock_ledger"] if row["opening_line_id"] is not None}
    if expected_events != actual_events or actual_openings != set(openings):
        raise ValueError("Posted document/opening ledger coverage mismatch")
    sale_prices, default_quotes = {}, {}
    for part in parts.values():
        anchor = Decimal(part["reference_price"])
        for supplier in suppliers.values():
            if not supplier["active"] or part["category"] not in supplier["supply_categories"]:
                continue
            key = (supplier["id"], part["id"])
            price = quotes.get(key)
            amount = Decimal(price["supply_price"]) if price else (money(anchor)
                if supplier["id"] == part["default_supplier_id"] else
                candidate_price(anchor, f'{part["sku"]}:{supplier["code"]}', 95, 105))
            if price is None:
                inserts.append({"table": "supplier_prices", "values": {"supplier_id": supplier["id"],
                    "part_id": part["id"], "supply_price": str(amount), "version": 1,
                    "active": True, "source": SOURCE}})
            if supplier["id"] == part["default_supplier_id"]:
                default_quotes[part["id"]] = amount
        if part["sale_price"] is None:
            amount = candidate_price(anchor, f'{part["sku"]}:retail', 125, 160)
            change("parts", part, {"sale_price": amount, "sale_price_version": 1})
        else:
            amount = Decimal(part["sale_price"])
        sale_prices[part["id"]] = amount

    first_receipt = {}
    for event in before["stock_ledger"]:
        if event["event"] == "receipt":
            first_receipt.setdefault(event["part_id"], Decimal(lines[event["document_line_id"]]["unit_price"]))
    opening_costs = {}
    for row in openings.values():
        if row["unit_cost"] is None:
            unit_cost = first_receipt.get(row["part_id"], default_quotes.get(row["part_id"]))
            if unit_cost is None:
                raise ValueError(f'No opening cost anchor for part {row["part_id"]}')
            origin = "最早实际入库成交价" if row["part_id"] in first_receipt else "默认供应商模拟报价"
            change("opening_lines", row, {"unit_cost": unit_cost,
                "cost_source": f"模拟期初成本补录；{origin}；{SOURCE}"})
        else:
            unit_cost = Decimal(row["unit_cost"])
        opening_costs[row["id"]] = unit_cost

    issue_types = Counter()
    for doc in documents.values():
        if doc["kind"] in ("purchase", "receipt") and not doc["pricing_source"]:
            change("documents", doc, {"pricing_source": "模拟历史原成交价保留；"+SOURCE})
        if doc["kind"] != "issue" or doc["status"] != "posted":
            continue
        purpose = doc["issue_type"] or ("warranty" if fixed_index(f'issue:{doc["id"]}', 100) < 15 else "paid_repair")
        if purpose not in ("warranty", "paid_repair"):
            raise ValueError("Unknown issue purpose")
        issue_types[purpose] += 1
        if doc["issue_type"] is None:
            change("documents", doc, {"issue_type": purpose, "pricing_source": SOURCE,
                "warranty_reason": "模拟历史质保维修补录，无真实质保凭证" if purpose == "warranty" else doc["warranty_reason"]})
        for line in lines.values():
            if line["document_id"] != doc["id"] or line["sale_unit_price"] is not None:
                continue
            part = parts[line["part_id"]]
            price = Decimal("0") if purpose == "warranty" else candidate_price(sale_prices[part["id"]],
                f'{part["sku"]}:{doc["business_date"][:7]}:historical_retail', 97, 103)
            change("document_lines", line, {"sale_unit_price": price, "pricing_source": SOURCE})

    for line in lines.values():
        if documents[line["document_id"]]["kind"] in ("purchase", "receipt") and not line["pricing_source"]:
            change("document_lines", line, {"pricing_source": "模拟历史原成交价保留；"+SOURCE})

    quantities, values = defaultdict(int), defaultdict(lambda: Decimal("0.00"))
    transit, line_costs = {}, {}
    inflow, issue_cost = Decimal("0.00"), Decimal("0.00")
    for event in before["stock_ledger"]:
        key = (event["warehouse_id"], event["part_id"])
        delta, kind = event["quantity_delta"], event["event"]
        line = lines.get(event["document_line_id"])
        doc = documents[line["document_id"]] if line else None
        if kind == "opening":
            opening = openings.get(event["opening_line_id"])
            if opening is None or delta != opening["quantity"] or event["part_id"] != opening["part_id"] or key[0] != opening_documents[opening["document_id"]]["warehouse_id"]:
                raise ValueError("Opening quantity/source mismatch")
            cost = money(opening_costs[opening["id"]] * delta)
            inflow += cost
        else:
            if line is None or line["part_id"] != event["part_id"] or abs(delta) != line["quantity"]:
                raise ValueError("Ledger quantity/source mismatch")
            if kind == "receipt":
                if delta <= 0 or doc["kind"] != "receipt" or doc["warehouse_id"] != key[0]:
                    raise ValueError("Invalid receipt ledger")
                cost = money(Decimal(line["unit_price"]) * delta)
                inflow += cost
            elif kind in ("issue", "transfer_send"):
                if delta >= 0 or doc["warehouse_id"] != key[0] or quantities[key] < -delta:
                    raise ValueError("Negative quantity in ledger ID replay")
                if (kind == "issue" and doc["kind"] != "issue") or (kind == "transfer_send" and doc["kind"] != "transfer"):
                    raise ValueError("Wrong document kind for ledger event")
                allocated = allocated_cost(values[key], quantities[key], -delta)
                line_costs[line["id"]] = allocated
                if kind == "transfer_send":
                    transit[line["id"]] = {"quantity": -delta, "value": allocated,
                        "warehouse_id": doc["target_warehouse_id"], "part_id": line["part_id"]}
                else:
                    issue_cost += allocated
                cost = -allocated
            elif kind == "transfer_receive":
                sending = transit.pop(line["id"], None)
                if sending is None or delta != sending["quantity"] or key != (sending["warehouse_id"], sending["part_id"]):
                    raise ValueError("Transfer receipt lacks matching fixed sending cost")
                cost = sending["value"]
            else:
                raise ValueError(f"Unsupported ledger event {kind}")
        if event["value_delta"] is not None and money(event["value_delta"]) != cost:
            raise ValueError("Existing ledger value disagrees with replay; manual changes are not overwritten")
        change("stock_ledger", event, {"value_delta": cost})
        quantities[key] += delta
        values[key] = money(values[key] + cost)
        if quantities[key] < 0 or values[key] < 0 or (quantities[key] == 0 and values[key] != 0):
            raise ValueError("Invalid quantity/value in ledger ID replay")
    for line_id, cost in line_costs.items():
        line = lines[line_id]
        if line["cost_amount"] is not None and money(line["cost_amount"]) != cost:
            raise ValueError("Existing line cost differs from replay")
        update = {"cost_amount": cost}
        if line["cost_amount"] is None and not line["pricing_source"]:
            update["pricing_source"] = SOURCE
        change("document_lines", line, update)
    balance_keys = set()
    for balance in before["stock_balances"]:
        key = (balance["warehouse_id"], balance["part_id"])
        balance_keys.add(key)
        if quantities[key] != balance["quantity"]:
            raise ValueError(f'Balance quantity mismatch for warehouse/part {key}')
        if balance["stock_value"] is not None and money(balance["stock_value"]) != values[key]:
            raise ValueError("Existing stock value differs from replay")
        change("stock_balances", balance, {"stock_value": values[key]})
    if any(quantity != 0 and key not in balance_keys for key, quantity in quantities.items()):
        raise ValueError("Replay quantity has no current balance row")
    for row in before["agent_proposals"]:
        if row["status"] == "pending":
            change("agent_proposals", row, {"status": "superseded"})
    stock_value = sum(values.values(), Decimal("0.00"))
    transit_value = sum((row["value"] for row in transit.values()), Decimal("0.00"))
    if stock_value + transit_value + issue_cost != inflow:
        raise ValueError("Value conservation failed")
    summary = {"source": SOURCE, "balances": len(before["stock_balances"]),
        "opening_lines": len(openings), "ledger_events": len(before["stock_ledger"]),
        "receipt_lines": sum(documents[row["document_id"]]["kind"] == "receipt" for row in lines.values()),
        "issue_lines": sum(documents[row["document_id"]]["kind"] == "issue" and documents[row["document_id"]]["status"] == "posted" for row in lines.values()),
        "quantity": sum(row["quantity"] for row in before["stock_balances"]),
        "in_transit_quantity": sum(row["quantity"] for row in transit.values()),
        "stock_value": str(money(stock_value)), "in_transit_value": str(money(transit_value)),
        "inflow_value": str(money(inflow)), "issue_cost": str(money(issue_cost)),
        "paid_documents": issue_types["paid_repair"], "warranty_documents": issue_types["warranty"],
        "warranty_document_percent": str(money(Decimal(issue_types["warranty"])*100/sum(issue_types.values()))) if issue_types else None,
        "superseded_proposals": sum(row["status"] == "pending" for row in before["agent_proposals"]),
        "changes": len(changes), "new_quotes": len(inserts),
        "notice": "项目模拟历史补录；旧unit_price=0不表示免费；毛利不含经营费用"}
    plan = {"version": VERSION, "seed": SEED, "source": SOURCE, "baseline": before,
        "baseline_digest": digest(before), "quantity_fingerprint": quantity_fingerprint(before),
        "changes": changes, "inserts": inserts, "summary": summary,
        "in_transit": [{"line_id": line_id, **{key: json_value(value) for key, value in row.items()}}
            for line_id, row in sorted(transit.items())]}
    plan["digest"] = digest(plan)
    return plan


def apply_plan(session, plan, operator_id):
    """Apply within caller transaction; replays return the saved result without touching later prices."""
    if plan.get("version") != VERSION or plan.get("digest") != digest({key: value for key, value in plan.items() if key != "digest"}):
        raise ValueError("Invalid pricing plan version/digest")
    operator = session.get(User, operator_id)
    if operator is None or operator.role != "admin" or not operator.active:
        raise ValueError("Active administrator required")
    # Match stock business lock order and cover disabled catalog records too.
    for model in (Warehouse, Supplier, Part):
        list(session.scalars(select(model).order_by(*model.__table__.primary_key).with_for_update().execution_options(populate_existing=True)))
    old = session.get(PricingBackfill, VERSION, populate_existing=True)
    if old:
        if old.plan_digest != plan["digest"]:
            raise ValueError("Pricing version already applied with another digest")
        return old.summary
    for model in (Document, OpeningDocument, SupplierPrice, DocumentLine, DocumentReview, OpeningLine, StockBalance, StockLedger, AgentProposal):
        list(session.scalars(select(model).order_by(*model.__table__.primary_key).with_for_update().execution_options(populate_existing=True)))
    if digest(snapshot(session)) != plan["baseline_digest"]:
        raise ValueError("Pricing baseline changed; regenerate and review a new plan")
    if make_plan(session)["digest"] != plan["digest"]:
        raise ValueError("Plan digest does not match deterministic replay")
    # Savepoint keeps callers from accidentally committing a partially applied plan on failure.
    with session.begin_nested():
        for insertion in plan["inserts"]:
            data = insertion["values"]
            session.add(SupplierPrice(**data))
            session.add(PriceChange(part_id=data["part_id"], supplier_id=data["supplier_id"],
                kind="supply", before_price=None, after_price=Decimal(data["supply_price"]),
                source=SOURCE, operator_id=operator_id))
        for item in plan["changes"]:
            row = session.get(MODEL_BY_TABLE[item["table"]], item["id"])
            for key, value in item["after"].items():
                column = row.__table__.columns[key]
                if value is not None and column.type.python_type is Decimal:
                    value = Decimal(value)
                setattr(row, key, value)
            if item["table"] == "parts" and "sale_price" in item["after"]:
                session.add(PriceChange(part_id=row.id, kind="sale", before_price=None,
                    after_price=row.sale_price, source=SOURCE, operator_id=operator_id))
        session.add(PricingBackfill(version=VERSION, plan_digest=plan["digest"],
            quantity_fingerprint=plan["quantity_fingerprint"], summary=plan["summary"]))
        session.add(AuditLog(operator_id=operator_id, action="pricing_backfill", entity="pricing_backfills", entity_id=0))
        session.flush()
    return plan["summary"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path)
    mode.add_argument("--apply", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("Restricted to fours_agent_dev / fours_dev")
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            if args.plan:
                plan = make_plan(session)
                args.plan.parent.mkdir(parents=True, exist_ok=True)
                with args.plan.open("x", encoding="utf-8") as file:
                    json.dump(plan, file, ensure_ascii=False, indent=2)
                result = plan["summary"]
            else:
                plan = json.loads(args.apply.read_text(encoding="utf-8"))
                if plan.get("version") != VERSION or plan.get("digest") != digest({key: value for key, value in plan.items() if key != "digest"}):
                    raise ValueError("Invalid pricing plan version/digest")
                operator = session.scalar(select(User).where(User.username == "demo_admin", User.role == "admin", User.active == True))
                if operator is None:
                    raise ValueError("Expected active project administrator")
                backup = ROOT/"data/local-backups"/f"{VERSION}-{plan['digest'][:12]}.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    with backup.open("x", encoding="utf-8") as file:
                        json.dump({"version": plan["version"], "digest": plan["digest"],
                            "baseline": plan["baseline"]}, file, ensure_ascii=False, indent=2)
                result = apply_plan(session, plan, operator.id)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
