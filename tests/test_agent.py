import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent import worker, run_agent
from backend.app.agent_proposals import ApprovalRuntime, decide, make_proposal
from backend.app.agent_tools import StockArgs, ToolContext, query_tool
from backend.app.documents import create_document, transition
from backend.app.knowledge import KnowledgeError
from backend.app.models import AgentProposal, Document, DocumentLine, OperationRequest, Part, StockBalance, StockLedger, SupplierPrice, User, utcnow
from backend.app.schemas import ActionInput, DocumentInput
from test_m1 import system, resources as base_resources, opening_body
from test_m2 import call

pytestmark = pytest.mark.mysql


def resources(system, count=2):
    warehouse, parts = base_resources(system, count)
    with Session(system[2]) as session, session.begin():
        for part in parts:
            row = session.get(Part, part['id'])
            row.sale_price, row.sale_price_version = Decimal('20.00'), 1
    return warehouse, parts


def actor(system, role='storekeeper'):
    with Session(system[2]) as s:
        return s.scalar(select(User.id).where(User.username == system[3].lower()+'_'+role))


def issue_payload(warehouse, part, quantity=1):
    return {'kind': 'issue', 'warehouse_id': warehouse['id'], 'business_date': str(date.today()), 'issue_type': 'paid_repair',
        'lines': [{'part_id': part['id'], 'quantity': quantity}]}


def make(system, payload, **extra):
    return make_proposal(system[2], actor(system), extra.pop('thread_id', uuid4().hex), uuid4().hex,
        extra.pop('action', 'create'), payload)


def test_no_write_before_approval_and_concurrent_repeated_confirmation(system, tmp_path):
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    engine, uid = system[2], actor(system)
    with Session(engine) as s:
        before = s.scalar(select(func.count()).select_from(Document))
    proposal = make(system, issue_payload(warehouse, parts[0]))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Document)) == before
        assert s.scalar(select(StockBalance.quantity).where(StockBalance.warehouse_id == warehouse['id'])) == 3
    runtime = ApprovalRuntime(engine, tmp_path/'checkpoints.sqlite3')
    runtime.prepare(proposal)
    config = {'configurable': {'thread_id': proposal['id']}}
    assert runtime.graph.get_state(config).next == ('review',)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.resume(proposal['id'], 1, uid, True), range(2)))
    runtime.close()
    assert results[0]['result']['id'] == results[1]['result']['id']
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Document)) == before+1
        assert s.scalar(select(StockBalance.quantity).where(StockBalance.warehouse_id == warehouse['id'])) == 3
    post = make(system, {'document_id': results[0]['result']['id'], 'business_date': str(date.today())}, action='post')
    out = decide(engine, post['id'], 1, uid, True)
    assert decide(engine, post['id'], 1, uid, True)['result'] == out['result']
    with Session(engine) as s:
        assert s.scalar(select(StockBalance.quantity).where(StockBalance.warehouse_id == warehouse['id'])) == 2


def test_reject_supersede_version_acl_expiry_and_stale_stock(system):
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    engine, uid = system[2], actor(system)
    tid = uuid4().hex
    old = make(system, issue_payload(warehouse, parts[0]), thread_id=tid)
    new = make(system, issue_payload(warehouse, parts[0], 2), thread_id=tid)
    for proposal_id, version, user in [(old['id'], 1, uid), (new['id'], 2, uid), (new['id'], 1, actor(system, 'manager')), (new['id'], 1, actor(system, 'admin'))]:
        with pytest.raises(HTTPException):
            decide(engine, proposal_id, version, user, True)
    assert decide(engine, new['id'], 1, uid, False)['status'] == 'rejected'
    assert decide(engine, new['id'], 1, uid, True)['status'] == 'rejected'
    stale = make(system, issue_payload(warehouse, parts[0]))
    with Session(engine) as s, s.begin():
        created = create_document(s, DocumentInput.model_validate(issue_payload(warehouse, parts[0]) | {'request_id': str(uuid4())}), uid)
        transition(s, created['id'], 'post', ActionInput(request_id=uuid4()), uid)
    with pytest.raises(HTTPException, match='已变化'):
        decide(engine, stale['id'], 1, uid, True)
    expired = make(system, issue_payload(warehouse, parts[0]))
    with Session(engine) as s, s.begin():
        s.get(AgentProposal, expired['id']).expires_at = utcnow()-timedelta(seconds=1)
    with pytest.raises(HTTPException, match='过期'):
        decide(engine, expired['id'], 1, uid, True)


def test_restart_after_commit_before_checkpoint_does_not_duplicate(system, tmp_path, monkeypatch):
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    engine, uid = system[2], actor(system)
    proposal = make(system, issue_payload(warehouse, parts[0]))
    path = tmp_path/'persistent.sqlite3'
    runtime = ApprovalRuntime(engine, path)
    runtime.prepare(proposal)
    def crash(*args, **kwargs):
        decide(*args, **kwargs)
        raise RuntimeError('simulated crash after MySQL commit before graph checkpoint')
    with monkeypatch.context() as patch:
        patch.setattr('backend.app.agent_proposals.decide', crash)
        with pytest.raises(RuntimeError, match='simulated crash'):
            runtime.resume(proposal['id'], 1, uid, True)
    runtime.close()
    restored = ApprovalRuntime(engine, path)
    result = restored.resume(proposal['id'], 1, uid, True)
    restored.close()
    assert result['status'] == 'executed'
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OperationRequest).where(OperationRequest.document_id == result['result']['id'])) == 1


def test_proposal_routes_require_thread_owner_and_writer(system):
    client, tokens, engine, *_ = system
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    tid = client.post('/api/assistant/threads', headers=tokens['storekeeper']).json()['id']
    proposal = make(system, issue_payload(warehouse, parts[0]), thread_id=tid)
    path = f"/api/assistant/threads/{tid}/proposals/{proposal['id']}/decision"
    assert client.post(path, headers=tokens['manager'], json={'version': 1, 'approved': True}).status_code == 404
    assert client.post(path, headers=tokens['storekeeper'], json={'version': 1, 'approved': 'true'}).status_code == 422
    assert client.post(path, headers=tokens['storekeeper'], json={'version': 1, 'approved': True, 'quantity': 50}).status_code == 422
    assert client.post(path, headers=tokens['storekeeper'], json={'version': 1, 'approved': False}).json()['status'] == 'rejected'
    second = make(system, issue_payload(warehouse, parts[0]), thread_id=tid)
    path = f"/api/assistant/threads/{tid}/proposals/{second['id']}/decision"
    response = client.post(path, headers=tokens['storekeeper'], json={'version': 1, 'approved': True})
    assert response.status_code == 200 and response.json()['status'] == 'executed'
    repeated = client.post(path, headers=tokens['storekeeper'], json={'version': 1, 'approved': True})
    assert repeated.json()['result']['id'] == response.json()['result']['id']
    client.delete(f'/api/assistant/threads/{tid}', headers=tokens['storekeeper'])


def test_tools_validate_real_ids_and_worker_native_evidence_binding(system, monkeypatch):
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, '查询库存', allow_web=False)
    seen = []
    async def native(settings, messages, tools):
        seen.append(tools)
        if len(seen) == 1:
            name, args = 'stock', {'part_id': parts[0]['id'], 'warehouse_id': warehouse['id']}
        else:
            result = json.loads(messages[-1]['content'])
            name, args = 'finish', {'claims': [{'text': '可用库存3件', 'evidence_ids': [result['evidence_id']]}]}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(len(seen)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}, {'total_tokens': 2}
    monkeypatch.setattr('backend.app.agent.chat_tools', native)
    result = asyncio.run(worker(ctx, '查询库存', 'business'))
    assert result['claims'][0]['citation_ids'][0] == result['citations'][0]['citation_id']
    assert '"available": 3' in result['citations'][0]['excerpt']
    with Session(system[2]) as s:
        rows = query_tool(s, 'stock', StockArgs(part_id=parts[0]['id'], warehouse_id=warehouse['id']))['items']
        assert rows[0]['quantity'] == 3
    assert all(t['function']['name'] != 'execute' for t in seen[0])


def test_diagnostic_agent_has_no_write_or_stock_tool(system, monkeypatch):
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, '症状不明', allow_web=False)
    async def native(settings, messages, tools):
        assert {t['function']['name'] for t in tools} == {'knowledge', 'finish'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': '1', 'type': 'function', 'function': {'name': 'finish', 'arguments': json.dumps({'follow_up_questions': ['具体是什么症状？']})}}]}, {'total_tokens': 1}
    monkeypatch.setattr('backend.app.agent.chat_tools', native)
    assert asyncio.run(worker(ctx, '症状不明', 'diagnostics'))['follow_up_questions']


def test_handoff_accepts_only_server_carried_citations(system, monkeypatch):
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, '查询库存')
    prior = {'citations': [{'citation_id': 'server-dx', 'document_id': 'DX-STR-002'}]}
    async def native(settings, messages, tools):
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': '1', 'type': 'function', 'function': {'name': 'finish',
            'arguments': json.dumps({'claims': [{'text': '已在前一专员给出故障证据', 'evidence_ids': ['server-dx']}]})}}]}, {'total_tokens': 1}
    monkeypatch.setattr('backend.app.agent.chat_tools', native)
    assert asyncio.run(worker(ctx, '库存子任务', 'business', prior=prior))['claims'] == []  # Prior answer is merged once by coordinator.
    with pytest.raises(KnowledgeError, match='未取得'):
        asyncio.run(worker(ctx, '库存子任务', 'business'))


def test_worker_accepts_actual_nested_rag_citation_without_duplicate_rewrite(system, monkeypatch):
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, '仅问故障')
    bound = {'answer_status': 'answered', 'claims': [{'text': '保留服务端检索回答', 'citation_ids': ['real-rag']}],
        'citations': [{'citation_id': 'real-rag', 'document_id': 'DX-STR-002'}]}
    async def call(name, args, role):
        ctx.knowledge_answers.append(bound)
        return bound
    monkeypatch.setattr(ctx, 'call', call)
    calls = []
    async def native(settings, messages, tools):
        calls.append(1)
        name, args = ('knowledge', {'question': '启动故障', 'knowledge_base': 'general_diagnostics'}) if len(calls) == 1 else ('finish', {'claims': [{'text': '不要重复改写的故障总结', 'evidence_ids': ['real-rag']}]})
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(len(calls)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}, {'total_tokens': 1}
    monkeypatch.setattr('backend.app.agent.chat_tools', native)
    result = asyncio.run(worker(ctx, '仅问故障', 'diagnostics'))
    assert result['claims'] == bound['claims'] and result['citations'] == bound['citations']


def test_knowledge_rewrite_cannot_supply_fake_test_record(system, monkeypatch):
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    original = '发动机不转，只有咔嗒声，未做检测；再查询库存'
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, original)
    async def query(runtime, query, **kwargs):
        assert kwargs['user_evidence'] == original
        assert kwargs['conversation_context'] == [{'role': 'user', 'content': original}]
        assert query.question != original
        return {'answer_status': 'needs_clarification'}
    monkeypatch.setattr('backend.app.agent_tools.run_query', query)
    asyncio.run(ctx.call('knowledge', {'question': '只分析启动故障子任务', 'knowledge_base': 'general_diagnostics'}, 'diagnostics'))
    with pytest.raises(HTTPException):
        asyncio.run(ctx.call('stock', {}, 'diagnostics'))
    with pytest.raises(HTTPException):
        asyncio.run(ctx.call('knowledge', {'question': '库存制度', 'knowledge_base': 'inventory_policies'}, 'diagnostics'))


def test_purchase_partial_receipt_and_transfer_conservation_through_approval(system):
    engine, uid = system[2], actor(system)
    source, parts = resources(system, 1)
    target, _ = resources(system, 0)
    supplier = call(system, '/api/suppliers', {'code': system[3]+'-AG-S', 'name': 'Agent测试供应商'}, role='admin', expected=201)
    with Session(engine) as session, session.begin():
        session.add(SupplierPrice(supplier_id=supplier['id'], part_id=parts[0]['id'], supply_price=Decimal('10.20'), version=1, active=True, source='测试报价'))
    def execute(action, payload):
        p = make(system, payload, action=action)
        return decide(engine, p['id'], 1, uid, True)['result']
    order = execute('create', {'kind': 'purchase', 'warehouse_id': source['id'], 'business_date': str(date.today()),
        'lines': [{'part_id': parts[0]['id'], 'quantity': 10, 'supplier_id': supplier['id'], 'unit_price': '10.20'}]})
    from test_m2 import approve_document
    approve_document(system, order['id'])
    execute('confirm', {'document_id': order['id'], 'business_date': str(date.today())})
    with Session(engine) as s:
        line_id = s.scalar(select(DocumentLine.id).where(DocumentLine.document_id == order['id']))
    execute('receipt', {'document_id': order['id'], 'business_date': str(date.today()), 'lines': [{'purchase_line_id': line_id, 'quantity': 4}]})
    with Session(engine) as s:
        assert s.get(Document, order['id']).status == 'partial'
    transfer = execute('create', {'kind': 'transfer', 'warehouse_id': source['id'], 'target_warehouse_id': target['id'],
        'business_date': str(date.today()), 'lines': [{'part_id': parts[0]['id'], 'quantity': 3}]})
    approve_document(system, transfer['id'])
    execute('send', {'document_id': transfer['id'], 'business_date': str(date.today())})
    with Session(engine) as s:
        rows = query_tool(s, 'stock', StockArgs(part_id=parts[0]['id']))['items']
        assert sum(r['quantity']+r['in_transit'] for r in rows if r['warehouse_id'] in [source['id'], target['id']]) == 4
    execute('receive', {'document_id': transfer['id'], 'business_date': str(date.today())})
    with Session(engine) as s:
        assert s.scalar(select(func.sum(StockBalance.quantity)).where(StockBalance.part_id == parts[0]['id'])) == 4


def test_replenishment_does_not_offer_same_source_stock_twice(system, monkeypatch):
    warehouse, parts = resources(system, 1)
    a, _ = resources(system, 0)
    b, _ = resources(system, 0)
    part = parts[0]
    call(system, '/api/inventory/openings', opening_body(warehouse, parts) | {'lines': [{'part_id': part['id'], 'quantity': 15, 'unit_cost':'12.30','cost_source':'测试期初'}]})
    supplier = call(system, '/api/suppliers', {'code': system[3]+'-RP-S', 'name': '补货测试供应商'}, role='admin', expected=201)
    with Session(system[2]) as s, s.begin():
        s.get(Part, part['id']).safety_stock = 10
    monkeypatch.setattr('backend.app.agent_tools.complete_history', lambda *args: True)  # Known complete synthetic zero-demand calendar.
    class Runtime:
        root = Path(__file__).resolve().parents[1]
        settings = system[4]
    ctx = ToolContext(Runtime(), system[2], actor(system), uuid4().hex, uuid4().hex, '分析补货')
    async def calculate():
        first = await ctx.call('replenishment', {'part_id': part['id'], 'warehouse_id': a['id'], 'supplier_id': supplier['id']}, 'business')
        second = await ctx.call('replenishment', {'part_id': part['id'], 'warehouse_id': b['id'], 'supplier_id': supplier['id']}, 'business')
        repeated = await ctx.call('replenishment', {'part_id': part['id'], 'warehouse_id': a['id'], 'supplier_id': supplier['id']}, 'business')
        return first, second, repeated
    first, second, repeated = asyncio.run(calculate())
    assert sum(r['quantity'] for r in first['transfers']+second['transfers']) == 5
    assert second['purchase_gap'] == 10 and first == repeated
