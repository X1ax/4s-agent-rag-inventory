from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_m1 import opening_body, resources, system
from backend.app.models import OperationRequest, Part, StockBalance, StockLedger, SupplierPrice

pytestmark = pytest.mark.mysql


def call(system, path, body, role="storekeeper", expected=200):
    client, tokens, *_ = system
    if path == "/api/suppliers":
        body = {"supply_categories": [""], **body}  # These test parts explicitly use the uncategorized bucket.
    response = client.post(path, headers=tokens[role], json=body)
    assert response.status_code == expected, response.text
    return response.json()


def action(system, document_id, verb, **extra):
    if verb in {'confirm','send'} and detail(system, document_id)['status'] == 'draft':
        approve_document(system, document_id)
    return call(system, f"/api/documents/{document_id}/{verb}", {"request_id": str(uuid4()), "business_date": str(date.today())}, **extra)


def approve_document(system, document_id):
    """Existing transaction tests explicitly complete the new review prerequisite."""
    if detail(system, document_id)['review']['status'] == 'approved':
        return
    call(system, f'/api/reviews/documents/{document_id}/submit', {})
    call(system, f'/api/reviews/documents/{document_id}/decision', {'approved': True}, role='manager')


def create(system, kind, warehouse, parts, **extra):
    body = {"request_id": str(uuid4()), "kind": kind, "warehouse_id": warehouse["id"], "business_date": str(date.today()),
        "lines": [{"part_id": part["id"], "quantity": 5, **({"unit_price": "12.30"} if kind == "purchase" else {})} for part in parts], **extra}
    # Explicit prerequisite data for legacy transaction tests; production has no defaults.
    if kind == 'issue':
        body.setdefault('issue_type', 'paid_repair')
    with Session(system[2]) as session, session.begin():
        for line in body['lines']:
            part = session.get(Part, line['part_id'])
            if kind == 'issue' and part.sale_price is None:
                part.sale_price, part.sale_price_version = Decimal('20.00'), 1
            if kind == 'purchase':
                supplier_id = line.get('supplier_id') or body.get('supplier_id')
                price = session.scalar(select(SupplierPrice).where(SupplierPrice.part_id==part.id, SupplierPrice.supplier_id==supplier_id))
                if price is None:
                    session.add(SupplierPrice(supplier_id=supplier_id,part_id=part.id,supply_price=Decimal(line.get('unit_price','12.30')),version=1,active=True,source='测试供应商报价'))
    return call(system, "/api/documents", body, expected=201)


def detail(system, document_id):
    return system[0].get(f"/api/documents/{document_id}", headers=system[1]["manager"]).json()


def quantities(system, warehouse):
    return {row["part_id"]: row for row in system[0].get("/api/inventory/balances", headers=system[1]["manager"], params={"warehouse_id": warehouse["id"] ,"page_size":100}).json()["items"]}


def test_purchase_partial_receipts_retry_cancel_and_amounts(system):
    warehouse, parts = resources(system)
    supplier = call(system, "/api/suppliers", {"code": system[3]+"-M2-S", "name": "模拟采购供应商"}, role="admin", expected=201)
    order = create(system, "purchase", warehouse, parts, supplier_id=supplier["id"])
    assert detail(system, order["id"])["total"] == "123.00"
    assert quantities(system,warehouse)[parts[0]["id"]]["quantity"] == 0
    action(system, order["id"], "confirm")
    assert quantities(system,warehouse)[parts[0]["id"]]["purchase_pending"] == 5
    line = detail(system,order["id"])["lines"][0]
    payload = {"request_id":str(uuid4()),"business_date":str(date.today()),"lines":[{"purchase_line_id":line["id"],"quantity":2}]}
    receipt = call(system,f"/api/documents/{order['id']}/receipts",payload,expected=201)
    assert call(system,f"/api/documents/{order['id']}/receipts",payload,expected=201)["replayed"]
    assert detail(system,order["id"])["status"] == "partial"
    assert quantities(system,warehouse)[parts[0]["id"]]["quantity"] == 2
    assert quantities(system,warehouse)[parts[0]["id"]]["purchase_pending"] == 3
    call(system,f"/api/documents/{order['id']}/receipts",payload|{"lines":[{"purchase_line_id":line["id"],"quantity":1}]},expected=409)
    call(system,f"/api/documents/{order['id']}/receipts",payload|{"request_id":str(uuid4()),"lines":[{"purchase_line_id":line["id"],"quantity":4}]},expected=409)
    action(system,order["id"],"cancel")
    row=quantities(system,warehouse)[parts[0]["id"]]
    assert row["quantity"] == 2 and row["purchase_pending"] == 0
    call(system,f"/api/documents/{order['id']}/receipts",payload|{"request_id":str(uuid4())},expected=409)
    assert detail(system,receipt["id"])["purchase_id"] == order["id"]


def test_purchase_completion_and_cross_order_line_rejection(system):
    warehouse, parts = resources(system,count=1)
    supplier=call(system,"/api/suppliers",{"code":system[3]+"-M2-S2","name":"模拟供应商"},role="admin",expected=201)
    first=create(system,"purchase",warehouse,parts,supplier_id=supplier["id"])
    second=create(system,"purchase",warehouse,parts,supplier_id=supplier["id"])
    action(system,first["id"],"confirm");action(system,second["id"],"confirm")
    line=detail(system,first["id"])["lines"][0]
    payload={"request_id":str(uuid4()),"lines":[{"purchase_line_id":line["id"],"quantity":5}]}
    call(system,f"/api/documents/{second['id']}/receipts",payload,expected=409)
    call(system,f"/api/documents/{first['id']}/receipts",payload,expected=201)
    assert detail(system,first["id"])["status"]=="completed"
    action(system,first["id"],"cancel",expected=409)
    assert quantities(system,warehouse)[parts[0]["id"]]["quantity"]==5


def test_issue_insufficient_rollback_and_concurrent_no_oversell(system):
    warehouse,parts=resources(system)
    call(system,"/api/inventory/openings",opening_body(warehouse,parts))
    issue=create(system,"issue",warehouse,parts,lines=[{"part_id":parts[0]["id"],"quantity":2},{"part_id":parts[1]["id"],"quantity":5}])
    body={"request_id":str(uuid4())}
    call(system,f"/api/documents/{issue['id']}/post",body,expected=409)
    assert detail(system,issue["id"])["status"]=="draft"
    with Session(system[2]) as s:
        assert s.get(OperationRequest,body["request_id"]) is None
        assert s.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.warehouse_id==warehouse["id"]))==2
    assert quantities(system,warehouse)[parts[0]["id"]]["quantity"]==3
    # Each needs 2 from a stock of 3: exactly one may commit.
    orders=[create(system,"issue",warehouse,[parts[0]],lines=[{"part_id":parts[0]["id"],"quantity":2}]) for _ in range(2)]
    def post(order):
        return system[0].post(f"/api/documents/{order['id']}/post",headers=system[1]["storekeeper"],json={"request_id":str(uuid4())})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(post,orders))
    assert sorted(result.status_code for result in results)==[200,409]
    assert quantities(system,warehouse)[parts[0]["id"]]["quantity"]==1


def test_transfer_in_transit_conservation_retry_and_states(system):
    source,parts=resources(system,count=1);target,_=resources(system,count=0)
    call(system,"/api/inventory/openings",opening_body(source,parts))
    order=create(system,"transfer",source,parts,target_warehouse_id=target["id"],lines=[{"part_id":parts[0]["id"],"quantity":2}])
    action(system,order["id"],"receive",expected=409)
    body={"request_id":str(uuid4()),"business_date":str(date.today())}
    approve_document(system, order['id'])
    call(system,f"/api/documents/{order['id']}/send",body)
    assert call(system,f"/api/documents/{order['id']}/send",body)["replayed"]
    a,b=quantities(system,source)[parts[0]["id"]],quantities(system,target)[parts[0]["id"]]
    assert a["quantity"]==1 and b["quantity"]==0 and b["in_transit"]==2
    assert a["quantity"]+b["quantity"]+b["in_transit"]==3
    action(system,order["id"],"send",expected=409);action(system,order["id"],"cancel",expected=409)
    received={"request_id":str(uuid4()),"business_date":str(date.today())}
    def receive(_):
        return system[0].post(f"/api/documents/{order['id']}/receive",headers=system[1]["storekeeper"],json=received)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(receive,range(2)))
    assert [r.status_code for r in results]==[200,200]
    assert sorted(r.json()["replayed"] for r in results)==[False,True]
    a,b=quantities(system,source)[parts[0]["id"]],quantities(system,target)[parts[0]["id"]]
    assert a["quantity"]+b["quantity"]==3 and b["in_transit"]==0 and b["quantity"]==2
    action(system,order["id"],"receive",expected=409)


def test_m2_permissions_idempotent_create_and_statistics(system):
    warehouse,parts=resources(system,count=1)
    response=system[0].patch(f"/api/pricing/parts/{parts[0]['id']}/sale-price",headers=system[1]['manager'],
        json={'sale_price':'20.00','source':'测试领用门店定价','expected_version':0})
    assert response.status_code==200,response.text
    payload={"request_id":str(uuid4()),"kind":"issue","issue_type":"paid_repair","warehouse_id":warehouse["id"],"business_date":str(date.today()),"lines":[{"part_id":parts[0]["id"],"quantity":1}]}
    call(system,"/api/documents",payload,role="manager",expected=403)
    first=call(system,"/api/documents",payload,expected=201)
    assert call(system,"/api/documents",payload,expected=201)["id"]==first["id"]
    call(system,"/api/documents",payload|{"note":"different"},expected=409,role="admin")
    action(system,first["id"],"post",role="manager",expected=403)
    action(system,first["id"],"cancel")
    action(system,first["id"],"post",expected=409)
    call(system,"/api/documents",payload|{"request_id":str(uuid4()),"kind":"transfer","issue_type":None,"target_warehouse_id":warehouse["id"]},expected=422)
    call(system,"/api/documents",payload|{"request_id":str(uuid4()),"lines":[{"part_id":parts[0]["id"],"quantity":True}]},expected=422)
    call(system,"/api/parts",{"sku":system[3]+"-BAD-IMG","name":"测试备件","unit":"件","image_key":"warehouse-main"},role="admin",expected=422)
    call(system,"/api/inventory/openings",opening_body(warehouse,parts))
    order=create(system,"issue",warehouse,parts,lines=[{"part_id":parts[0]["id"],"quantity":1}]);action(system,order["id"],"post")
    client,tokens,*_=system
    stats=client.get("/api/statistics",headers=tokens["manager"],params={"warehouse_id":warehouse["id"],"unit":"件"}).json()
    assert stats["totals"]["on_hand"]==2
    assert stats["consumption_ranking"][0]["quantity"]==1
    assert any(row["event"]=="issue" and row["quantity"]==-1 for row in stats["trend"])
    assert client.get("/api/statistics",headers=tokens["manager"],params={"date_from":"2026-10-22","date_to":"2026-10-01"}).status_code==422
    with Session(system[2]) as s:
        balances={(w,p):q for w,p,q in s.execute(select(StockBalance.warehouse_id,StockBalance.part_id,StockBalance.quantity))}
        ledger={(w,p):q for w,p,q in s.execute(select(StockLedger.warehouse_id,StockLedger.part_id,func.sum(StockLedger.quantity_delta)).group_by(StockLedger.warehouse_id,StockLedger.part_id))}
        assert balances==ledger


def test_statistics_filters_each_measure_by_part_and_uses_its_unit(system):
    client, tokens, _, prefix, _ = system
    warehouse, _ = resources(system, count=0)
    parts = [call(system, "/api/parts", {"sku": f"{prefix}-STAT-{i}", "name": f"统计备件{i}",
        "unit": unit, "safety_stock": 10}, role="admin", expected=201) for i, unit in enumerate(["件", "件", "件"])]
    call(system, "/api/inventory/openings", opening_body(warehouse, parts))
    supplier = call(system, "/api/suppliers", {"code": prefix+"-STAT", "name": "统计供应商"}, role="admin", expected=201)
    purchase = create(system, "purchase", warehouse, parts, supplier_id=supplier["id"])
    action(system, purchase["id"], "confirm")
    issue = create(system, "issue", warehouse, parts, lines=[{"part_id": p["id"], "quantity": 1} for p in parts])
    action(system, issue["id"], "post")
    for index in (0, 2):
        part = parts[index]
        response = client.get("/api/statistics", headers=tokens["manager"], params={
            "warehouse_id": warehouse["id"], "part_id": part["id"]})
        assert response.status_code == 200
        result = response.json()
        assert result["part_id"] == part["id"] and result["part_name"] == part["name"]
        assert result["unit"] == part["unit"]
        assert result["totals"] == {"on_hand": index+2, "purchase_pending": 5, "in_transit": 0}
        assert {row["part_id"] for row in result["low_stock"]} == {part["id"]}
        assert [(row["sku"], row["quantity"]) for row in result["consumption_ranking"]] == [(part["sku"], 1)]
        assert next(row["quantity"] for row in result["trend"] if row["event"] == "issue") == -1
        assert result["warehouses"] == [{"warehouse": warehouse["name"], "quantity": index+2}]
    assert client.get("/api/statistics", headers=tokens["manager"], params={"part_id": 0}).status_code == 422
    assert client.get("/api/statistics", headers=tokens["manager"], params={"part_id": 2147483647}).status_code == 404
