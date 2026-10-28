"""API/output boundaries with real local Chroma and mocked HTTP, not quality scores."""
import asyncio
import json
import shutil
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.ai import chat_json, query_embedding
from backend.app.config import Settings
from backend.app.knowledge import CORPORA, ROOT, KnowledgeError
from backend.app.knowledge_answers import bind_answer, requires_live_data
from backend.app.knowledge_math import CalculationInputError, calculate_example
from backend.app.main import create_app
from backend.app.retrieval import build_index, index_owner
from backend.app.security import current_user
from test_retrieval import FakeEmbedding
from test_m1 import system


@pytest.fixture(scope="module")
def app_system(tmp_path_factory):
    root = tmp_path_factory.mktemp("answer-index")
    for relative, _ in CORPORA.values():
        shutil.copytree(ROOT / relative, root / relative)
    settings = Settings(_env_file=None, mysql_user="", api_key="mock-chat-only", base_url="https://example.org/v1",
        model_id="mock-model", embedding_api_key="mock-embedding-only")
    build_index(settings, root, embedder=FakeEmbedding(settings))
    application = create_app(settings, knowledge_root=root)
    application.dependency_overrides[current_user] = lambda: SimpleNamespace(id=42, role="manager")
    with TestClient(application) as client:
        yield application, client, root, settings


@pytest.fixture
def mocked_ai(monkeypatch):
    calls = {"embedding": 0, "chat": 0}
    async def embedding(settings, question):
        calls["embedding"] += 1
        return [1.0] + [0.0]*1023, {"total_tokens": 5}
    async def chat(settings, messages):
        calls["chat"] += 1
        payload = json.loads(messages[1]["content"])
        evidence = payload["evidence"][0]
        return json.dumps({"answer_status": "answered", "claims": [{"text": "待核准稿的规则须结合原文核对。", "citation_ids": [evidence["citation_id"]]}]}), {"total_tokens": 8}
    monkeypatch.setattr("backend.app.knowledge_api.query_embedding", embedding)
    monkeypatch.setattr("backend.app.knowledge_api.chat_json", chat)
    return calls


def request_body(**updates):
    return {"question": "调拨未收货时如何处理？", "knowledge_base": "inventory_policies", "knowledge_scope": "draft_preview", **updates}


def test_default_empty_is_free_and_draft_requires_explicit_selection(app_system, mocked_ai):
    app, client, _, _ = app_system
    found = client.get("/api/knowledge/status").json()
    assert found["available"] and found["unit_count"] == 242
    assert found["domains"]["inventory_policies"] == {"released_documents": 0, "preview_documents": 12, "domain_error": None}
    result = client.post("/api/knowledge/ask", json=request_body(knowledge_scope="released_only")).json()
    assert result["answer_status"] == "no_eligible_documents" and not result["citations"]
    assert mocked_ai == {"embedding": 0, "chat": 0}
    assert "密钥" not in str(found) and "mock-chat-only" not in str(found)
    with pytest.raises(KnowledgeError, match="其他进程"):
        with index_owner(app.state.knowledge.root):
            pass


def test_authenticated_answer_source_expansion_generation_and_scope_guards(app_system, mocked_ai):
    _, client, _, _ = app_system
    response = client.post("/api/knowledge/ask", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer_status"] == "answered" and result["citations"]
    assert mocked_ai == {"embedding": 1, "chat": 1}
    citation = result["citations"][0]
    assert citation["source_state"] == "draft" and "excerpt" in citation and "path" not in citation
    parameters = {"generation_id": result["generation_id"], "knowledge_scope": "draft_preview"}
    url = "/api/knowledge/chunks/" + citation["chunk_id"]
    detail = client.get(url, params=parameters)
    assert detail.status_code == 200 and detail.json()["fragment"]["excerpt"] == citation["excerpt"]
    for options in [{**parameters, "generation_id": "old"}, {**parameters, "knowledge_scope": "released_only"}, {**parameters, "knowledge_base": "general_diagnostics"}]:
        response = client.get(url, params=options)
        assert response.status_code == 409 and response.json()["error_code"] == "citation_outdated"


@pytest.mark.parametrize("updates", [{"user_id": 2}, {"top_k": True}, {"question": " "}, {"knowledge_scope": "all"}, {"document_ids": ["../outside"]}])
def test_bad_inputs_never_call_models(app_system, mocked_ai, updates):
    _, client, _, _ = app_system
    response = client.post("/api/knowledge/ask", json=request_body(**updates))
    assert response.status_code == 422 and mocked_ai == {"embedding": 0, "chat": 0}


def test_source_revoked_while_model_runs_discards_entire_answer(app_system, mocked_ai, monkeypatch):
    _, client, root, _ = app_system
    source = root / "data/knowledge/inventory/policies/INV-ADJ-008.md"
    original = source.read_bytes()
    async def change_during_generation(settings, messages):
        payload = json.loads(messages[1]["content"])
        source.write_bytes(original + b"\nchanged during generation")
        return json.dumps({"answer_status": "answered", "claims": [{"text": "不能返回这个答案", "citation_ids": [payload["evidence"][0]["citation_id"]]}]}), {}
    monkeypatch.setattr("backend.app.knowledge_api.chat_json", change_during_generation)
    try:
        response = client.post("/api/knowledge/ask", json=request_body())
        assert response.status_code == 409 and response.json()["error_code"] == "knowledge_changed"
        assert "不能返回这个答案" not in response.text
    finally:
        source.write_bytes(original)


def test_missing_index_does_not_block_business_and_missing_auth_is_rejected(tmp_path):
    app = create_app(Settings(_env_file=None), knowledge_root=tmp_path)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/knowledge/status").json()["available"] is False
        response = client.post("/api/knowledge/ask", json=request_body())
        assert response.status_code == 503 and response.json()["error_code"] == "index_not_built"


def test_live_inventory_is_not_inferred_from_documents(app_system, mocked_ai):
    _, client, _, _ = app_system
    result = client.post("/api/knowledge/ask", json=request_body(question="目前主仓机油滤清器有多少件？")).json()
    assert result["answer_status"] == "requires_live_data" and result["claims"] == []
    assert mocked_ai == {"embedding": 0, "chat": 0}
    assert not requires_live_data("可用库存怎么计算？")


def test_diagnostic_live_stock_boundary_also_explains_fitment_and_vehicle_quantity(app_system, mocked_ai):
    _, client, _, _ = app_system
    result = client.post('/api/knowledge/ask', json=request_body(
        question='依据手册告诉我现在仓库有多少块电瓶，够不够换三辆车？不要查询系统。',
        knowledge_base='general_diagnostics')).json()
    assert result['answer_status'] == 'requires_live_data' and not result['claims']
    assert any('适配' in note and '用量' in note for note in result['limitations'])
    assert mocked_ai == {'embedding': 0, 'chat': 0}


@pytest.mark.parametrize("raw,code", [("not JSON", "answer_structure"), ('{"answer_status":"answered","claims":[]}', "answer_structure"), ('{"answer_status":"answered","claims":[{"text":"假的","citation_ids":["fake-generation"]}]}', "invalid_citation")])
def test_invalid_model_outputs_are_not_displayed(app_system, mocked_ai, monkeypatch, raw, code):
    async def malformed(*args, **kwargs):
        return raw, {}
    monkeypatch.setattr("backend.app.knowledge_api.chat_json", malformed)
    response = app_system[1].post("/api/knowledge/ask", json=request_body())
    assert response.status_code == 502 and response.json()["error_code"] == code


def test_native_format_locators_and_contexts(app_system, mocked_ai):
    _, client, _, _ = app_system
    for doc_id, locator in [("INV-RCV-004", "page_start"), ("INV-TRF-006", "block_start")]:
        result = client.post("/api/knowledge/ask", json=request_body(document_ids=[doc_id])).json()
        citation = result["citations"][0]
        assert citation["document_id"] == doc_id and locator in citation
        if locator != "page_start":
            assert "page_start" not in citation


def pool_for_unit():
    return {"c1": {"excerpt": "目标库存 库存位置 完整自然日 在途 可调", "chunk_id": "unit", "unit_id": "DX-STR-01"}}


def test_diagnostic_type_whitelist_and_recommendation_record_requirements():
    pool = pool_for_unit()
    types = {"part-battery": {"catalog_sku": "x", "catalog_name": "蓄电池", "unit": "件", "fitment_status": "not_assessed", "required_quantity": None, "case_ids": ["DX-STR-01"]}}
    base = {"answer_status": "answered", "part_decisions": [{"text": "建议更换", "citation_ids": ["c1"], "part_type_key": "part-battery", "status": "recommended", "condition": "检查确认不合格"}]}
    result = bind_answer(json.dumps(base), pool, types, "只有咔哒声，没有检测", "general_diagnostics")
    assert result["part_decisions"][0]["status"] == "undetermined" and result["part_decisions"][0]["text"] != "建议更换"
    row = base["part_decisions"][0]
    row["check_record"] = {"quote": "检测记录：负载测试结果不合格，判据为原车维修要求。", "test": "负载测试", "observed": "不合格", "criterion": "原车维修要求"}
    result = bind_answer(json.dumps(base), pool, types, row["check_record"]["quote"], "general_diagnostics")
    assert result["part_decisions"][0]["status"] == "recommended"
    assert any("真实性" in s for s in result["limitations"])
    row["part_type_key"] = "invented-part"
    with pytest.raises(KnowledgeError, match="受控映射"):
        bind_answer(json.dumps(base), pool, types, "症状", "general_diagnostics")


def quoted(value, quote):
    return {"value": value, "quote": quote}


def calculation(kind, inputs):
    return {"kind": kind, "hypothesis_quote": "假设", "inputs": inputs, "citation_ids": ["c1"]}


def test_four_hypothetical_calculations_exact_results_and_question_provenance():
    inputs = {"issue_30d": quoted(61, "30个完整自然日已过账领用61件"), "days": quoted(30, "30个完整自然日"), "lead_days": quoted(5, "交期5天"),
        "safety": quoted(10, "安全库存10件"), "available": quoted(4, "可用4件"), "purchase_pending": quoted(8, "采购待收8件"), "transfer_inbound": quoted(3, "调拨在途3件")}
    question = "假设，" + "；".join(v["quote"] for v in inputs.values())
    spec = calculation("replenishment", inputs)
    result = calculate_example(spec, question)
    assert result["result"] == {"daily_consumption": "61/30", "target": 21, "position": 15, "gap": 6}
    bound = bind_answer(json.dumps({"answer_status": "calculate_example", "calculation": spec}), pool_for_unit(), {}, question, "inventory_policies")
    assert bound["calculation"]["result"]["gap"] == 6
    with pytest.raises(CalculationInputError):
        calculate_example(spec, "假设，没有给这些参数")
    inputs["days"] = quoted(20, "20天")
    with pytest.raises(CalculationInputError, match="30"):
        calculate_example(spec, question + "20天")
    transfer = {"source": quoted(12, "来源仓12件"), "target": quoted(7, "目标仓7件"), "quantity": quoted(5, "调拨5件")}
    result = calculate_example(calculation("transfer_conservation", transfer), "假设来源仓12件，目标仓7件，调拨5件")
    assert result["result"] == {"after_ship": {"source": 7, "target": 7, "in_transit": 5}, "after_receive": {"source": 7, "target": 12, "in_transit": 0}, "total": 19}
    window = {"baseline": quoted("2026-03-01", "基准日2026-03-01"), "days": quoted(30, "30天")}
    result = calculate_example(calculation("complete_day_window", window), "假设基准日2026-03-01，向前30天")
    assert result["result"] == {"start": "2026-01-30", "end": "2026-02-28", "count": 30}
    order = "可调余量降序、同余量编码升序、按需求顺序"
    allocation = {"sources": [{"code": quoted("A", "A仓"), "available": quoted(10, "A仓可用10件"), "target": quoted(4, "A仓目标4件")}],
        "demands": [{"code": quoted("B", "B仓"), "gap": quoted(4, "B仓缺口4件")}, {"code": quoted("C", "C仓"), "gap": quoted(4, "C仓缺口4件")}], "order_quote": order}
    question = "假设A仓可用10件，A仓目标4件，B仓缺口4件，C仓缺口4件，" + order
    result = calculate_example(calculation("allocation", allocation), question)
    assert result["result"] == {"allocations": [{"source": "A", "target": "B", "quantity": 4}, {"source": "A", "target": "C", "quantity": 2}], "purchase_gaps": {"B": 0, "C": 2}}


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "tools", "truncated"])
def test_async_ai_protocol_errors_are_sanitized_and_never_retried(failure):
    calls = []
    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-upstream-address", request=request)
        if failure == "http":
            return httpx.Response(429, text="private-upstream-body")
        if failure == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if failure == "truncated" else "stop", "message": {"content": "{}", "tool_calls": [1] if failure == "tools" else []}}]})
    settings = Settings(_env_file=None, api_key="mock", base_url="https://example.org/v1", model_id="mock")
    with pytest.raises(KnowledgeError) as error:
        asyncio.run(chat_json(settings, [], transport=httpx.MockTransport(respond)))
    assert len(calls) == 1 and "private-upstream" not in str(error.value)


def test_async_embedding_uses_dedicated_key_and_validates_dimension():
    def respond(request):
        assert request.headers["Authorization"] == "Bearer embedding-only"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] + [0.0]*1023}], "usage": {"total_tokens": 9}})
    settings = Settings(_env_file=None, embedding_api_key="embedding-only", api_key="chat-only")
    vector, usage = asyncio.run(query_embedding(settings, "查询", transport=httpx.MockTransport(respond)))
    assert len(vector) == 1024 and usage == {"total_tokens": 9}


def test_overall_deadline_cancels_inflight_embedding_and_returns_explicit_error(app_system, monkeypatch):
    cancelled = []
    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    real_timeout = asyncio.timeout
    monkeypatch.setattr("backend.app.knowledge_api.query_embedding", slow)
    monkeypatch.setattr("backend.app.knowledge_api.asyncio.timeout", lambda seconds: real_timeout(.5 if seconds == 45 else seconds))
    response = app_system[1].post("/api/knowledge/ask", json=request_body())
    assert response.status_code == 504 and response.json()["error_code"] == "ask_timeout"
    assert cancelled == [True]


def test_reference_is_rejected_after_source_changes(app_system, mocked_ai):
    _, client, root, _ = app_system
    answer = client.post("/api/knowledge/ask", json=request_body(document_ids=["INV-ADJ-008"])).json()
    citation = answer["citations"][0]
    source = root / "data/knowledge/inventory/policies/INV-ADJ-008.md"
    original = source.read_bytes()
    try:
        source.write_bytes(original + b"\nrevoked source")
        response = client.get("/api/knowledge/chunks/" + citation["chunk_id"], params={"generation_id": answer["generation_id"], "knowledge_scope": "draft_preview"})
        assert response.status_code == 409 and response.json()["error_code"] == "citation_outdated"
    finally:
        source.write_bytes(original)


def test_status_and_retrieval_are_readonly_and_do_not_expose_paths(app_system, mocked_ai):
    _, client, _, _ = app_system
    assert client.post("/api/knowledge/build").status_code == 404
    response = client.post("/api/knowledge/retrieve", json=request_body(document_ids=["INV-TRF-006"]))
    assert response.status_code == 200 and response.json()["hits"]
    assert mocked_ai == {"embedding": 1, "chat": 0}
    assert "D:\\" not in response.text and "mock-chat-only" not in response.text


def test_conflicts_and_followups_must_have_reviewable_evidence():
    pool = pool_for_unit()
    with pytest.raises(KnowledgeError, match="冲突"):
        bind_answer(json.dumps({"answer_status": "conflict", "claims": [{"text": "冲突", "citation_ids": ["c1"]}]}), pool, {}, "规则", "inventory_policies")
    with pytest.raises(KnowledgeError, match="补充问题"):
        bind_answer('{"answer_status":"needs_clarification"}', pool, {}, "规则", "inventory_policies")
    result = bind_answer('{"answer_status":"needs_clarification","follow_up_questions":["是否已经实际收货？"]}', pool, {}, "规则", "inventory_policies")
    assert result["answer_status"] == "needs_clarification"


@pytest.mark.parametrize("bad", [True, -1, 1.5, "61", 10**10])
def test_calculation_rejects_non_integer_negative_or_unbounded_quantities(bad):
    spec = calculation("transfer_conservation", {"source": quoted(bad, "来源仓61件"), "target": quoted(7, "目标仓7件"), "quantity": quoted(5, "调拨5件")})
    with pytest.raises(CalculationInputError):
        calculate_example(spec, "假设来源仓61件，目标仓7件，调拨5件")


@pytest.mark.mysql
def test_real_mysql_authentication_all_roles_and_no_business_writes(system):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from backend.app.models import Document, StockBalance, StockLedger, OperationRequest
    client, tokens, engine, _, _ = system
    def counts():
        with Session(engine) as session:
            return [session.scalar(select(func.count()).select_from(model)) for model in (Document, StockBalance, StockLedger, OperationRequest)]
    before = counts()
    assert client.get("/api/knowledge/status").status_code == 401
    assert client.get("/api/knowledge/status", headers={"Authorization": "Bearer invalid"}).status_code == 401
    for headers in tokens.values():
        state = client.get("/api/knowledge/status", headers=headers)
        assert state.status_code == 200
        response = client.post("/api/knowledge/ask", headers=headers, json=request_body(knowledge_scope="released_only"))
        if state.json()["available"]:
            assert response.status_code == 200 and response.json()["answer_status"] == "no_eligible_documents"
        else:
            assert response.status_code == 503 and response.json()["error_code"] == "index_in_use"
    assert counts() == before


def test_deepseek_official_endpoint_uses_non_thinking_json_mode():
    def respond(request):
        payload = json.loads(request.content)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert payload["thinking"] == {"type": "disabled"} and payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})
    settings = Settings(_env_file=None, api_key="mock", base_url="https://api.deepseek.com", model_id="deepseek-flash")
    result, _ = asyncio.run(chat_json(settings, [], transport=httpx.MockTransport(respond)))
    assert result == '{"ok":true}'
