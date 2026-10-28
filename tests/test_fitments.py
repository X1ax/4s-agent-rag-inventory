from uuid import uuid4
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import AuditLog, PartFitment, VehicleModel
from tests.test_m1 import resources, system

pytestmark = pytest.mark.mysql


def inputs(system):
    client, tokens, _, prefix, _ = system
    _, parts = resources(system, 1)
    part = parts[0]
    part_body = {key: value for key, value in part.items() if key not in {"id", "reviewed_by", "reviewed_at", "sale_price", "sale_price_version"}}
    part_body.update(aliases="测试别名-" + uuid4().hex[:8], manufacturer="测试制造商", manufacturer_part_number="TEST/PN-001.A",
        specification="测试规格，非真实商品", source_reference="隔离测试来源 PART 页1", source_version="test-v1")
    response = client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body)
    assert response.status_code == 200, response.text
    vehicle_body = {"code": prefix + "-V-" + uuid4().hex[:6].upper(), "brand": "隔离测试品牌", "model_name": "测试车型，非真实适配",
        "model_year": 2024, "powertrain_type": "测试汽油动力", "engine_code": "TEST-ENGINE", "transmission": "测试变速器",
        "market": "测试市场", "configuration": "测试配置A", "source_reference": "隔离测试来源 VEH 第2章", "source_version": "test-v1"}
    response = client.post("/api/vehicle-models", headers=tokens["admin"], json=vehicle_body)
    assert response.status_code == 201, response.text
    vehicle = response.json()
    fitment_body = {"vehicle_id": vehicle["id"], "part_id": part["id"], "conditions": "仅测试配置A，禁止真实维修引用",
        "restrictions": "不得用于测试配置B", "required_quantity": 2, "source_reference": "隔离测试适配矩阵表1", "source_version": "test-v1"}
    return part, part_body, vehicle, vehicle_body, fitment_body


def test_traceable_identity_search_and_permissions(system):
    client, tokens, _, _, _ = system
    part, part_body, vehicle, _, body = inputs(system)
    for q in (part_body["aliases"], "TEST/PN-001.A"):
        response = client.get("/api/parts", headers=tokens["manager"], params={"q": q})
        assert response.status_code == 200 and part["id"] in [item["id"] for item in response.json()["items"]]
    assert client.get("/api/vehicle-models", headers=tokens["manager"], params={"q": vehicle["code"]}).json()["total"] == 1
    assert client.get("/api/fitments").status_code == 401
    for role in ("manager", "storekeeper"):
        assert client.post("/api/fitments", headers=tokens[role], json=body).status_code == 403
        assert client.post(f"/api/catalog/parts/{part['id']}/review", headers=tokens[role]).status_code == 403
    assert client.post("/api/fitments", headers=tokens["admin"], json=body | {"reviewed_by": 1}).status_code == 422
    for value in (0, -1, 1.5, True):
        assert client.post("/api/fitments", headers=tokens["admin"], json=body | {"required_quantity": value}).status_code == 422
    assert client.post("/api/fitments", headers=tokens["admin"], json=body | {"part_id": 2_000_000_000}).status_code == 404


def test_evidence_review_invalidation_and_unit_semantics(system):
    client, tokens, engine, _, _ = system
    part, part_body, vehicle, vehicle_body, body = inputs(system)
    fitment = client.post("/api/fitments", headers=tokens["admin"], json=body).json()
    review_path = f"/api/catalog/fitments/{fitment['id']}/review"
    assert client.post(review_path, headers=tokens["admin"]).status_code == 409
    for kind, row in (("parts", part), ("vehicle-models", vehicle)):
        assert client.post(f"/api/catalog/{kind}/{row['id']}/review", headers=tokens["admin"]).status_code == 200
    response = client.post(review_path, headers=tokens["admin"])
    assert response.status_code == 200 and response.json()["reviewed_by"] is not None
    reviewed_at = response.json()["reviewed_at"]
    assert client.post(review_path, headers=tokens["admin"]).json()["reviewed_at"] == reviewed_at
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity == "part_fitments",
            AuditLog.entity_id == fitment["id"], AuditLog.action == "catalog.review")) == 1
    query = {"vehicle_id": vehicle["id"], "part_id": part["id"], "reviewed": True}
    rows = client.get("/api/fitments", headers=tokens["manager"], params=query).json()
    assert rows["total"] == 1 and rows["items"][0]["unit"] == part["unit"] and rows["items"][0]["required_quantity"] == 2
    assert client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"unit": "套"}).status_code == 422
    # A reference-price edit does not change the evidence supporting identity or fitment.
    client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"reference_price": "13.00"})
    assert client.get("/api/fitments", headers=tokens["manager"], params=query).json()["total"] == 1
    client.put(f"/api/vehicle-models/{vehicle['id']}", headers=tokens["admin"], json=vehicle_body | {"source_version": "test-v2"})
    assert client.get("/api/fitments", headers=tokens["manager"], params=query).json()["total"] == 0
    with Session(engine) as session:
        assert session.get(PartFitment, fitment["id"]).reviewed_at is None
    assert client.post(review_path, headers=tokens["admin"]).status_code == 409
    client.post(f"/api/catalog/vehicle-models/{vehicle['id']}/review", headers=tokens["admin"])
    client.post(review_path, headers=tokens["admin"])
    client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"source_version": "test-v2"})
    assert client.get("/api/fitments", headers=tokens["manager"], params=query).json()["total"] == 0


def test_incomplete_sources_and_changed_fitment_require_new_review(system):
    client, tokens, _, _, _ = system
    part, part_body, vehicle, _, body = inputs(system)
    client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"manufacturer_part_number": ""})
    assert client.post(f"/api/catalog/parts/{part['id']}/review", headers=tokens["admin"]).status_code == 409
    client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body)
    for kind, row in (("parts", part), ("vehicle-models", vehicle)):
        client.post(f"/api/catalog/{kind}/{row['id']}/review", headers=tokens["admin"])
    fitment = client.post("/api/fitments", headers=tokens["admin"], json=body | {"required_quantity": None}).json()
    path = f"/api/catalog/fitments/{fitment['id']}/review"
    assert client.post(path, headers=tokens["admin"]).status_code == 409
    client.put(f"/api/fitments/{fitment['id']}", headers=tokens["admin"], json=body)
    assert client.post(path, headers=tokens["admin"]).status_code == 200
    result = client.put(f"/api/fitments/{fitment['id']}", headers=tokens["admin"], json=body | {"restrictions": "更新后的测试限制"})
    assert result.status_code == 200 and result.json()["reviewed_at"] is None
    client.post(path, headers=tokens["admin"])
    client.put(f"/api/parts/{part['id']}", headers=tokens["admin"], json=part_body | {"active": False})
    assert client.post(path, headers=tokens["admin"]).status_code == 409


def assert_downgrade_blocked(engine, monkeypatch, module_name, message):
    migration = import_module(module_name)
    with engine.connect() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, 'op', operations)

        def refuse_ddl(*args, **kwargs):
            pytest.fail('非空数据回退守卫未在DDL前拒绝操作')

        for method in ('drop_table', 'drop_column', 'drop_constraint'):
            monkeypatch.setattr(operations, method, refuse_ddl)
        with pytest.raises(RuntimeError, match=message):
            migration.downgrade()


def test_catalog_migration_refuses_to_drop_existing_evidence(system, monkeypatch):
    _, _, engine, _, _ = system
    _, _, vehicle, _, _ = inputs(system)
    assert_downgrade_blocked(engine, monkeypatch, 'backend.migrations.versions.0004_catalog', '禁止通过迁移回退')
    with Session(engine) as session:
        assert session.get(VehicleModel, vehicle["id"]) is not None


def test_pricing_migration_refuses_to_drop_registered_sale_price(system, monkeypatch):
    client, tokens, engine, _, _ = system
    _, parts = resources(system, count=1)
    part_id = parts[0]['id']
    response = client.patch(f'/api/pricing/parts/{part_id}/sale-price', headers=tokens['manager'],
        json={'sale_price': '18.00', 'source': '测试迁移保护售价', 'expected_version': 0})
    assert response.status_code == 200, response.text
    assert_downgrade_blocked(engine, monkeypatch, 'backend.migrations.versions.0008_pricing_costs', '禁止回退')
    assert client.get('/api/pricing/parts', headers=tokens['manager'], params={'part_id': part_id}).json()['items'][0]['sale_price'] == '18.00'
