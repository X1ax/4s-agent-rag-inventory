from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.main import create_app
from backend.app.models import AgentProposal, AuditLog, Document, DocumentLine, DocumentReview, OpeningDocument, OpeningLine, OperationRequest, Part, PartFitment, PriceChange, StockBalance, StockLedger, Supplier, SupplierPrice, User, VehicleModel, Warehouse
from backend.app.security import passwords

pytestmark = pytest.mark.mysql


@pytest.fixture(scope="module")
def system():
    settings = Settings()
    if not settings.test_mysql_password.get_secret_value():
        pytest.skip("Project test database is not configured")
    assert settings.test_mysql_database == "fours_agent_test" and settings.test_mysql_user == "fours_test"
    settings = settings.model_copy(update={"mysql_user": settings.test_mysql_user, "mysql_password": settings.test_mysql_password,
        "mysql_database": settings.test_mysql_database, "jwt_secret": SecretStr(uuid4().hex + uuid4().hex)})
    engine = make_engine(settings)
    prefix = "T" + uuid4().hex[:10].upper()
    password = uuid4().hex
    hashed = passwords.hash(password)
    with Session(engine) as session, session.begin():
        for role in ("admin", "storekeeper", "manager"):
            session.add(User(username=f"{prefix.lower()}_{role}", display_name=f"测试{role}", role=role, password_hash=hashed))
    try:
        with TestClient(create_app(settings)) as client:
            tokens = {}
            for role in ("admin", "storekeeper", "manager"):
                response = client.post("/api/auth/login", json={"username": f"{prefix.lower()}_{role}", "password": password})
                assert response.status_code == 200
                tokens[role] = {"Authorization": f"Bearer {response.json()['access_token']}"}
            yield client, tokens, engine, prefix, settings
    finally:
        # Delete only rows belonging to this run, from the explicitly isolated test database.
        with Session(engine) as session, session.begin():
            user_ids = list(session.scalars(select(User.id).where(User.username.startswith(prefix.lower()))))
            session.execute(delete(AgentProposal).where(AgentProposal.user_id.in_(user_ids)))
            part_ids = list(session.scalars(select(Part.id).where(Part.sku.startswith(prefix))))
            session.execute(delete(PriceChange).where(PriceChange.part_id.in_(part_ids)))
            session.execute(delete(SupplierPrice).where(SupplierPrice.part_id.in_(part_ids)))
            vehicle_ids = list(session.scalars(select(VehicleModel.id).where(VehicleModel.code.startswith(prefix))))
            session.execute(delete(PartFitment).where((PartFitment.part_id.in_(part_ids)) | (PartFitment.vehicle_id.in_(vehicle_ids))))
            session.execute(delete(VehicleModel).where(VehicleModel.id.in_(vehicle_ids)))
            warehouse_ids = list(session.scalars(select(Warehouse.id).where(Warehouse.code.startswith(prefix))))
            document_ids = list(session.scalars(select(OpeningDocument.id).where(OpeningDocument.warehouse_id.in_(warehouse_ids))))
            line_ids = list(session.scalars(select(OpeningLine.id).where(OpeningLine.document_id.in_(document_ids))))
            session.execute(delete(StockLedger).where(StockLedger.warehouse_id.in_(warehouse_ids)))
            business_ids = list(session.scalars(select(Document.id).where(Document.warehouse_id.in_(warehouse_ids))))
            session.execute(delete(DocumentReview).where(DocumentReview.document_id.in_(business_ids)))
            session.execute(delete(OperationRequest).where(OperationRequest.document_id.in_(business_ids)))
            session.execute(delete(DocumentLine).where(DocumentLine.document_id.in_(business_ids), DocumentLine.purchase_line_id.is_not(None)))
            session.execute(delete(DocumentLine).where(DocumentLine.document_id.in_(business_ids)))
            session.execute(delete(Document).where(Document.id.in_(business_ids), Document.purchase_id.is_not(None)))
            session.execute(delete(Document).where(Document.id.in_(business_ids)))
            session.execute(delete(StockBalance).where(StockBalance.warehouse_id.in_(warehouse_ids)))
            session.execute(delete(OpeningLine).where(OpeningLine.id.in_(line_ids)))
            session.execute(delete(OpeningDocument).where(OpeningDocument.id.in_(document_ids)))
            session.execute(delete(AuditLog).where(AuditLog.operator_id.in_(user_ids)))
            session.execute(delete(Part).where(Part.id.in_(part_ids)))
            session.execute(delete(Warehouse).where(Warehouse.id.in_(warehouse_ids)))
            session.execute(delete(Supplier).where(Supplier.code.startswith(prefix)))
            session.execute(delete(User).where(User.id.in_(user_ids)))
        engine.dispose()


def resources(system, count=2):
    client, tokens, _, prefix, _ = system
    code = prefix + "-" + uuid4().hex[:6].upper()
    response = client.post("/api/warehouses", headers=tokens["admin"], json={"code": code, "name": "测试仓库"})
    assert response.status_code == 201, response.text
    warehouse = response.json()
    parts = []
    for i in range(count):
        response = client.post("/api/parts", headers=tokens["admin"], json={"sku": f"{code}-{i}", "name": "测试备件", "unit": "件", "reference_price": "12.30"})
        assert response.status_code == 201, response.text
        parts.append(response.json())
    return warehouse, parts


def opening_body(warehouse, parts):
    return {"request_id": str(uuid4()), "warehouse_id": warehouse["id"], "business_date": str(date.today()),
        "lines": [{"part_id": part["id"], "quantity": i + 3, "unit_cost": "12.30", "cost_source": "测试期初凭证"} for i, part in enumerate(parts)]}


def test_auth_permissions_validation_and_account_changes(system):
    client, tokens, engine, prefix, settings = system
    assert client.get("/api/parts").status_code == 401
    forged = jwt.encode({"sub": "1", "exp": 9999999999, "iat": 1}, "x" * 40, algorithm="HS256")
    assert client.get("/api/parts", headers={"Authorization": "Bearer " + forged}).status_code == 401
    expired = jwt.encode({"sub": "1", "exp": 1, "iat": 1}, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    assert client.get("/api/parts", headers={"Authorization": "Bearer " + expired}).status_code == 401
    for role in ("manager", "storekeeper"):
        assert client.post("/api/parts", headers=tokens[role], json={"sku": "BAD", "name": "备件", "unit": "件"}).status_code == 403
        assert client.get("/api/users", headers=tokens[role]).status_code == 403
    assert client.post("/api/inventory/openings", headers=tokens["manager"], json={}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "missing", "password": "wrong"}).status_code == 401
    invalid_password = "secret!"
    response = client.post("/api/users", headers=tokens["admin"], json={"username": prefix.lower() + "_bad", "display_name": "测试", "password": invalid_password, "role": "manager"})
    assert response.status_code == 422 and invalid_password not in response.text
    password = uuid4().hex
    response = client.post("/api/users", headers=tokens["admin"], json={"username": prefix.lower() + "_new", "display_name": "新库管", "password": password, "role": "storekeeper"})
    assert response.status_code == 201 and "password" not in response.text
    user = response.json()
    with Session(engine) as session:
        stored = session.get(User, user["id"]).password_hash
        assert stored != password and passwords.verify(password, stored)
    login = client.post("/api/auth/login", json={"username": user["username"], "password": password}).json()
    headers = {"Authorization": "Bearer " + login["access_token"]}
    assert client.patch(f"/api/users/{user['id']}", headers=tokens["admin"], json={"role": "manager", "active": True}).status_code == 200
    assert client.post("/api/inventory/openings", headers=headers, json={}).status_code == 403
    client.patch(f"/api/users/{user['id']}", headers=tokens["admin"], json={"role": "manager", "active": False})
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    me = client.get("/api/auth/me", headers=tokens["admin"]).json()
    assert client.patch(f"/api/users/{me['id']}", headers=tokens["admin"], json={"role": "manager", "active": True}).status_code == 409
    assert "password_hash" not in client.get("/api/users", headers=tokens["admin"]).text


def test_catalog_constraints_filters_and_soft_disable(system):
    client, tokens, _, prefix, _ = system
    warehouse, parts = resources(system)
    part = parts[0]
    body = {key: value for key, value in part.items() if key not in {"id", "reviewed_by", "reviewed_at", "sale_price", "sale_price_version"}}
    assert client.post("/api/parts", headers=tokens["admin"], json=body).status_code == 409
    for changes in ({"reference_price": "1.001"}, {"safety_stock": -1}, {"safety_stock": True}, {"default_supplier_id": 2147483647}):
        response = client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=body | changes)
        assert response.status_code in (409, 422)
    response = client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=body | {"active": False})
    assert response.status_code == 200 and not response.json()["active"]
    rows = client.get("/api/parts", headers=tokens["manager"], params={"q": part["sku"], "active": False, "page_size": 1}).json()
    assert rows["total"] == 1 and rows["items"][0]["id"] == part["id"]
    assert client.delete(f"/api/parts/{part['id']}", headers=tokens["admin"]).status_code == 405
    body_opening = opening_body(warehouse, [part])
    assert client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=body_opening).status_code == 409
    supplier = {"code": prefix + "-S", "name": "模拟供应商", "lead_time_days": 3}
    response = client.post("/api/suppliers", headers=tokens["admin"], json=supplier)
    assert response.status_code == 201
    assert client.post("/api/suppliers", headers=tokens["admin"], json=supplier).status_code == 409
    assert client.get("/api/suppliers", headers=tokens["manager"], params={"q": supplier["code"]}).json()["total"] == 1


def test_opening_idempotency_ledger_and_immutable_unit(system):
    client, tokens, engine, _, _ = system
    warehouse, parts = resources(system)
    body = opening_body(warehouse, parts)
    first = client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=body)
    assert first.status_code == 200 and not first.json()["replayed"]
    replay = client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=body | {"lines": list(reversed(body["lines"]))})
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"] and replay.json()["replayed"]
    assert client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=body | {"note": "不同参数"}).status_code == 409
    assert client.post("/api/inventory/openings", headers=tokens["admin"], json=body).status_code == 409
    assert client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=body | {"request_id": str(uuid4())}).status_code == 409
    with Session(engine) as session:
        for part, line in zip(parts, body["lines"]):
            balance = session.scalar(select(StockBalance.quantity).where(StockBalance.part_id == part["id"], StockBalance.warehouse_id == warehouse["id"]))
            ledger_sum = session.scalar(select(func.sum(StockLedger.quantity_delta)).where(StockLedger.part_id == part["id"], StockLedger.warehouse_id == warehouse["id"]))
            assert balance == ledger_sum == line["quantity"]
    part = parts[0]
    part_body = {key: value for key, value in part.items() if key not in {"id", "reviewed_by", "reviewed_at", "sale_price", "sale_price_version"}}
    assert client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"unit": "套"}).status_code == 422
    assert client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"active": False}).status_code == 200
    ledger = client.get("/api/inventory/ledger", headers=tokens["manager"], params={"warehouse_id": warehouse["id"]}).json()
    assert ledger["total"] == 2 and ledger["items"][0]["document_id"] == first.json()["id"]
    assert client.put("/api/inventory/balances", headers=tokens["admin"], json={"quantity": 100}).status_code == 405


def test_opening_failure_rolls_back_every_write_and_validates_quantity(system):
    client, tokens, engine, _, _ = system
    warehouse, parts = resources(system)
    assert client.post("/api/inventory/openings", headers=tokens["admin"], json=opening_body(warehouse, [parts[1]])).status_code == 200
    body = opening_body(warehouse, parts)
    assert client.post("/api/inventory/openings", headers=tokens["admin"], json=body).status_code == 409
    with Session(engine) as session:
        assert session.scalar(select(OpeningDocument.id).where(OpeningDocument.request_id == body["request_id"])) is None
        assert session.scalar(select(StockBalance.id).where(StockBalance.warehouse_id == warehouse["id"], StockBalance.part_id == parts[0]["id"])) is None
        assert session.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.warehouse_id == warehouse["id"])) == 1
    for qty in (0, -1, 1.5, True, "3"):
        invalid = body | {"lines": [{"part_id": parts[0]["id"], "quantity": qty}]}
        assert client.post("/api/inventory/openings", headers=tokens["admin"], json=invalid).status_code == 422
    assert client.post("/api/inventory/openings", headers=tokens["admin"], json=body | {"lines": [body["lines"][0]] * 2}).status_code == 422
    assert client.post("/api/inventory/openings", headers=tokens["admin"], json=body | {"business_date": str(date.today() + timedelta(days=1))}).status_code == 422


def test_concurrent_retries_and_competing_initializations(system):
    client, tokens, engine, _, _ = system
    warehouse, parts = resources(system, count=3)
    body = opening_body(warehouse, [parts[0]])
    def post(payload):
        return client.post("/api/inventory/openings", headers=tokens["storekeeper"], json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, [body, body]))
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["replayed"] for response in responses) == [False, True]
    body = opening_body(warehouse, [parts[1]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, [body, body | {"request_id": str(uuid4())}]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.warehouse_id == warehouse["id"])) == 2
    other_warehouse, _ = resources(system, count=0)
    body = opening_body(warehouse, [parts[2]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, [body, body | {"warehouse_id": other_warehouse["id"]}]))
    assert sorted(response.status_code for response in responses) == [200, 409]
