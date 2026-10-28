"""Add component types without creating stock events or asserting vehicle fitment."""
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.inventory import audit
from backend.app.models import Part, User
from backend.app.schemas import PartInput

ROOT = Path(__file__).resolve().parents[1]


def main():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise ValueError("Only the explicitly configured project development database is allowed")
    source = json.loads((ROOT / "data/catalog/parts-expansion.json").read_text(encoding="utf-8"))
    payloads = [PartInput.model_validate({key: value for key, value in row.items() if key != "image_prompt"}) for row in source["parts"]]
    for payload in payloads:
        if not (ROOT / "frontend/public/images" / f"{payload.image_key}.png").is_file():
            raise ValueError(f"Missing image for {payload.sku}")
    engine = make_engine(settings)
    created = skipped = sources_added = 0
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == "demo_admin", User.role == "admin", User.active))
            if operator is None:
                raise ValueError("The existing project administrator is required")
            for payload in payloads:
                existing = session.scalar(select(Part).where(Part.sku == payload.sku).with_for_update())
                if existing:
                    if existing.name != payload.name:
                        raise ValueError(f"SKU collision: {payload.sku}")
                    skipped += 1
                    continue
                row = Part(**payload.model_dump())
                session.add(row)
                session.flush()
                audit(session, operator.id, "catalog.create", "parts", row.id)
                created += 1
            for number in range(1, 13):
                row = session.scalar(select(Part).where(Part.sku == f"DEMO-{number:03}").with_for_update())
                if row is not None and not row.source_reference:
                    row.source_reference = "backend.seed_demo：项目模拟备件资料；不是厂家件号或车型适配依据"
                    row.source_version = "M1-seed-1"
                    audit(session, operator.id, "catalog.source", "parts", row.id)
                    sources_added += 1
        print(json.dumps({"created": created, "skipped_existing": skipped, "legacy_sources_added": sources_added,
            "stock_events_created": 0, "fitments_created": 0}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
