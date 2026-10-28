import hashlib
import json
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.documents import request_digest
from backend.app.models import Document, OperationRequest, StockLedger
from backend.app.schemas import DocumentInput
from test_m1 import resources, system
from test_m2 import action, call, detail, quantities

pytestmark = pytest.mark.mysql


def update_catalog(system, path, body, *, role="admin", expected=200):
    response = system[0].put(path, headers=system[1][role], json=body)
    assert response.status_code == expected, response.text
    return response.json()


def setup_supply(system):
    warehouse, _ = resources(system, count=0)
    suppliers = [call(system, "/api/suppliers", {"code": f"{system[3]}-{uuid4().hex[:6].upper()}",
        "name": name, "supply_categories": categories}, role="admin", expected=201)
        for name, categories in [("测试油液商", ["油液"]), ("测试制动件商", ["制动系统"]), ("测试备选商", ["油液", "制动系统"])]]
    parts = [call(system, "/api/parts", {"sku": f"{system[3]}-{uuid4().hex[:6].upper()}",
        "name": name, "category": category, "unit": unit}, role="admin", expected=201)
        for name, category, unit in [("测试整桶机油", "油液", "件"), ("测试制动盘", "制动系统", "件")]]
    body = {"request_id": str(uuid4()), "kind": "purchase", "warehouse_id": warehouse["id"],
        "business_date": str(date.today()), "lines": [
            {"part_id": parts[0]["id"], "supplier_id": suppliers[0]["id"], "quantity": 3, "unit_price": "12.30"},
            {"part_id": parts[1]["id"], "supplier_id": suppliers[1]["id"], "quantity": 4, "unit_price": "23.40"}]}
    for part, price in zip(parts, ('12.30', '23.40')):
        for supplier in suppliers:
            if part['category'] in supplier['supply_categories']:
                response = system[0].put(f"/api/pricing/suppliers/{supplier['id']}/parts/{part['id']}",
                    headers=system[1]['admin'], json={'supply_price': price, 'source': '测试供应商报价',
                        'active': True, 'expected_version': 0})
                assert response.status_code == 200, response.text
    return warehouse, suppliers, parts, body


def test_per_line_supplier_persists_through_partial_receipts_and_replay(system):
    warehouse, suppliers, parts, body = setup_supply(system)
    order = call(system, "/api/documents", body, expected=201)
    assert call(system, "/api/documents", body, expected=201)["replayed"]
    changed = {**body, "lines": [{**body["lines"][0], "supplier_id": suppliers[2]["id"]}, body["lines"][1]]}
    call(system, "/api/documents", changed, expected=409)
    result = detail(system, order["id"])
    assert result["supplier_id"] is None and result["total"] == "130.50"
    assert [line["supplier_id"] for line in result["lines"]] == [s["id"] for s in suppliers[:2]]
    assert [line["supplier"] for line in result["lines"]] == [s["name"] for s in suppliers[:2]]
    action(system, order["id"], "confirm")
    first = {"request_id": str(uuid4()), "lines": [{"purchase_line_id": result["lines"][0]["id"], "quantity": 1}]}
    receipt = call(system, f"/api/documents/{order['id']}/receipts", first, expected=201)
    assert call(system, f"/api/documents/{order['id']}/receipts", first, expected=201)["replayed"]
    received = detail(system, receipt["id"])
    assert received["supplier_id"] == suppliers[0]["id"] and received["lines"][0]["supplier_id"] == suppliers[0]["id"]
    assert detail(system, order["id"])["status"] == "partial"
    second = {"request_id": str(uuid4()), "lines": [
        {"purchase_line_id": result["lines"][0]["id"], "quantity": 2},
        {"purchase_line_id": result["lines"][1]["id"], "quantity": 4}]}
    received = detail(system, call(system, f"/api/documents/{order['id']}/receipts", second, expected=201)["id"])
    assert received["supplier_id"] is None
    assert [line["supplier_id"] for line in received["lines"]] == [s["id"] for s in suppliers[:2]]
    assert detail(system, order["id"])["status"] == "completed"
    balances = quantities(system, warehouse)
    assert [balances[p["id"]]["quantity"] for p in parts] == [3, 4]


def test_wrong_scope_missing_supplier_and_conflicting_header_do_not_create_documents(system):
    warehouse, suppliers, _, body = setup_supply(system)
    invalid = {**body, "lines": [body["lines"][0], {**body["lines"][1], "supplier_id": suppliers[0]["id"]}]}
    call(system, "/api/documents", invalid, expected=409)
    call(system, "/api/documents", {**body, "supplier_id": suppliers[0]["id"]}, expected=422)
    call(system, "/api/documents", {**body, "lines": [{**body["lines"][0], "supplier_id": None}]}, expected=422)
    call(system, "/api/documents", {**body, "lines": [{**body["lines"][0], "supplier_id": True}]}, expected=422)
    with Session(system[2]) as session:
        assert session.get(OperationRequest, body["request_id"]) is None
        assert session.scalar(select(func.count()).select_from(Document).where(Document.warehouse_id == warehouse["id"])) == 0
        assert session.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.warehouse_id == warehouse["id"])) == 0


def test_confirmation_rechecks_coverage_but_existing_confirmed_receipts_keep_provenance(system):
    _, suppliers, _, body = setup_supply(system)
    order = call(system, "/api/documents", body, expected=201)
    supplier = suppliers[0]
    update = {k: v for k, v in supplier.items() if k != "id"}
    update_catalog(system, f"/api/suppliers/{supplier['id']}", {**update, "supply_categories": []})
    action(system, order["id"], "confirm", expected=409)
    assert detail(system, order["id"])["status"] == "draft"
    update_catalog(system, f"/api/suppliers/{supplier['id']}", update)
    action(system, order["id"], "confirm")
    update_catalog(system, f"/api/suppliers/{supplier['id']}", {**update, "supply_categories": []})
    line = detail(system, order["id"])["lines"][0]
    receipt = call(system, f"/api/documents/{order['id']}/receipts", {"request_id": str(uuid4()),
        "lines": [{"purchase_line_id": line["id"], "quantity": 1}]}, expected=201)
    assert detail(system, receipt["id"])["lines"][0]["supplier_id"] == supplier["id"]


def test_default_supplier_and_coverage_updates_cannot_disagree(system):
    _, suppliers, parts, _ = setup_supply(system)
    part = parts[1]
    update = {k: v for k, v in part.items() if k not in {"id", "reviewed_at", "reviewed_by", "sale_price", "sale_price_version"}}
    update_catalog(system, f"/api/parts/{part['id']}", {**update, "default_supplier_id": suppliers[0]["id"]}, expected=409)
    update_catalog(system, f"/api/parts/{part['id']}", {**update, "default_supplier_id": suppliers[1]["id"]})
    supplier_update = {k: v for k, v in suppliers[1].items() if k != "id"}
    update_catalog(system, f"/api/suppliers/{suppliers[1]['id']}", {**supplier_update, "supply_categories": []}, expected=409)


def test_legacy_request_hash_is_unchanged_when_per_line_supplier_is_omitted():
    values = {"kind": "purchase", "warehouse_id": 1, "target_warehouse_id": None, "supplier_id": 2,
        "business_date": "2026-10-02", "note": "历史请求", "lines": [{"part_id": 3, "quantity": 2, "unit_price": "12.30"}]}
    expected = hashlib.sha256(json.dumps({"action": "purchase.create", "payload": values},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    payload = DocumentInput(request_id=uuid4(), **values)
    assert request_digest(payload, "purchase.create") == expected
