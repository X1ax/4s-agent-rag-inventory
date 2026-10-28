"""Real Embedding retrieval on development inputs only; never generates answers."""
import json
import time
from datetime import datetime, timezone

from backend.app.ai import EmbeddingClient
from backend.app.config import get_settings
from backend.app.knowledge import ROOT, KnowledgeError, digest
from backend.app.retrieval import open_index


def main():
    settings = get_settings()
    embedder = EmbeddingClient(settings)
    report = {"run_type": "real_online_embedding_dense_development_baseline", "acceptance_run": False,
        "chat_model_called": False, "answer_quality_evaluated": False, "top_k": 5, "cases": [], "input_files": {}}
    try:
        with open_index(settings, embedder=embedder) as index:
            report.update(generation_id=index.report["generation_id"], fingerprint=index.report["fingerprint"],
                embedding=index.report["embedding"], created_at=datetime.now(timezone.utc).isoformat())
            report["default_scope_check"] = index.search("库存制度现在可以执行吗？", "inventory_policies")
            if report["default_scope_check"]["status"] != "no_eligible_documents" or embedder.requests:
                raise KnowledgeError("unexpected_release_state", "当前开发基线预期全部资料待核准，请先核查范围")
            for suite, kb in (("inventory", "inventory_policies"), ("diagnostics", "general_diagnostics")):
                path = ROOT / f"evals/{suite}/development.json"
                raw = path.read_bytes()
                report["input_files"][path.relative_to(ROOT).as_posix()] = digest(raw)
                for case in json.loads(raw)["cases"]:
                    groups = ([{r["clause_id"] for r in group["any_of"]} for group in case["evidence_groups"]]
                        if suite == "inventory" else [{s} for s in case["expected_sections"] if not s.startswith("DX-RULE-")])
                    record = {"case_id": case["case_id"], "knowledge_base": kb, "question": case["input"],
                        "scope": "draft_preview", "expected_groups": [sorted(group) for group in groups]}
                    report["cases"].append(record)
                    available = {u["unit_id"] for u in index.units.values() if u["knowledge_base"] == kb}
                    if not all(group & available for group in groups):
                        record["status"] = "not_run_evidence_not_indexed"
                        continue
                    started = time.monotonic()
                    found = index.search(case["input"], kb, scope="draft_preview")
                    hit_ids = {u["unit_id"] for u in found["hits"]}
                    expanded = {unit for context in found["contexts"] for unit in context["unit_ids"]}
                    record.update(status=found["status"], elapsed_seconds=round(time.monotonic()-started, 3),
                        hits=[{k: u[k] for k in ("unit_id", "chunk_id", "document_id", "document_version", "source_sha256",
                            "section_path", "distance", "page_start", "page_end", "block_start", "block_end") if k in u} for u in found["hits"]],
                        expanded_contexts=[{k: c[k] for k in ("context_id", "document_id", "unit_ids")} for c in found["contexts"]],
                        raw_groups_hit=sum(bool(g & hit_ids) for g in groups),
                        expanded_groups_hit=sum(bool(g & expanded) for g in groups), expected_group_count=len(groups))
                    print(json.dumps({"case_id": case["case_id"], "hits": sorted(hit_ids),
                        "raw_groups_hit": record["raw_groups_hit"], "expected_groups": len(groups)}, ensure_ascii=False), flush=True)
            report["usage"] = embedder.usage()
        report["summary"] = {}
        for kb in ("inventory_policies", "general_diagnostics"):
            cases = [r for r in report["cases"] if r["knowledge_base"] == kb and r["status"] == "retrieved"]
            report["summary"][kb] = {"queries_run": len(cases), "not_run": sum(r["knowledge_base"] == kb and r["status"].startswith("not_run") for r in report["cases"]),
                "raw_evidence_groups_hit": sum(r["raw_groups_hit"] for r in cases), "expected_groups": sum(r["expected_group_count"] for r in cases),
                "raw_complete_queries": sum(r["raw_groups_hit"] == r["expected_group_count"] for r in cases),
                "expanded_complete_queries": sum(r["expanded_groups_hit"] == r["expected_group_count"] for r in cases)}
        directory = ROOT / "evals/runs"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / ("dense-development-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"output": output.relative_to(ROOT).as_posix(), "summary": report["summary"], "usage": report["usage"]}, ensure_ascii=False, indent=2))
        return 0
    except KnowledgeError as error:
        print(json.dumps({"status": "failed", "error_code": error.code, "detail": error.detail}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
