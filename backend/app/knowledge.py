"""Controlled source parsing; native format libraries are loaded only when needed."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
PARSER_VERSION = "controlled-text-v1"
NATIVE_PARSER_VERSION = "controlled-office-v1"
CHUNKING_VERSION = "clause-section-v1"
MAX_UNIT_CHARS = 4000
CORPORA = {
    "inventory_policies": ("data/knowledge/inventory", "policies"),
    "general_diagnostics": ("data/knowledge/diagnostics", "manuals"),
}
CLAUSE = re.compile(r"^(INV-[A-Z]{3}-\d{3}-C\d{3})(?:\s+(.*))?$")
DIAGNOSTIC_FIELDS = ("适用与追问", "故障类型", "检查路径", "换件判断", "依据与限制")


class KnowledgeError(ValueError):
    def __init__(self, code, detail, *, state="failed"):
        super().__init__(detail)
        self.code, self.detail, self.state = code, detail, state


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def controlled_path(base, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise KnowledgeError("invalid_path", "来源路径必须是语料目录内的相对路径")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise KnowledgeError("path_outside_corpus", "来源路径越出语料目录")
    return path


def read_bytes(path):
    try:
        return path.read_bytes()
    except OSError:
        raise KnowledgeError("source_unreadable", "来源文件不存在或无法读取") from None


def read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise KnowledgeError("duplicate_json_key", "清单存在重复字段")
            result[key] = value
        return result
    raw = read_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys)
    except (UnicodeError, json.JSONDecodeError):
        raise KnowledgeError("invalid_json", "来源清单不是有效的UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise KnowledgeError("invalid_manifest", "来源清单必须是JSON对象")
    return value, digest(raw)


def source_scope_eligibility(document, scope="released_only", *, as_of=None):
    """Source-scope eligibility only. This does NOT mean indexed or retrievable."""
    if scope not in {"draft_preview", "released_only"}:
        raise KnowledgeError("invalid_scope", "不支持的知识查询范围")
    if document["parse_status"] != "parsed":
        return False, "not_parsed"
    state = document["source_state"]
    if state == 'reference':
        return (True, 'admin_reviewed_local_reference') if scope == 'draft_preview' else (False, 'not_formally_released')
    if state in {"withdrawn", "superseded", "unknown"}:
        return False, "source_not_current"
    if scope == "draft_preview" and state == "draft":
        return True, "explicit_draft_preview"
    if state != "released":
        return False, "not_released"
    # Presence of these records does not authenticate the actual business approval.
    if not (document["default_retrieval_enabled"] and document.get("approval_reference")
            and document.get("approved_by") and document.get("effective_from")):
        return False, "release_record_incomplete"
    try:
        start = date.fromisoformat(document["effective_from"])
        end = date.fromisoformat(document["effective_until"]) if document.get("effective_until") else None
    except (TypeError, ValueError):
        return False, "invalid_effective_dates"
    today = as_of or date.today()
    if (end and end < start) or today < start or (end and today > end):
        return False, "not_effective"
    return True, "released"


def frontmatter(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise KnowledgeError("invalid_utf8", "正文不是有效的UTF-8文本") from None
    if "\ufffd" in text or "\x00" in text:
        raise KnowledgeError("invalid_text", "正文存在替换乱码字符或NUL")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise KnowledgeError("missing_frontmatter", "正文缺少受控元数据头")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise KnowledgeError("invalid_frontmatter", "元数据头没有结束边界") from None
    metadata = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[a-z_]+", key) or key in metadata:
            raise KnowledgeError("invalid_frontmatter", "元数据头字段不规范或重复")
        value = value.strip()
        if not value or value[0] in "[{|>&*":
            raise KnowledgeError("unsupported_frontmatter", "仅支持当前受控格式的平铺标量元数据", state="needs_review")
        metadata[key] = {"null": None, "true": True, "false": False}.get(value, value)
    return metadata, list(enumerate(lines[end + 1:], start=end + 2))


def sections(lines, native_format, title):
    """The registered text profile uses one title and level-2/numbered sections."""
    intro, result, current = [], [], None
    title_seen = False
    for number, line in lines:
        if line.startswith(("~~~", chr(96) * 3)) or re.match(r"^#{3,}\s", line):
            raise KnowledgeError("unsupported_structure", "代码围栏或更深标题需先人工复核", state="needs_review")
        if line == f"# {title}" or (native_format == "txt" and line == title):
            if title_seen or current:
                raise KnowledgeError("invalid_title", "正文标题重复或位置不正确")
            title_seen = True
            continue
        if line.startswith("# "):
            raise KnowledgeError("invalid_title", "正文出现未登记标题", state="needs_review")
        heading = re.match(r"^## (.+)$", line) if native_format == "md" else re.match(r"^(\d+ .+)$", line)
        if heading:
            current = {"heading": heading[1], "line_start": number, "lines": []}
            result.append(current)
        elif current is None:
            intro.append((number, line))
        else:
            current["lines"].append((number, line))
    if not title_seen or not result or any(line.startswith("# ") for _, line in intro):
        raise KnowledgeError("invalid_structure", "正文标题或章节结构不符合登记资料")
    if len({s["heading"] for s in result}) != len(result):
        raise KnowledgeError("duplicate_section", "正文存在重复章节")
    return "\n".join(line for _, line in intro).strip(), result


def tables(lines):
    result, cursor = [], 0
    while cursor < len(lines):
        if not lines[cursor][1].startswith("|"):
            cursor += 1
            continue
        start = cursor
        while cursor < len(lines) and lines[cursor][1].startswith("|"):
            cursor += 1
        block = lines[start:cursor]
        cells = [line.strip().strip("|").split("|") for _, line in block]
        cells = [[cell.strip() for cell in row] for row in cells]
        if (len(cells) < 3 or not all(re.fullmatch(r":?-{3,}:?", c) for c in cells[1])
                or not all(len(row) == len(cells[0]) for row in cells)):
            raise KnowledgeError("invalid_table", "表格表头或行列关系需要人工复核", state="needs_review")
        result.append({"ordinal": len(result) + 1, "headers": cells[0], "rows": cells[2:],
            "line_start": block[0][0], "line_end": block[-1][0],
            "text": "\n".join(line for _, line in block)})
    return result


def parse_document(document, raw):
    native = document["native_format"] in {"pdf", "docx"}
    if native:
        from backend.app.knowledge_formats import extract_docx, extract_pdf, native_location
        metadata, lines, locations = (extract_pdf if document["native_format"] == "pdf" else extract_docx)(document, raw)
    else:
        metadata, lines = frontmatter(raw)
    parser_version = NATIVE_PARSER_VERSION if native else PARSER_VERSION
    for key, expected in (("document_id", document["document_id"]), ("version", document["document_version"]),
                          ("title", document["title"]), (document["status_field"], document["source_status"])):
        if metadata.get(key) != expected:
            raise KnowledgeError("metadata_mismatch", "正文元数据与来源清单不一致")
    if "native_format" in metadata and metadata["native_format"] != document["native_format"]:
        raise KnowledgeError("metadata_mismatch", "正文格式元数据与清单不一致")
    for key in ("source_type", "effective_from", "effective_until", "approval_reference", "approved_by", "default_retrieval_enabled"):
        if key in metadata and metadata[key] != document[key]:
            raise KnowledgeError("metadata_mismatch", "正文来源与发布元数据和清单不一致")
    intro, blocks = sections(lines, "md" if native else document["native_format"], document["title"])
    identity = [document[k] for k in ("knowledge_base", "document_id", "document_version", "source_sha256")]
    units, contexts = [], []
    actual_ids = []
    for block in blocks:
        heading, body = block["heading"], block["lines"]
        parent_id = digest(identity + [parser_version, CHUNKING_VERSION, "context", heading])
        context_text = "\n\n".join(filter(None, [document["title"], intro, heading,
            "\n".join(line for _, line in body).strip()]))
        parent = {"context_id": parent_id, "document_id": document["document_id"],
            "document_version": document["document_version"], "source_sha256": document["source_sha256"],
            "knowledge_base": document["knowledge_base"], "section_path": [document["title"], heading],
            "context_text": context_text, "tables": tables(body), "unit_ids": []}
        if native:
            parent.update(native_location(locations, [block["line_start"]] + [n for n, line in body if line.strip()]))
            for table in parent["tables"]:
                table.update(native_location(locations, range(table["line_start"], table["line_end"]+1)),
                    ordinal=locations[table["line_start"]]["table_ordinal"])
                del table["line_start"], table["line_end"]
        if document["knowledge_base"] == "inventory_policies":
            starts = [(i, CLAUSE.fullmatch(line)) for i, (_, line) in enumerate(body) if CLAUSE.fullmatch(line)]
            if not starts:
                raise KnowledgeError("empty_section", "制度章节没有完整条款", state="needs_review")
            section_intro = "\n".join(line for _, line in body[:starts[0][0]]).strip()
            parts = []
            for i, (start, match) in enumerate(starts):
                finish = starts[i + 1][0] if i + 1 < len(starts) else len(body)
                source_lines = body[start:finish]
                text_lines = [(n, line) for n, line in source_lines if not line.startswith("|")]
                if not match[2] or not match[2].strip():
                    raise KnowledgeError("empty_unit", "条款正文为空")
                parts.append((match[1], text_lines, section_intro))
        else:
            unit_id = heading.split(" ", 1)[0]
            if unit_id in document["case_ids"] and not all(f"**{field}：**" in context_text for field in DIAGNOSTIC_FIELDS):
                raise KnowledgeError("incomplete_diagnostic_case", "故障条目缺少必要条件或换件限制", state="needs_review")
            parts = [(unit_id, body, "")]
        for unit_id, source_lines, section_intro in parts:
            text = "\n".join(line for _, line in source_lines).strip()
            if not text:
                raise KnowledgeError("empty_unit", "知识单元正文为空")
            embedding_text = "\n\n".join(filter(None, [document["title"], intro, heading, section_intro, text]))
            if len(embedding_text) > MAX_UNIT_CHARS:
                raise KnowledgeError("oversized_unit", "知识单元超长，需人工复核分块", state="needs_review")
            chunk_id = digest(identity + [parser_version, CHUNKING_VERSION, unit_id, digest(embedding_text.encode("utf-8"))])
            units.append({"knowledge_base": document["knowledge_base"], "document_id": document["document_id"],
                "document_version": document["document_version"], "source_sha256": document["source_sha256"],
                "unit_id": unit_id, "chunk_id": chunk_id, "parent_id": parent_id, "title": document["title"],
                "section_path": parent["section_path"], "unit_text": text, "embedding_text": embedding_text,
                "embedding_text_sha256": digest(embedding_text.encode("utf-8")),
                "line_start": source_lines[0][0], "line_end": source_lines[-1][0],
                "source_state": document["source_state"], "source_status": document["source_status"],
                "source_type": document["source_type"], "source_ids": document["source_ids"],
                "effective_from": document["effective_from"], "effective_until": document["effective_until"],
                "required_context_document_id": document["required_context_document_id"],
                "parser_version": parser_version, "chunking_version": CHUNKING_VERSION,
                "index_status": "not_indexed", "retrievable": False})
            if native:
                units[-1].update(native_location(locations, [n for n, line in source_lines if line.strip() and not line.startswith("|")]))
                del units[-1]["line_start"], units[-1]["line_end"]
                pages = document.get("clause_pages", {})
                if document["native_format"] == "pdf" and pages.get(unit_id) != units[-1]["page_start"]:
                    raise KnowledgeError("pdf_page_mismatch", "PDF条款实际起始页与登记不一致", state="needs_review")
            actual_ids.append(unit_id)
            parent["unit_ids"].append(unit_id)
        contexts.append(parent)
    if actual_ids != document["expected_unit_ids"] or len(actual_ids) != len(set(actual_ids)):
        raise KnowledgeError("unit_ids_mismatch", "条款或章节编号缺失、重复、乱序或未登记")
    return units, contexts


def parse_corpora(project_root=ROOT, *, knowledge_base=None):
    if knowledge_base is not None and knowledge_base not in CORPORA:
        raise KnowledgeError("invalid_knowledge_base", "不支持的知识域")
    root = Path(project_root).resolve()
    qualification_date = date.today()
    documents, units, contexts, inputs, shared_documents = [], [], [], [], {}
    for kb, (relative, folder) in CORPORA.items():
        if knowledge_base is not None and kb != knowledge_base:
            continue
        base = controlled_path(root, relative)
        manifest_path = controlled_path(base, "manifest.json")
        manifest, manifest_hash = read_json(manifest_path)
        if (manifest.get("collection") != kb or not isinstance(manifest.get("documents"), list)
                or not manifest["documents"]):
            raise KnowledgeError("invalid_manifest", "清单知识域或文档列表不正确")
        inputs.append({"path": manifest_path.relative_to(root).as_posix(), "sha256": manifest_hash})
        if kb == "general_diagnostics":
            shared_documents[kb] = manifest.get("shared_context_document_id")
            for field in ("sources_path", "part_type_map_path"):
                path = controlled_path(base, manifest.get(field))
                if path.suffix != ".json":
                    raise KnowledgeError("invalid_auxiliary_path", "辅助清单必须为语料目录内JSON文件")
                _, sha = read_json(path)
                inputs.append({"path": path.relative_to(root).as_posix(), "sha256": sha})
        seen = set()
        for row in manifest["documents"]:
            if not isinstance(row, dict):
                raise KnowledgeError("invalid_document", "文档登记必须为对象")
            doc_id, version = row.get("document_id"), row.get("version")
            prefix = "INV" if kb == "inventory_policies" else "DX"
            ids = row.get("clause_ids" if prefix == "INV" else "section_ids")
            if (not isinstance(doc_id, str) or not re.fullmatch(prefix + r"-[A-Z]{3}-\d{3}", doc_id)
                    or doc_id in seen or not isinstance(version, str) or not version
                    or not isinstance(row.get("title"), str) or not row["title"]
                    or not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids)
                    or len(ids) != len(set(ids))):
                raise KnowledgeError("invalid_document", "文档身份或预期编号缺失、重复或不规范")
            seen.add(doc_id)
            unit_pattern = re.escape(doc_id) + r"-C\d{3}" if prefix == "INV" else r"DX-[A-Z]{3,4}-\d{2}"
            if not all(re.fullmatch(unit_pattern, unit_id) for unit_id in ids):
                raise KnowledgeError("invalid_document", "条款或章节标识不符合登记规范")
            status_field = "approval_status" if prefix == "INV" else "review_status"
            status = row.get(status_field, manifest.get(status_field))
            if (not isinstance(status, str) or row.get("native_format") not in ("md", "txt", "pdf", "docx")
                    or not isinstance(row.get("case_ids", []), list)
                    or not all(isinstance(i, str) for i in row.get("case_ids", []))
                    or not isinstance(row.get("source_ids", []), list)
                    or not all(isinstance(i, str) for i in row.get("source_ids", []))
                    or not set(row.get("case_ids", [])) <= set(ids)):
                raise KnowledgeError("invalid_document", "文档状态、格式或故障条目登记不规范")
            if prefix == "DX" and (row.get("requires_shared_context") is not (doc_id != shared_documents[kb])
                    or row.get("case_ids") != ([] if doc_id == shared_documents[kb] else ids)):
                raise KnowledgeError("invalid_document", "故障条目与公共规则依赖登记不一致")
            state = {"pending_approval": "draft", "pending_technical_review": "draft",
                     "approved": "released", "withdrawn": "withdrawn", "superseded": "superseded"}.get(status, "unknown")
            document = {"knowledge_base": kb, "document_id": doc_id, "document_version": version,
                "title": row["title"], "native_format": row.get("native_format"), "source_sha256": row.get("sha256"),
                "source_status": status, "status_field": status_field, "source_state": state,
                "source_type": row.get("source_type", manifest.get("source_type")),
                "drafted_on": row.get("drafted_on"), "owner_role": row.get("owner_role"),
                "source_ids": row.get("source_ids", []),
                "effective_from": row.get("effective_from", manifest.get("effective_from")),
                "effective_until": row.get("effective_until", manifest.get("effective_until")),
                "default_retrieval_enabled": row.get("default_retrieval_enabled", manifest.get("default_retrieval_enabled", False)) is True,
                "approval_reference": row.get("approval_reference"), "approved_by": row.get("approved_by"),
                "expected_unit_ids": ids, "case_ids": row.get("case_ids", []),
                "required_context_document_id": manifest.get("shared_context_document_id") if row.get("requires_shared_context") else None,
                "parse_status": "pending", "index_status": "not_indexed", "retrievable": False}
            if document["native_format"] == "pdf":
                document["clause_pages"] = row.get("clause_pages", {})
            try:
                if document["native_format"] == "pdf" and (not isinstance(document["clause_pages"], dict)
                        or set(document["clause_pages"]) != set(ids)
                        or any(type(page) is not int or page < 1 for page in document["clause_pages"].values())):
                    raise KnowledgeError("invalid_pdf_locator", "PDF页码登记缺失或不规范", state="needs_review")
                path = controlled_path(base, row.get("path"))
                if (not path.is_relative_to(base / folder) or path.name != f"{doc_id}.{document['native_format']}"
                        or document["native_format"] not in {"md", "txt", "pdf", "docx"}):
                    raise KnowledgeError("invalid_source_profile", "只能读取登记原生目录及对应文件格式")
                raw = read_bytes(path)
                if not isinstance(document["source_sha256"], str) or digest(raw) != document["source_sha256"]:
                    raise KnowledgeError("source_hash_mismatch", "原生来源哈希与清单不一致")
                document["path"] = path.relative_to(root).as_posix()
                inputs.append({"path": document["path"], "sha256": digest(raw)})
                if state == "unknown":
                    raise KnowledgeError("unknown_source_status", "来源状态未识别，须复核", state="needs_review")
                parsed_units, parents = parse_document(document, raw)
                units.extend(parsed_units)
                contexts.extend(parents)
                document["parse_status"] = "parsed"
            except KnowledgeError as error:
                document.update(parse_status=error.state, error_code=error.code, detail=error.detail)
            documents.append(document)
    registry_path = root/'data/knowledge/uploads/approved.json'
    if registry_path.exists():
        approved, sha = read_json(registry_path)
        if not isinstance(approved.get('documents'), list):
            raise KnowledgeError('invalid_upload_registry', '上传资料登记清单不完整')
        inputs.append({'path': registry_path.relative_to(root).as_posix(), 'sha256': sha})
        for item in approved.get('documents', []):
            if (not isinstance(item, dict) or not {'id','kb','path','sha','format','title','reviewed_at','reviewer_id','chunks'} <= item.keys()
                    or not isinstance(item['id'], str) or not re.fullmatch(r'[a-f0-9]{32}', item['id'])
                    or item['kb'] not in CORPORA or item['format'] not in {'txt','md','pdf','docx'}
                    or item['path'] != f"data/knowledge/uploads/{item['id']}.{item['format']}"
                    or not isinstance(item['chunks'], list) or not 1 <= len(item['chunks']) <= 300
                    or any(not isinstance(c, dict) or not isinstance(c.get('text'), str) or not c['text'].strip()
                        or not isinstance(c.get('location'), dict) or not c['location']
                        or any(type(v) is not int or v < 1 for v in c['location'].values()) for c in item['chunks'])):
                raise KnowledgeError('invalid_upload_registry', '上传资料登记清单内容不规范')
            kb = item['kb']
            if kb not in CORPORA or (knowledge_base is not None and kb != knowledge_base): continue
            path = controlled_path(root, item['path'])
            if not path.is_relative_to(root/'data/knowledge/uploads') or digest(read_bytes(path)) != item['sha']:
                raise KnowledgeError('source_hash_mismatch', '已审核上传资料原件发生变化')
            inputs.append({'path': item['path'], 'sha256': item['sha']})
            doc_id, version = 'UPL-'+item['id'], '1'
            ids = [f'{doc_id}-C{i:03}' for i in range(1,len(item['chunks'])+1)]
            document = {'knowledge_base': kb,'document_id': doc_id,'document_version': version,'title': item['title'],
                'native_format': item['format'],'source_sha256': item['sha'],'source_status': 'admin_reviewed_reference',
                'status_field': 'admin_review_status','source_state': 'reference','source_type': 'local_upload_reviewed',
                'drafted_on': item['reviewed_at'][:10],'owner_role': '资料上传人','source_ids': [],'effective_from': None,'effective_until': None,
                'default_retrieval_enabled': False,'approval_reference': item['id'],'approved_by': str(item['reviewer_id']),
                'expected_unit_ids': ids,'case_ids': [],'required_context_document_id': 'DX-GEN-001' if kb == 'general_diagnostics' else None,
                'parse_status': 'parsed','index_status': 'not_indexed','retrievable': False,'path': item['path']}
            documents.append(document)
            for unit_id, chunk in zip(ids,item['chunks']):
                identity = [doc_id,version,item['sha'],unit_id]
                parent_id, chunk_id = digest(identity+['context']),digest(identity+['unit'])
                text = chunk['text']; section = [item['title'],f'上传原文片段{unit_id[-3:]}']
                unit = {'knowledge_base':kb,'document_id':doc_id,'document_version':version,'source_sha256':item['sha'],
                    'unit_id':unit_id,'chunk_id':chunk_id,'parent_id':parent_id,'title':item['title'],'section_path':section,
                    'unit_text':text,'embedding_text':item['title']+'\n'+text,'embedding_text_sha256':digest((item['title']+'\n'+text).encode()),
                    'source_state':'reference','source_status':'admin_reviewed_reference','source_type':'local_upload_reviewed','source_ids':[],
                    'effective_from':None,'effective_until':None,'required_context_document_id':document['required_context_document_id'],
                    'parser_version':'uploaded-blocks-v1','chunking_version':CHUNKING_VERSION,'index_status':'not_indexed','retrievable':False,**chunk['location']}
                units.append(unit)
                contexts.append({'context_id':parent_id,'document_id':doc_id,'document_version':version,'source_sha256':item['sha'],
                    'knowledge_base':kb,'section_path':section,'context_text':text,'tables':[],'unit_ids':[unit_id]})
    for kb, common_id in shared_documents.items():
        if not any(d["knowledge_base"] == kb and d["document_id"] == common_id and d["parse_status"] == "parsed" for d in documents):
            for document in documents:
                if document["knowledge_base"] == kb and document["parse_status"] == "parsed":
                    document.update(parse_status="needs_review", error_code="missing_shared_context", detail="故障公共规则缺失或无效")
    failed_ids = {(d["knowledge_base"], d["document_id"]) for d in documents if d["parse_status"] != "parsed"}
    units = [u for u in units if (u["knowledge_base"], u["document_id"]) not in failed_ids]
    contexts = [c for c in contexts if (c["knowledge_base"], c["document_id"]) not in failed_ids]
    for document in documents:
        document["scope_eligibility"] = {scope: dict(zip(("eligible", "reason"), source_scope_eligibility(document, scope, as_of=qualification_date)))
            for scope in ("released_only", "draft_preview")}
    snapshot = {"parser_version": PARSER_VERSION, "native_parser_version": NATIVE_PARSER_VERSION, "chunking_version": CHUNKING_VERSION,
        "qualification_date": qualification_date.isoformat(),
        "coverage": "registered_native_formats", "inputs": sorted(inputs, key=lambda i: i["path"]),
        "documents": documents, "units": units, "contexts": contexts, "index_status": "not_indexed"}
    verify_sources(snapshot, root)
    failed = [d for d in documents if d["parse_status"] in {"failed", "needs_review"}]
    snapshot["parse_status"] = "failed" if failed else "parsed"
    snapshot["parse_id"] = digest([PARSER_VERSION, NATIVE_PARSER_VERSION, CHUNKING_VERSION, snapshot["qualification_date"], snapshot["inputs"]])
    snapshot["summary"] = {"registered_documents": len(documents),
        "parsed_documents": sum(d["parse_status"] == "parsed" for d in documents),
        "unsupported_documents": sum(d["parse_status"] == "unsupported_format" for d in documents),
        "failed_documents": len(failed), "units": len(units), "contexts": len(contexts),
        "released_scope_documents": sum(d["scope_eligibility"]["released_only"]["eligible"] for d in documents),
        "draft_preview_documents": sum(d["scope_eligibility"]["draft_preview"]["eligible"] for d in documents),
        "indexed_documents": 0}
    return snapshot


def verify_sources(snapshot, project_root=ROOT):
    root = Path(project_root).resolve()
    for item in snapshot["inputs"]:
        path = controlled_path(root, item["path"])
        if not (any(path.is_relative_to((root / relative).resolve()) for relative, _ in CORPORA.values()) or path.is_relative_to(root/'data/knowledge/uploads')):
            raise KnowledgeError("path_outside_corpus", "快照来源不在登记语料目录")
        if digest(read_bytes(path)) != item["sha256"]:
            raise KnowledgeError("knowledge_changed", "解析期间或解析后来源发生变化，须重新解析")


def export_parsed(snapshot, project_root=ROOT):
    """Export immutable parse-only files; never publish an active/indexed generation."""
    if snapshot["parse_status"] != "parsed":
        raise KnowledgeError("parse_failed", "存在解析失败或需复核资料，不导出可用片段快照")
    root = Path(project_root).resolve()
    verify_sources(snapshot, root)
    report = {k: v for k, v in snapshot.items() if k not in {"units", "contexts"}}
    payloads = {"report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n"}
    payloads.update({f"{key}.jsonl": "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in snapshot[key])
                     for key in ("units", "contexts")})
    base = controlled_path(root, "data/chroma/rag/parsed")
    base.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot["parse_id"]):
        raise KnowledgeError("invalid_parse_id", "解析快照标识不规范")
    target = controlled_path(base, snapshot["parse_id"])
    if target.exists():
        if not target.is_dir() or any(read_bytes(target / name) != value.encode("utf-8") for name, value in payloads.items()):
            raise KnowledgeError("snapshot_conflict", "已有解析快照内容不完整或被修改，不覆盖")
        return target, False
    with TemporaryDirectory(prefix=".parse-", dir=base) as temporary:
        staging = Path(temporary).resolve()
        if not staging.is_relative_to(base) or not staging.name.startswith(".parse-"):
            raise KnowledgeError("invalid_output_path", "解析临时目录不在预期范围")
        for name, value in payloads.items():
            (staging / name).write_text(value, encoding="utf-8", newline="\n")
        verify_sources(snapshot, root)
        try:
            staging.rename(target)
        except FileExistsError:
            raise KnowledgeError("snapshot_conflict", "相同解析快照正被其他进程写入，请核对后重试") from None
    return target, True

