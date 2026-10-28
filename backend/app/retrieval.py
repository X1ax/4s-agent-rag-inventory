"""Owned local generations, live source qualification, dense and hybrid retrieval."""
import json
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import ChromaError

from backend.app.ai import EmbeddingClient, embedding_contract, validate_vectors
from backend.app.knowledge import (ROOT, CORPORA, KnowledgeError, controlled_path, digest,
    parse_corpora, read_bytes, read_json, verify_sources)

from backend.app.knowledge_lifecycle import qualify_sources, verify_qualification
from backend.app.lexical import DOCUMENT_CODE, UNIT_CODE, fuse, lexical_candidates

INDEX_VERSION = "dense-bge-m3-v1"
RETRIEVAL_VERSION = "hybrid-tfidf-rrf-context-v2"
CANDIDATE_LIMIT = 20
CONTEXT_BUDGET = 12000
# Persist every add, including its short tail: 1.5.9 cache reloads can lose unflushed ID mappings.
HNSW_CONFIG = {"space": "cosine", "batch_size": 16, "sync_threshold": 1}


@contextmanager
def index_owner(project_root=ROOT):
    base = controlled_path(Path(project_root).resolve(), "data/chroma/rag")
    base.mkdir(parents=True, exist_ok=True)
    # Keep this inode: deleting a lock file can allow two processes to own different locks.
    with (base / "owner.lock").open("a+b", buffering=0) as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise KnowledgeError("index_in_use", "本地知识索引正被其他进程使用，请结束该索引操作后重试") from None
        try:
            yield base
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def chroma_client(directory):
    return chromadb.PersistentClient(path=str(directory / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False))


def load_generation(base, generation_id=None):
    active = None
    if generation_id is None:
        if not (base / "active.json").exists():
            raise KnowledgeError("index_not_built", "知识索引尚未构建")
        active, _ = read_json(base / "active.json")
        generation_id = active.get("generation_id", "")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", generation_id):
        raise KnowledgeError("invalid_generation", "活动索引代次标识无效")
    directory = controlled_path(base, "generations/" + generation_id)
    report, report_sha = read_json(directory / "generation.json")
    if ((active is not None and active.get("generation_sha256") != report_sha) or report.get("generation_id") != generation_id
            or report.get("index_status") != "indexed" or report.get("index_version") != INDEX_VERSION):
        raise KnowledgeError("invalid_generation", "索引代次报告不完整或不兼容")
    for name in ("sources.json", "units.jsonl", "contexts.jsonl"):
        if digest(read_bytes(directory / name)) != report.get("files", {}).get(name):
            raise KnowledgeError("index_corrupt", "索引快照内容与构建记录不一致")
    sources, _ = read_json(directory / "sources.json")
    try:
        units = [json.loads(line) for line in (directory / "units.jsonl").read_text(encoding="utf-8").splitlines()]
        contexts = [json.loads(line) for line in (directory / "contexts.jsonl").read_text(encoding="utf-8").splitlines()]
        if (len(units) != report["unit_count"] or len({u["chunk_id"] for u in units}) != len(units)
                or len(contexts) != report["context_count"]
                or not {u["parent_id"] for u in units} <= {c["context_id"] for c in contexts}):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise KnowledgeError("index_corrupt", "索引单元或上下文不完整") from None
    return directory, report, sources, units, contexts


def publish_generation(base, generation_id, directory):
    temporary = base / (".active-" + generation_id + ".json")
    write_json(temporary, {"generation_id": generation_id, "generation_sha256": digest(read_bytes(directory / "generation.json"))})
    os.replace(temporary, base / "active.json")


def activate_generation(settings, generation_id, project_root=ROOT):
    """Explicit recovery only: historical sources must still match the current registry."""
    with index_owner(project_root) as base:
        directory, report, sources, units, _ = load_generation(base, generation_id)
        verify_sources(sources, project_root)
        if embedding_contract(settings) != report["embedding"]:
            raise KnowledgeError("index_model_mismatch", "历史代次与当前Embedding配置不兼容")
        client = chroma_client(directory)
        try:
            validate_collections(client, report, units)
            verify_sources(sources, project_root)
            publish_generation(base, generation_id, directory)
        finally:
            client.close()
        return {"index_status": "indexed", "generation_id": generation_id, "embedding_requests": 0}


def historical_versions(base):
    known = {}
    for directory in sorted((base / "generations").glob("*")):
        if not re.fullmatch(r"[a-f0-9]{32}", directory.name):
            continue
        report_path = controlled_path(base, "generations/" + directory.name + "/generation.json")
        if not report_path.is_file():
            continue  # A crash between mkdir and the first report never published this directory.
        report, _ = read_json(report_path)
        if report.get("index_status") != "indexed":
            continue
        _, _, sources, _, _ = load_generation(base, directory.name)
        for doc in sources["documents"]:
            key = (doc["knowledge_base"], doc["document_id"], doc["document_version"])
            if key in known and known[key] != doc["source_sha256"]:
                raise KnowledgeError("document_version_reused", "历史代次中相同文档身份对应不同正文，须人工复核")
            known[key] = doc["source_sha256"]
    return known


def validate_collections(client, report, units):
    for kb in CORPORA:
        collection = client.get_collection(kb, embedding_function=None)
        expected = {u["chunk_id"] for u in units if u["knowledge_base"] == kb}
        if (collection.count() != len(expected) or set(collection.get(include=[])["ids"]) != expected
                or collection.metadata != {"generation_id": report["generation_id"], "index_version": INDEX_VERSION,
                    "embedding_model": report["embedding"]["model"], "dimension": report["embedding"]["dimension"]}
                or collection.configuration["hnsw"]["space"] != "cosine"):
            raise KnowledgeError("index_corrupt", "Chroma数量、片段标识或模型配置校验失败")
        if expected:
            # Metadata/count reads do not load the persisted vector segment.
            probe = collection.get(ids=[min(expected)], include=["embeddings"])
            found = collection.query(query_embeddings=probe["embeddings"], n_results=1, include=[])
            if not found["ids"][0] or not set(found["ids"][0]) <= expected:
                raise KnowledgeError("index_corrupt", "持久化向量段查询校验失败")


def build_index(settings, project_root=ROOT, *, embedder=None, progress=None):
    started = time.monotonic()
    with index_owner(project_root) as base:
        snapshot = parse_corpora(project_root)
        if snapshot["parse_status"] != "parsed":
            raise KnowledgeError("parse_failed", "存在解析失败或需复核资料，不构建索引")
        embedding = embedding_contract(settings)
        fingerprint = digest([INDEX_VERSION, HNSW_CONFIG, embedding, snapshot["inputs"], snapshot["parser_version"],
            snapshot["chunking_version"], [u["chunk_id"] for u in snapshot["units"]]])
        if (base / "active.json").exists():
            old_dir, old_report, _, old_units, _ = load_generation(base)
            if old_report["fingerprint"] == fingerprint:
                client = chroma_client(old_dir)
                try:
                    validate_collections(client, old_report, old_units)
                finally:
                    client.close()
                verify_sources(snapshot, project_root)
                return {"index_status": "indexed", "generation_id": old_report["generation_id"],
                    "reused": True, "embedding_requests": 0, "embedded_texts": 0, "unit_count": len(old_units)}
            previous = historical_versions(base)
            if any(previous.get((d["knowledge_base"], d["document_id"], d["document_version"]), d["source_sha256"]) != d["source_sha256"] for d in snapshot["documents"]):
                raise KnowledgeError("document_version_reused", "相同文档ID和版本的正文发生变化，请先提升文档版本并复核")
        embedder = embedder or EmbeddingClient(settings)
        if embedder.contract != embedding:
            raise KnowledgeError("embedding_model_mismatch", "构建向量的模型配置与索引不一致")
        generation_id = uuid4().hex
        directory = controlled_path(base, "generations/" + generation_id)
        directory.mkdir(parents=True)
        report = {"generation_id": generation_id, "index_version": INDEX_VERSION, "fingerprint": fingerprint,
            "embedding": embedding, "parser_version": snapshot["parser_version"],
            "native_parser_version": snapshot["native_parser_version"],
            "chunking_version": snapshot["chunking_version"], "index_status": "indexing",
            "created_at": datetime.now(timezone.utc).isoformat(), "chroma_version": version("chromadb"),
            "unit_count": len(snapshot["units"]), "context_count": len(snapshot["contexts"])}
        write_json(directory / "generation.json", report)
        client = None
        try:
            client = chroma_client(directory)
            for kb in CORPORA:
                collection = client.create_collection(kb, embedding_function=None,
                    configuration={"hnsw": HNSW_CONFIG}, metadata={"generation_id": generation_id,
                        "index_version": INDEX_VERSION, "embedding_model": embedding["model"], "dimension": embedding["dimension"]})
                units = [u for u in snapshot["units"] if u["knowledge_base"] == kb]
                for offset in range(0, len(units), 16):
                    batch = units[offset:offset+16]
                    vectors = embedder.embed([u["embedding_text"] for u in batch], build=True)
                    collection.add(ids=[u["chunk_id"] for u in batch], embeddings=vectors,
                        documents=[u["embedding_text"] for u in batch], metadatas=[{key: u[key] for key in (
                            "knowledge_base", "document_id", "document_version", "source_sha256", "unit_id", "parent_id")} for u in batch])
                    if progress:
                        progress({"knowledge_base": kb, "indexed_units": min(offset+16, len(units)), "total_units": len(units)})
            validate_collections(client, report, snapshot["units"])
            client.close()
            client = None
            # Public close/reopen flushes and validates persisted storage before pointer publication.
            client = chroma_client(directory)
            validate_collections(client, report, snapshot["units"])
            client.close()
            client = None
            for document in snapshot["documents"]:
                if document["parse_status"] == "parsed":
                    document["index_status"] = "indexed"
            for unit in snapshot["units"]:
                unit["index_status"] = "indexed"
            sources = {k: v for k, v in snapshot.items() if k not in {"units", "contexts"}}
            sources["index_status"] = "indexed"
            sources["summary"]["indexed_documents"] = sources["summary"]["parsed_documents"]
            write_json(directory / "sources.json", sources)
            for key in ("units", "contexts"):
                (directory / f"{key}.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in snapshot[key]), encoding="utf-8", newline="\n")
            report.update(index_status="indexed", files={name: digest(read_bytes(directory / name))
                for name in ("sources.json", "units.jsonl", "contexts.jsonl")}, **embedder.usage(), elapsed_seconds=round(time.monotonic()-started, 3))
            write_json(directory / "generation.json", report)
            verify_sources(snapshot, project_root)
            publish_generation(base, generation_id, directory)
            return {**report, "reused": False, "indexed_documents": sources["summary"]["indexed_documents"],
                "unsupported_documents": sources["summary"]["unsupported_documents"]}
        except Exception as error:
            report.update(index_status="failed", error_code=error.code if isinstance(error, KnowledgeError) else "index_build_failed",
                **embedder.usage(), elapsed_seconds=round(time.monotonic()-started, 3))
            write_json(directory / "generation.json", report)
            if isinstance(error, KnowledgeError):
                raise
            raise KnowledgeError("index_build_failed", "知识索引构建失败，未切换活动代次") from None
        finally:
            if client is not None:
                client.close()


class DenseIndex:
    def __init__(self, client, report, sources, units, contexts, settings, root, embedder):
        self.client, self.report, self.sources = client, report, sources
        self.units = {u["chunk_id"]: u for u in units}
        self.contexts = {c["context_id"]: c for c in contexts}
        self.settings, self.root, self.embedder = settings, root, embedder

    def search(self, question, knowledge_base, *, scope="released_only", top_k=5, mode="dense",
            document_ids=None, document_versions=None, query_vector=None):
        if (not isinstance(question, str) or not question.strip() or len(question) > 2000
                or knowledge_base not in CORPORA or scope not in {"released_only", "draft_preview"}
                or mode not in {"dense", "hybrid"} or type(top_k) is not int or not 1 <= top_k <= 10
                or (document_ids is not None and (not isinstance(document_ids, list)
                    or not all(isinstance(d, str) for d in document_ids)))
                or (document_versions is not None and (not isinstance(document_versions, dict)
                    or not all(isinstance(d, str) and isinstance(v, str) for d, v in document_versions.items())))):
            raise KnowledgeError("invalid_query", "查询需指定有效知识域、范围、模式、文档过滤及1至10条结果，问题限2000字符")
        qualification = qualify_sources(self.sources, self.root, knowledge_base, scope)
        allowed = set(qualification["allowed"])
        if document_ids is not None:
            allowed.intersection_update(document_ids)
        if document_versions is not None:
            allowed = {d for d in allowed if qualification["allowed"][d]["document_version"] == document_versions.get(d)}
        result = {"generation_id": self.report["generation_id"], "retrieval_mode": mode,
            "retrieval_version": RETRIEVAL_VERSION, "knowledge_base": knowledge_base, "knowledge_scope": scope,
            "top_k": top_k, "status": "no_eligible_documents", "hits": [], "contexts": [], "embedding_requests": 0,
            "unavailable_documents": qualification["unavailable"], "domain_error": qualification["domain_error"],
            "document_ids": document_ids, "document_versions": document_versions,
            "candidate_limit": CANDIDATE_LIMIT, "context_budget": CONTEXT_BUDGET, "context_characters": 0,
            "omitted_contexts": [], "missing_exact_ids": []}

        def finished():
            verify_qualification(qualification, self.sources, self.root, knowledge_base, scope)
            return result

        if not allowed:
            return finished()
        if embedding_contract(self.settings) != self.report["embedding"]:
            raise KnowledgeError("index_model_mismatch", "当前Embedding配置与活动索引不兼容，须重建")
        eligible = [u for u in self.units.values() if u["knowledge_base"] == knowledge_base and u["document_id"] in allowed]
        exact_ids = list(dict.fromkeys(code.upper() for code in UNIT_CODE.findall(question))) if mode == "hybrid" else []
        by_unit = {u["unit_id"]: u["chunk_id"] for u in eligible}
        exact = [by_unit[code] for code in exact_ids if code in by_unit]
        result["missing_exact_ids"] = [code for code in exact_ids if code not in by_unit]
        if result["missing_exact_ids"] or len(exact) > top_k:
            result["status"] = "exact_not_found" if result["missing_exact_ids"] else "exact_limit_exceeded"
            return finished()
        if query_vector is None:
            embedder = self.embedder or EmbeddingClient(self.settings)
            if embedder.contract != self.report["embedding"]:
                raise KnowledgeError("index_model_mismatch", "查询向量配置与活动索引不兼容")
            vectors = embedder.embed([question.strip()])
        else:
            # Internal async API path only; callers cannot submit vectors over HTTP.
            vectors = validate_vectors([query_vector], 1)
        result["embedding_requests"] = 1
        try:
            collection = self.client.get_collection(knowledge_base, embedding_function=None)
            retrieved = collection.query(query_embeddings=vectors, n_results=min(CANDIDATE_LIMIT, len(eligible)),
                where={"document_id": {"$in": sorted(allowed)}}, include=["distances"])
        except ChromaError:
            raise KnowledgeError("index_query_failed", "本地向量检索失败，请检查索引状态后重试；本次未返回检索结果") from None
        dense = []
        for chunk_id, distance in zip(retrieved["ids"][0], retrieved["distances"][0], strict=True):
            unit = self.units.get(chunk_id)
            if (unit is None or unit["knowledge_base"] != knowledge_base or unit["document_id"] not in allowed
                    or not math.isfinite(distance)):
                raise KnowledgeError("index_corrupt", "检索片段不属于本次允许的来源")
            dense.append((chunk_id, distance))
        dense.sort(key=lambda item: (item[1], item[0]))
        lexical = lexical_candidates(question, eligible, CANDIDATE_LIMIT) if mode == "hybrid" else []
        if mode == "hybrid":
            ordered, scores = fuse(dense, lexical, exact)
        else:
            ordered, scores = [key for key, _ in dense], {}
        distances, lexical_scores = dict(dense), dict(lexical)
        candidates = [{k: v for k, v in self.units[key].items() if k not in {"embedding_text", "index_status", "retrievable"}}
            | {"distance": distances.get(key), **scores.get(key, {}), **({"lexical_score": lexical_scores.get(key)} if mode == "hybrid" else {})}
            for key in ordered[:top_k]]
        supplemental = []
        if mode == 'hybrid' and knowledge_base == 'inventory_policies' and not exact_ids:
            # Repeated clauses in one section must not hide the other branch's leading sections.
            for branch in (dense, lexical):
                parents = set()
                for key, _ in branch:
                    unit = self.units[key]
                    if unit['parent_id'] not in parents:
                        parents.add(unit['parent_id'])
                        supplemental.append(unit)
                    if len(parents) == top_k:
                        break
        context_allowed = allowed | ({"DX-GEN-001"} if knowledge_base == "general_diagnostics" else set())
        hits, contexts, omitted = self.expand_contexts(candidates, knowledge_base, context_allowed, supplemental)
        if exact_ids and not set(exact_ids) <= {u["unit_id"] for u in hits}:
            result.update(status="insufficient_context", omitted_contexts=omitted)
            return finished()
        result.update(status="retrieved" if hits else "no_hits", hits=hits, contexts=contexts,
            context_characters=sum(len(c["context_text"]) for c in contexts), omitted_contexts=omitted,
            dense_candidates=len(dense), lexical_candidates=len(lexical), rrf_constant=60 if mode == "hybrid" else None)
        return finished()

    def expand_contexts(self, candidates, knowledge_base, allowed, supplemental=()):
        selected, omitted, hits = {}, [], []
        size = 0

        def add(context, reason):
            nonlocal size
            key = context["context_id"]
            if key in selected:
                return True
            if size + len(context["context_text"]) > CONTEXT_BUDGET:
                omitted.append({"context_id": key, "document_id": context["document_id"], "reason": "context_budget"})
                return False
            selected[key] = context | {"expansion_reason": reason}
            size += len(context["context_text"])
            return True

        if knowledge_base == "general_diagnostics":
            for context in self.contexts.values():
                if context["document_id"] == "DX-GEN-001":
                    if not add(context, "required_common_rules"):
                        raise KnowledgeError("required_context_too_large", "公共规则超出证据预算，不能省略限制后继续检索")
        for unit in candidates:
            context = self.contexts[unit["parent_id"]]
            if context["knowledge_base"] != knowledge_base or context["document_id"] not in allowed:
                raise KnowledgeError("index_corrupt", "上下文不属于本次允许来源")
            if add(context, "parent_section"):
                hits.append(unit)
        # Expand explicit document references once, only after all original hit parents.
        references = {code.upper() for context in list(selected.values())
            for code in DOCUMENT_CODE.findall(context["context_text"])}
        present = {c["document_id"] for c in selected.values()}
        for doc_id in sorted(references - present):
            if doc_id not in allowed:
                omitted.append({"document_id": doc_id, "reason": "reference_unavailable_or_filtered"})
                continue
            for context in self.contexts.values():
                if context["knowledge_base"] == knowledge_base and context["document_id"] == doc_id:
                    add(context, "explicit_reference")
        # Preserve all original evidence first; add whole eligible sections only within the same budget.
        for unit in supplemental:
            context = self.contexts[unit['parent_id']]
            if context['knowledge_base'] != knowledge_base or context['document_id'] not in allowed:
                raise KnowledgeError('index_corrupt', '补充上下文不属于本次允许来源')
            add(context, 'branch_candidate_section')
        return hits, list(selected.values()), omitted


@contextmanager
def open_index(settings, project_root=ROOT, *, embedder=None):
    with index_owner(project_root) as base:
        directory, report, sources, units, contexts = load_generation(base)
        if embedding_contract(settings) != report["embedding"]:
            raise KnowledgeError("index_model_mismatch", "当前Embedding配置与活动索引不兼容，须重建")
        client = chroma_client(directory)
        try:
            validate_collections(client, report, units)
            yield DenseIndex(client, report, sources, units, contexts, settings, project_root, embedder)
        finally:
            client.close()
