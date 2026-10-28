"""Authenticated read-only RAG endpoints; no stock services or write tools."""
import asyncio
import logging
import threading
import time
from functools import partial
from typing import Annotated, Literal
from uuid import uuid4

from chromadb.errors import ChromaError
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator, model_validator

from backend.app.ai import chat_json, query_embedding
from backend.app.knowledge import CORPORA, ROOT, KnowledgeError
from backend.app.knowledge_answers import StrictModel, answer_messages, bind_answer, citation_pool, part_types, requires_live_data
from backend.app.knowledge_lifecycle import qualify_sources, verify_qualification
from backend.app.retrieval import open_index, build_index
from backend.app.security import current_user

logger = logging.getLogger("uvicorn.error.knowledge")
KnowledgeBase = Literal["inventory_policies", "general_diagnostics"]
KnowledgeScope = Literal["released_only", "draft_preview"]
router = APIRouter(prefix="/api/knowledge", tags=["知识服务"], dependencies=[Depends(current_user)])


class KnowledgeQuery(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    knowledge_base: KnowledgeBase
    knowledge_scope: KnowledgeScope = "released_only"
    top_k: int = Field(default=5, ge=1, le=10)
    retrieval_mode: Literal["dense", "hybrid"] | None = None
    document_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(default=None, min_length=1, max_length=20)
    document_versions: dict[Annotated[str, Field(min_length=1, max_length=64)], Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(default=None, max_length=20)

    @model_validator(mode='after')
    def default_retrieval(self):
        if self.retrieval_mode is None:
            self.retrieval_mode = 'hybrid' if self.knowledge_base == 'inventory_policies' else 'dense'
        return self

    @field_validator("question")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class KnowledgeRuntime:
    def __init__(self, settings, root=ROOT):
        self.settings, self.root = settings, root
        self.index = self.owner = None
        self.error = None
        self.maintenance = False
        # ponytail: serialize local index reads; revisit only for measured contention.
        self.lock = threading.RLock()

    def start(self):
        self.owner = open_index(self.settings, self.root)
        try:
            self.index = self.owner.__enter__()
        except KnowledgeError as error:
            self.error = error
        except (OSError, ValueError, ChromaError):
            self.error = KnowledgeError("index_unavailable", "本地索引不可用，请停服后检查或重建")

    def close(self):
        with self.lock:
            if self.index is not None:
                self.owner.__exit__(None, None, None)
                self.index = None

    def read(self, function, *args, **kwargs):
        if self.maintenance:
            raise KnowledgeError('index_maintenance', '知识索引正在更新，请稍后重试；库存业务仍可使用')
        with self.lock:
            if self.index is None:
                raise self.error or KnowledgeError("index_unavailable", "本地索引未就绪")
            return function(self.index, *args, **kwargs)

    def rebuild(self):
        self.maintenance = True
        try:
            with self.lock:
                self.close()
                try:
                    report = build_index(self.settings, self.root)
                finally:
                    self.error = None
                    self.start()
                if self.index is None:
                    raise self.error or KnowledgeError('index_unavailable', '新索引未能加载')
                return report
        finally:
            self.maintenance = False


def qualification(index, query):
    known = {d["document_id"] for d in index.sources["documents"] if d["knowledge_base"] == query.knowledge_base}
    ids = set(query.document_ids or []) | set(query.document_versions or {})
    if ids - known or (query.document_versions is not None and not query.document_versions):
        raise KnowledgeError("invalid_document_filter", "文档过滤只能使用当前知识域已登记的编号及版本")
    return qualify_sources(index.sources, index.root, query.knowledge_base, query.knowledge_scope)


def selected_documents(before, query):
    return {key: d for key, d in before["allowed"].items()
        if (query.document_ids is None or key in query.document_ids)
        and (query.document_versions is None or d["document_version"] == query.document_versions.get(key))}


def empty_answer(status, detail):
    return {"answer_status": status, "claims": [], "citations": [], "follow_up_questions": [],
        "fault_types": [], "checks": [], "part_decisions": [], "limitations": [detail]}


def live_data_answer(knowledge_base):
    result = empty_answer('requires_live_data', '该问题需要实时业务查询工具，当前知识问答无法确认；请在智能助手查询或查看库存总览。')
    if knowledge_base == 'general_diagnostics':
        result['limitations'].append('通用手册不能确认具体商品适配及每车用量；需先核实实际备件和需求数量，再判断车辆维修需求是否有足够库存。')
    return result


def finish_qualification(index, before, query):
    verify_qualification(before, index.sources, index.root, query.knowledge_base, query.knowledge_scope)


async def run_query(runtime, query, *, ask=False, search_question=None, conversation_context=None, user_evidence=None):
    stage = time.monotonic()
    read = partial(asyncio.to_thread, runtime.read)
    before = await read(qualification, query)
    metadata = {"generation_id": runtime.index.report["generation_id"], "knowledge_base": query.knowledge_base,
        "knowledge_scope": query.knowledge_scope, "retrieval_mode": query.retrieval_mode,
        "timings": {"qualification_seconds": round(time.monotonic()-stage, 3)}}
    limitations = ["本服务只读取文档，未查询实时库存或办理业务单据。"]
    if query.knowledge_scope == "draft_preview":
        limitations.append("当前使用项目编制或管理员审核的参考资料，不代表正式生效制度或厂家诊断结论。")
    if not selected_documents(before, query):
        await read(finish_qualification, before, query)
        result = empty_answer("no_eligible_documents", "所选范围内没有可用资料；可查看资料状态，或显式选择草稿预览。") if ask else {"status": "no_eligible_documents", "hits": [], "contexts": []}
        return metadata | result | {"unavailable_documents": before["unavailable"], "limitations": result.get("limitations", []) + limitations, "usage": {}, "embedding_requests": 0, "chat_requests": 0}
    if ask and requires_live_data(query.question):
        await read(finish_qualification, before, query)
        result = live_data_answer(query.knowledge_base)
        return metadata | result | {"limitations": result["limitations"] + limitations, "usage": {}, "embedding_requests": 0, "chat_requests": 0}
    stage = time.monotonic()
    vector, embedding_usage = await query_embedding(runtime.settings, search_question or query.question)
    metadata["timings"]["embedding_seconds"] = round(time.monotonic()-stage, 3)
    stage = time.monotonic()
    retrieval = await read(lambda index: index.search(search_question or query.question, query.knowledge_base, scope=query.knowledge_scope,
        top_k=query.top_k, mode=query.retrieval_mode, document_ids=query.document_ids,
        document_versions=query.document_versions, query_vector=vector))
    await read(finish_qualification, before, query)
    metadata["timings"]["retrieval_seconds"] = round(time.monotonic()-stage, 3)
    if not ask:
        return retrieval | {"timings": metadata["timings"], "usage": {"embedding": embedding_usage}, "chat_requests": 0}
    if retrieval["status"] != "retrieved":
        return metadata | empty_answer("insufficient_evidence", "未取得完整可用证据；请调整问题或文档范围。") | {"retrieval_status": retrieval["status"], "usage": {"embedding": embedding_usage}, "embedding_requests": 1, "chat_requests": 0}
    pool, types = await read(lambda index: (citation_pool(index, retrieval), part_types(index, retrieval)))
    stage = time.monotonic()
    content, chat_usage = await chat_json(runtime.settings, answer_messages(query.question, retrieval, pool, types, conversation_context=conversation_context))
    metadata["timings"]["chat_seconds"] = round(time.monotonic()-stage, 3)
    stage = time.monotonic()
    evidence_text = user_evidence if user_evidence is not None else "\n".join([m["content"] for m in conversation_context or [] if m["role"] == "user"] + [query.question])
    result = bind_answer(content, pool, types, evidence_text, query.knowledge_base)
    # Sources can be revoked while the remote model is running; discard the entire answer.
    await read(finish_qualification, before, query)
    metadata["timings"]["validation_seconds"] = round(time.monotonic()-stage, 3)
    return metadata | result | {"limitations": result["limitations"] + limitations,
        "usage": {"embedding": embedding_usage, "chat": chat_usage}, "embedding_requests": 1, "chat_requests": 1}


def error_response(error, request_id):
    code = error.code
    if code in {"knowledge_changed", "citation_outdated", "turn_conflict", "turn_in_progress"}:
        status = 409
    elif code in {"invalid_document_filter", "invalid_query", "search_query_invalid", "search_query_private"}:
        status = 422
    elif code.endswith("timeout"):
        status = 504
    elif code in {"answer_structure", "invalid_citation", "invalid_part_type", "embedding_response", "chat_response", "search_response"}:
        status = 502
    else:
        status = 503
    return JSONResponse(status_code=status, content={"detail": error.detail, "error_code": code, "request_id": request_id})


@router.get("/status")
def status(request: Request):
    runtime = request.app.state.knowledge
    result = {"available": runtime.index is not None, "embedding_configured": bool(runtime.settings.embedding_api_key.get_secret_value()),
        "chat_configured": bool(runtime.settings.api_key.get_secret_value() and runtime.settings.base_url and runtime.settings.model_id)}
    if runtime.index is None:
        return result | {"error_code": runtime.error.code if runtime.error else "index_unavailable", "detail": "知识索引未就绪，普通库存业务可继续使用。"}
    def summary(index):
        domains = {}
        for kb in CORPORA:
            released = qualify_sources(index.sources, index.root, kb, "released_only")
            draft = qualify_sources(index.sources, index.root, kb, "draft_preview")
            domains[kb] = {"released_documents": len(released["allowed"]), "preview_documents": len(draft["allowed"]), "domain_error": draft["domain_error"]}
        return {"generation_id": index.report["generation_id"], "document_count": len(index.sources["documents"]),
            "unit_count": len(index.units), "context_count": len(index.contexts), "domains": domains}
    return result | runtime.read(summary)


@router.get("/documents")
def documents(request: Request, knowledge_base: KnowledgeBase, knowledge_scope: KnowledgeScope = "released_only"):
    runtime = request.app.state.knowledge
    def listing(index):
        before = qualify_sources(index.sources, index.root, knowledge_base, knowledge_scope)
        reasons = {r["document_id"]: r["reason"] for r in before["unavailable"]}
        items = [{k: d[k] for k in ("document_id", "document_version", "title", "native_format", "source_state", "source_status", "parse_status")}
            | {"eligible": d["document_id"] in before["allowed"], "reason": reasons.get(d["document_id"]), "index_status": "indexed"}
            for d in index.sources["documents"] if d["knowledge_base"] == knowledge_base]
        verify_qualification(before, index.sources, index.root, knowledge_base, knowledge_scope)
        return {"items": items, "unavailable_documents": before["unavailable"]}
    try:
        return runtime.read(listing)
    except KnowledgeError as error:
        return error_response(error, uuid4().hex)


@router.post("/retrieve")
@router.post("/ask")
async def query_knowledge(request: Request, query: KnowledgeQuery, user=Depends(current_user)):
    request_id, started = uuid4().hex, time.monotonic()
    result = {}
    code = None
    try:
        async with asyncio.timeout(45):
            result = await run_query(request.app.state.knowledge, query, ask=request.url.path.endswith("/ask"))
        return result | {"request_id": request_id, "elapsed_seconds": round(time.monotonic()-started, 3)}
    except TimeoutError:
        code = "ask_timeout"
        return error_response(KnowledgeError(code, "知识请求超过45秒，请显式重试"), request_id)
    except KnowledgeError as error:
        code = error.code
        return error_response(error, request_id)
    finally:
        logger.info("knowledge_request %s", {"request_id": request_id, "user_id": user.id,
            "knowledge_base": query.knowledge_base, "scope": query.knowledge_scope,
            "generation_id": result.get("generation_id"), "chunks": [c["chunk_id"] for c in result.get("citations", [])],
            "elapsed_seconds": round(time.monotonic()-started, 3), "timings": result.get("timings", {}), "error_code": code, "usage": result.get("usage", {})})


@router.get("/chunks/{chunk_id}")
def chunk(request: Request, chunk_id: str, generation_id: str = Query(max_length=64),
        knowledge_base: KnowledgeBase = "inventory_policies", knowledge_scope: KnowledgeScope = "released_only"):
    runtime = request.app.state.knowledge
    def lookup(index):
        if generation_id != index.report["generation_id"]:
            raise KnowledgeError("citation_outdated", "引用所属索引已切换，请重新提问")
        before = qualify_sources(index.sources, index.root, knowledge_base, knowledge_scope)
        row = index.units.get(chunk_id) or index.contexts.get(chunk_id)
        if row is None or row["knowledge_base"] != knowledge_base or row["document_id"] not in before["allowed"]:
            raise KnowledgeError("citation_outdated", "引用来源已失效或不属于所选范围，请重新提问")
        if "unit_id" in row:
            retrieval = {"hits": [row], "contexts": [index.contexts[row["parent_id"]]]}
        else:
            retrieval = {"hits": [], "contexts": [row]}
        pool = citation_pool(index, retrieval)
        verify_qualification(before, index.sources, index.root, knowledge_base, knowledge_scope)
        return {"fragment": next(c for c in pool.values() if c["chunk_id"] == chunk_id),
            "context": next((c for c in pool.values() if c["unit_id"] is None), None)}
    try:
        return runtime.read(lookup)
    except KnowledgeError as error:
        return error_response(error, uuid4().hex)
