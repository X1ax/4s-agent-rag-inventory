"""Explicit package-label normalization; quantities and prices are never converted."""
import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.inventory import audit
from backend.app.models import Part, PartFitment, User


def normalize(session, operator_id):
    operator = session.get(User, operator_id)
    if operator is None or not operator.active or operator.role != "admin":
        raise ValueError("An active administrator is required")
    changes = []
    parts = list(session.scalars(select(Part).where(Part.unit != "件").order_by(Part.id).with_for_update()))
    for part in parts:
        if part.unit not in {"桶", "瓶", "套"}:
            raise ValueError("Unknown original unit; package meaning must be confirmed first")
        note = f"库存包装：整{part.unit}按1件计数，不拆零；具体规格待核准。"
        specification = "；".join(filter(None, [part.specification, note]))
        if len(specification) > 200:
            raise ValueError("Specification would exceed 200 characters; no truncation is allowed")
        changes.append({"id": part.id, "sku": part.sku, "name": part.name, "old_unit": part.unit,
            "old_specification": part.specification, "old_reviewed_by": part.reviewed_by,
            "old_reviewed_at": part.reviewed_at.isoformat() if part.reviewed_at else None,
            "new_unit": "件", "new_specification": specification})
    for part, change in zip(parts, changes):
        part.unit = "件"
        part.specification = change["new_specification"]
        part.reviewed_at = part.reviewed_by = None
        session.execute(update(PartFitment).where(PartFitment.part_id == part.id)
            .values(reviewed_at=None, reviewed_by=None))
        audit(session, operator_id, "part.unit.normalize", "parts", part.id)
    session.flush()
    return changes


def main():
    settings = Settings()
    if settings.mysql_database != "fours_agent_dev" or settings.mysql_user != "fours_dev":
        raise RuntimeError("Unit normalization is restricted to fours_agent_dev / fours_dev")
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == "demo_admin"))
            changes = normalize(session, operator.id if operator else None)
            backup = None
            if changes:
                folder = ROOT / "data/local-backups"
                folder.mkdir(parents=True, exist_ok=True)
                backup = folder / f"units-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
                backup.write_text(json.dumps({"operation": "package_unit_to_piece_v1", "changes": changes},
                    ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"changed_parts": len(changes), "unit": "件", "quantities_converted": False,
            "backup": str(backup) if backup else None}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
