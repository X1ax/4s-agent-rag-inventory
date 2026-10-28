"""Requalify current sources per domain; an old index never grants source authority."""
from backend.app.knowledge import CORPORA, KnowledgeError, digest, parse_corpora, source_scope_eligibility


def qualify_sources(indexed, root, knowledge_base, scope):
    documents = [d for d in indexed["documents"] if d["knowledge_base"] == knowledge_base]
    try:
        current = parse_corpora(root, knowledge_base=knowledge_base)
    except KnowledgeError as error:
        return {"allowed": {}, "unavailable": [{"document_id": d["document_id"], "reason": error.code} for d in documents],
            "signature": digest([knowledge_base, error.code]), "domain_error": error.code}
    relative = CORPORA[knowledge_base][0] + "/"

    def auxiliary(snapshot):
        document_paths = {d.get("path") for d in snapshot["documents"]}
        return sorted((i["path"], i["sha256"]) for i in snapshot["inputs"] if i["path"].startswith(relative)
            and i["path"] not in document_paths and i["path"] != relative + "manifest.json")

    auxiliary_changed = auxiliary(indexed) != auxiliary(current)
    active = {d["document_id"]: d for d in current["documents"]}
    allowed, unavailable = {}, []
    dynamic = {"index_status", "retrievable", "scope_eligibility"}
    for old in documents:
        doc_id = old["document_id"]
        row = active.get(doc_id)
        reason = None
        if auxiliary_changed:
            reason = "auxiliary_changed"
        elif row is None:
            reason = "source_removed"
        elif row["parse_status"] != "parsed":
            reason = row.get("error_code", "not_parsed")
        elif row["source_state"] in {"withdrawn", "superseded"}:
            reason = "source_not_current"
        elif {k: v for k, v in row.items() if k not in dynamic} != {k: v for k, v in old.items() if k not in dynamic}:
            reason = "source_changed_rebuild_required"
        else:
            eligible, why = source_scope_eligibility(row, scope)
            if eligible:
                allowed[doc_id] = row
            else:
                reason = why
        if reason:
            unavailable.append({"document_id": doc_id, "reason": reason})
    old_ids = {d["document_id"] for d in documents}
    unavailable.extend({"document_id": d, "reason": "not_indexed"} for d in sorted(active.keys() - old_ids))
    if knowledge_base == "general_diagnostics" and "DX-GEN-001" not in allowed:
        unavailable.extend({"document_id": d, "reason": "required_context_unavailable"} for d in sorted(allowed))
        allowed.clear()
    return {"allowed": allowed, "unavailable": unavailable,
        "signature": digest([current["inputs"], current["documents"]]),
        "domain_error": "auxiliary_changed" if auxiliary_changed else None}


def verify_qualification(before, indexed, root, knowledge_base, scope):
    after = qualify_sources(indexed, root, knowledge_base, scope)
    if before["signature"] != after["signature"] or before["allowed"] != after["allowed"]:
        raise KnowledgeError("knowledge_changed", "本次检索期间来源或资格发生变化，结果已丢弃，请重新查询")
