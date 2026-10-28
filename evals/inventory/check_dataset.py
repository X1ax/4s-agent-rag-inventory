"""Validate fixed inventory QA inputs and source locks; never call a model or DB."""
import hashlib
import json
from collections import Counter
from datetime import date, timedelta
from fractions import Fraction
from math import ceil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent


def read_json(path):
    text = path.read_text(encoding="utf-8")
    assert "\ufffd" not in text, path
    return json.loads(text)


def check_calculation(calc):
    values, expected = calc["inputs"], calc["expected"]
    if calc["kind"] == "replenishment":
        assert values["days"] == 30
        daily = Fraction(values["issue_30d"], values["days"])
        target = ceil(daily * values["lead_days"] + values["safety"])
        position = sum(values[k] for k in ("available", "purchase_pending", "transfer_inbound"))
        assert Fraction(expected["daily_consumption"]) == daily
        assert (expected["target"], expected["position"], expected["gap"]) == (
            target, position, max(0, target - position))
    elif calc["kind"] == "transfer_conservation":
        source, target, quantity = (values[k] for k in ("source", "target", "quantity"))
        assert 0 < quantity <= source
        assert expected == {
            "after_send": {"source": source - quantity, "target": target, "in_transit": quantity},
            "after_receive": {"source": source - quantity, "target": target + quantity, "in_transit": 0},
            "total": source + target}
    elif calc["kind"] == "complete_day_window":
        baseline = date.fromisoformat(values["baseline"])
        assert expected == {"start": str(baseline - timedelta(days=values["days"])),
                            "end": str(baseline - timedelta(days=1)), "count": values["days"]}
    elif calc["kind"] == "allocation":
        remaining = {r["code"]: max(0, r["available"] - r["target"]) for r in values["sources"]}
        order = sorted(remaining, key=lambda code: (-remaining[code], code))
        allocations, gaps = [], {}
        for demand in values["demands"]:
            gap = demand["gap"]
            for code in order:
                quantity = min(gap, remaining[code])
                if quantity:
                    allocations.append({"source": code, "target": demand["code"], "quantity": quantity})
                    remaining[code] -= quantity
                    gap -= quantity
            gaps[demand["code"]] = gap
        assert expected == {"allocations": allocations, "purchase_gaps": gaps}
    else:
        raise AssertionError(f"Unknown calculation: {calc['kind']}")


def main():
    lock = read_json(BASE / "corpus-lock.json")
    manifest_path = ROOT / lock["source_manifest"]
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == lock["source_manifest_sha256"]
    manifest = read_json(manifest_path)
    docs = {d["document_id"]: d for d in manifest["documents"]}
    assert {d["document_id"] for d in lock["documents"]} == set(docs)
    for frozen in lock["documents"]:
        doc = docs[frozen["document_id"]]
        assert (ROOT / frozen["path"]).resolve() == (manifest_path.parent / doc["path"]).resolve()
        assert hashlib.sha256((ROOT / frozen["path"]).read_bytes()).hexdigest() == frozen["sha256"] == doc["sha256"]
        for field in ("native_format", "approval_status", "effective_from"):
            assert frozen[field] == doc[field]
        assert frozen["version"] == doc["version"]

    ids, questions, counts, tags, modes = set(), set(), {}, Counter(), Counter()
    covered_clauses, calculations, no_retrieval = set(), 0, 0
    for split, prefix in (("development", "DEV"), ("acceptance", "ACC")):
        suite = read_json(BASE / f"{split}.json")
        assert suite["split"] == split and suite["execution_status"] == "not_run"
        assert (ROOT / suite["corpus_lock"]).resolve() == BASE / "corpus-lock.json"
        coverage = set()
        for case in suite["cases"]:
            cid = case["case_id"]
            assert cid.startswith(prefix + "-INV-") and cid not in ids, cid
            assert case["input"] and case["input"] not in questions, cid
            assert case["expected_answer_points"] and case["forbidden_claims"], cid
            assert case["execution_status"] == "not_run" and case["origin"] == "project_authored_synthetic_case"
            assert case["answer_mode"] in {"grounded", "insufficient_data", "no_eligible_current_policy", "reject_unsafe_instruction"}
            ids.add(cid)
            questions.add(case["input"])
            tags.update(case["tags"])
            modes.update([case["answer_mode"]])
            groups = case["evidence_groups"]
            refs = [ref for group in groups for ref in group["any_of"]]
            assert all(group["any_of"] for group in groups), cid
            if case["knowledge_scope"] == "released_only":
                assert not groups and case["answer_mode"] == "no_eligible_current_policy", cid
                assert all(d["approval_status"] == "pending_approval" and not d["effective_from"] for d in docs.values())
                no_retrieval += 1
            else:
                assert case["knowledge_scope"] == "draft_preview" and groups, cid
            for ref in refs + case.get("rubric_only_refs", []):
                doc = docs[ref["document_id"]]
                assert ref["document_version"] == doc["version"], cid
                assert ref["clause_id"] in doc["clause_ids"], cid
                if doc["native_format"] == "pdf":
                    assert ref["page_number"] == doc["clause_pages"][ref["clause_id"]], cid
                else:
                    assert "page_number" not in ref, cid
            coverage.update(ref["document_id"] for ref in refs)
            covered_clauses.update(ref["clause_id"] for ref in refs)
            for fixture in case.get("context_injection", []):
                assert fixture["fixture_id"].startswith("FIXTURE-") and fixture["source_type"].startswith("synthetic_"), cid
            if "calculation" in case:
                check_calculation(case["calculation"])
                calculations += 1
        assert coverage == set(docs), (split, coverage)
        counts[split] = len(suite["cases"])
    assert {"prompt_injection", "exact_clause", "version_conflict", "missing_conditions", "realtime_boundary", "publication_filter"} <= set(tags)
    print(json.dumps({"result": "PASS", "cases": counts, "total": len(ids),
        "documents_covered_per_split": len(docs), "native_formats": sorted({d["native_format"] for d in docs.values()}),
        "unique_expected_clauses": len(covered_clauses), "calculations_checked": calculations,
        "retrieval_scored_cases": len(ids) - no_retrieval, "publication_filter_cases": no_retrieval,
        "answer_modes": dict(modes), "model_evaluations_run": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
