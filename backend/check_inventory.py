"""Read-only reconciliation for the project's current M2 ledger and document semantics."""
import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.models import Document, DocumentLine, OpeningDocument, OpeningLine, Part, StockBalance, StockLedger


def check(test=False):
    settings = Settings()
    database = settings.test_mysql_database if test else settings.mysql_database
    if database != ("fours_agent_test" if test else "fours_agent_dev"):
        raise RuntimeError("Reconciliation is restricted to the named project database")
    engine = make_engine(settings, test=test)
    try:
        with Session(engine) as session:
            balances = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity))}
            ledger = {(w, p): q for w, p, q in session.execute(select(StockLedger.warehouse_id, StockLedger.part_id,
                func.sum(StockLedger.quantity_delta)).group_by(StockLedger.warehouse_id, StockLedger.part_id))}
            assert balances == ledger and all(q >= 0 for q in balances.values()), "Balance/ledger mismatch or negative stock"
            for entry, line, document in session.execute(select(StockLedger, OpeningLine, OpeningDocument).join(OpeningLine,
                OpeningLine.id == StockLedger.opening_line_id).join(OpeningDocument, OpeningDocument.id == OpeningLine.document_id)):
                assert (entry.part_id, entry.warehouse_id, entry.quantity_delta, entry.event) == (line.part_id, document.warehouse_id, line.quantity, "opening")
            for entry, line, document in session.execute(select(StockLedger, DocumentLine, Document).join(DocumentLine,
                DocumentLine.id == StockLedger.document_line_id).join(Document, Document.id == DocumentLine.document_id)):
                assert entry.event in {"receipt", "issue", "transfer_send", "transfer_receive"}
                warehouse = document.target_warehouse_id if entry.event == "transfer_receive" else document.warehouse_id
                delta = -line.quantity if entry.event in {"issue", "transfer_send"} else line.quantity
                assert (entry.part_id, entry.warehouse_id, entry.quantity_delta) == (line.part_id, warehouse, delta), "Document/ledger source mismatch"
            on_hand = dict(session.execute(select(Part.unit, func.sum(StockBalance.quantity)).join(Part,
                Part.id == StockBalance.part_id).group_by(Part.unit)).all())
            transit = dict(session.execute(select(Part.unit, func.sum(DocumentLine.quantity)).join(DocumentLine,
                DocumentLine.part_id == Part.id).join(Document, Document.id == DocumentLine.document_id).where(
                Document.kind == "transfer", Document.status == "sent").group_by(Part.unit)).all())
            external = dict(session.execute(select(Part.unit, func.sum(StockLedger.quantity_delta)).join(StockLedger,
                StockLedger.part_id == Part.id).where(StockLedger.event.in_(["opening", "receipt", "issue"])).group_by(Part.unit)).all())
            for unit in set(on_hand) | set(transit) | set(external):
                assert on_hand.get(unit, 0) + transit.get(unit, 0) == external.get(unit, 0), f"Transfer conservation failed: {unit}"
            print(json.dumps({"database": database, "balance_rows": len(balances), "ledger_rows": session.scalar(select(func.count()).select_from(StockLedger)),
                "documents": session.scalar(select(func.count()).select_from(Document)), "balance_matches_ledger": True,
                "document_sources_match": True, "transfer_conservation": True, "on_hand_by_unit": on_hand,
                "in_transit_by_unit": transit}, ensure_ascii=False, default=str))
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    check(parser.parse_args().test)
