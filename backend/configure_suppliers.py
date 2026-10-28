"""Explicitly configure simulated supplier coverage; never rewrite purchase history."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.inventory import audit
from backend.app.models import Part, Supplier, User


def configure():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("Supplier configuration is restricted to fours_agent_dev / fours_dev")
    source = json.loads((ROOT / "data/catalog/supplier-coverage.json").read_text(encoding="utf-8"))
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == "demo_admin", User.active == True, User.role == "admin"))
            if operator is None:
                raise RuntimeError("An active demo administrator is required")
            suppliers = []
            changed = 0
            for item in source["suppliers"]:
                supplier = session.scalar(select(Supplier).where(Supplier.code == item["code"]).with_for_update())
                if supplier is None:
                    raise RuntimeError("Run the M1 catalog import first")
                if supplier.supply_categories != item["supply_categories"]:
                    supplier.supply_categories = item["supply_categories"]
                    audit(session, operator.id, "supplier.coverage.configure", "suppliers", supplier.id)
                    changed += 1
                if not supplier.image_key:
                    supplier.image_key = item["image_key"]
                suppliers.append(supplier)
            parts = list(session.scalars(select(Part).where((Part.sku.like("DEMO-%")) | (Part.sku.like("CAT-%"))).order_by(Part.id).with_for_update()))
            summary = []
            for part in parts:
                allowed = [s for s in suppliers if part.category in s.supply_categories]
                if not allowed:
                    raise RuntimeError("A registered part category has no configured supplier")
                default = allowed[0]
                if part.default_supplier_id != default.id:
                    part.default_supplier_id = default.id
                    audit(session, operator.id, "part.supplier.configure", "parts", part.id)
                    changed += 1
                summary.append({"sku": part.sku, "name": part.name, "category": part.category,
                    "default_supplier": default.name, "eligible_suppliers": [s.name for s in allowed]})
            print(json.dumps({"configured_parts": len(parts), "changes": changed, "parts": summary}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    configure()
