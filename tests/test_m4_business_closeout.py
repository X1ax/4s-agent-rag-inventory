"""Deterministic model calls, real isolated MySQL: a complete M4 business chain."""
import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent import run_agent
from backend.app.agent_proposals import ApprovalRuntime, decide, make_proposal
from backend.app.assistant_api import ChatMessage
from backend.app.business_issues import BusinessIssue
from backend.app.models import AgentProposal, Document, OperationRequest, Part, StockBalance, StockLedger
from test_agent import actor, issue_payload
from test_m1 import resources, system
from test_m2 import call, detail
from test_pricing_workflow import quote, sale

pytestmark = pytest.mark.mysql


def scripted_turn(system, monkeypatch, runtime, thread_id, text, calls):
    """Only model selection is scripted; tool validation, evidence and writes are real."""
    pending = iter(calls)

    async def dispatch(settings, messages, **kwargs):
        return json.dumps({'route': 'business', 'task': text}), {'total_tokens': 1}

    async def native(settings, messages, tools):
        names = {tool['function']['name'] for tool in tools}
        assert 'prepare_proposal' in names and 'execute' not in names
        next_call = next(pending, None)
        if next_call is None:
            evidence = [json.loads(message['content'])['evidence_id'] for message in messages
                if message['role'] == 'tool' and 'evidence_id' in json.loads(message['content'])]
            name, arguments = 'finish', {'claims': [{'text': '已取得本轮业务条件；待办理动作仍需人工确认。', 'evidence_ids': evidence[-1:]}]}
        else:
            name, arguments = next_call
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': uuid4().hex, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}, {'total_tokens': 1}

    with monkeypatch.context() as patch:
        patch.setattr('backend.app.agent.chat_json', dispatch)
        patch.setattr('backend.app.agent.chat_tools', native)
        result = asyncio.run(run_agent(runtime, system[2], actor(system), thread_id, uuid4().hex,
            ChatMessage(message=text, client_request_id=uuid4().hex, allow_web=False), []))
    assert result['roles'] == ['coordinator', 'business']
    assert len(result['tool_trace']) == len(calls) + 1
    return result


def test_shortage_human_choice_review_receipt_issue_profit_and_recovery(system, tmp_path, monkeypatch):
    engine, uid, today = system[2], actor(system), str(date.today())
    source, parts = resources(system, 1)
    target, _ = resources(system, 0)
    part = parts[0]
    supplier = call(system, '/api/suppliers', {'code': system[3]+'-CLOSE', 'name': '闭环模拟供应商',
        'lead_time_days': 3}, role='admin', expected=201)
    quote(system, supplier, part, '120.00')
    sale(system, part)
    with Session(engine) as session, session.begin():
        session.get(Part, part['id']).safety_stock = 6
    for warehouse, quantity in [(source, 9), (target, 1)]:
        call(system, '/api/inventory/openings', {'request_id': str(uuid4()), 'warehouse_id': warehouse['id'],
            'business_date': today, 'lines': [{'part_id': part['id'], 'quantity': quantity,
                'unit_cost': '100.00', 'cost_source': '闭环模拟期初凭证'}]})

    # Explicit synthetic complete zero-demand calendar; no production history or manifest is changed.
    manifest = tmp_path/'data/catalog/random-history-manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'consumption_from': str(date.today()-timedelta(days=30)),
        'consumption_to': str(date.today()-timedelta(days=1)), 'plan': [{'part_id': part['id'], 'sku': part['sku'],
            'warehouse_id': warehouse['id'], 'warehouse_code': warehouse['code']} for warehouse in [source, target]]}), encoding='utf-8')
    runtime = SimpleNamespace(root=tmp_path, settings=system[4])
    thread_id = uuid4().hex

    def turn(text, calls):
        return scripted_turn(system, monkeypatch, runtime, thread_id, text, calls)

    def propose(action, payload, text):
        result = turn(text, [('assess_request', {'action': action, 'payload': payload}),
            ('prepare_proposal', {'action': action, 'payload': payload, 'instruction_quote': text})])
        assert all(row['status'] == 'ok' for row in result['tool_trace'])
        assert len(result['proposals']) == 1
        return result['proposals'][0]

    def execute(action, payload, text):
        proposal = propose(action, payload, text)
        result = decide(engine, proposal['id'], 1, uid, True)
        assert decide(engine, proposal['id'], 1, uid, True)['result'] == result['result']
        return result['result']

    def state():
        with Session(engine) as session:
            balances = {row.warehouse_id: (row.quantity, row.stock_value) for row in session.scalars(
                select(StockBalance).where(StockBalance.part_id == part['id']))}
            documents = session.scalar(select(func.count()).select_from(Document).where(Document.operator_id == uid))
            ledgers = session.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.part_id == part['id']))
            operations = session.scalar(select(func.count()).select_from(OperationRequest).where(OperationRequest.operator_id == uid))
            return balances, documents, ledgers, operations

    purchase_payload = {'kind': 'purchase', 'warehouse_id': target['id'], 'business_date': today,
        'lines': [{'part_id': part['id'], 'quantity': 2, 'supplier_id': supplier['id'], 'unit_price': '120.00', 'quote_version': 1}]}
    requested = '请为目标仓办理该备件6件收费维修领用。'
    before = state()
    shortage = turn(requested, [('catalog', {'part_id': part['id']}),
        ('stock', {'part_id': part['id'], 'warehouse_id': target['id']}),
        ('assess_request', {'action': 'create', 'payload': issue_payload(target, part, 6)}),
        ('prepare_proposal', {'action': 'create', 'payload': purchase_payload, 'instruction_quote': requested})])
    issue = shortage['business_issues'][0]
    assert shortage['answer_status'] == 'needs_clarification' and not shortage['proposals']
    assert issue['code'] == 'insufficient_stock'
    assert {key: issue['facts']['items'][0][key] for key in ['requested', 'available', 'gap']} == {
        'requested': 6, 'available': 1, 'gap': 5}
    assert {row['label'] for row in issue['options']} >= {'核查跨仓调拨', '准备缺口采购'}
    assert shortage['tool_trace'][3]['status'] == 'error' and '员工选择' in shortage['tool_trace'][3]['error']
    assert state() == before

    calculation = turn('先核查跨仓可调条件及剩余采购缺口，不创建单据。', [('replenishment', {
        'part_id': part['id'], 'warehouse_id': target['id'], 'supplier_id': supplier['id']})])['tool_trace'][0]['result']['data']
    assert (calculation['target'], calculation['position'], calculation['purchase_gap']) == (6, 1, 2)
    assert calculation['transfers'] == [{'source_warehouse_id': source['id'], 'target_warehouse_id': target['id'],
        'part_id': part['id'], 'quantity': 3}]
    transfer_payload = {'kind': 'transfer', 'warehouse_id': source['id'], 'target_warehouse_id': target['id'],
        'business_date': today, 'lines': [{'part_id': part['id'], 'quantity': 3}]}
    rejected = propose('create', transfer_payload, '我选择调拨3件，请先准备调拨草稿提案。')
    assert decide(engine, rejected['id'], 1, uid, False)['status'] == 'rejected'
    assert decide(engine, rejected['id'], 1, uid, True)['status'] == 'rejected'
    assert state() == before
    proposal = propose('create', transfer_payload, '确认重新整理3件调拨草稿提案。')
    for other_role, status in [('manager', 403), ('admin', 404)]:
        with pytest.raises(HTTPException) as caught:
            decide(engine, proposal['id'], 1, actor(system, other_role), True)
        assert caught.value.status_code == status
    transfer = decide(engine, proposal['id'], 1, uid, True)['result']
    assert decide(engine, proposal['id'], 1, uid, True)['result'] == transfer
    assert state()[0] == before[0] and state()[1:] == (1, 2, 1)

    call(system, f"/api/reviews/documents/{transfer['id']}/submit", {})
    call(system, f"/api/reviews/documents/{transfer['id']}/decision", {'approved': True}, expected=403)
    call(system, f"/api/reviews/documents/{transfer['id']}/decision", {'approved': False, 'note': '请补充需求依据'}, role='manager')
    with pytest.raises(HTTPException, match='审核'):
        make_proposal(engine, uid, thread_id, uuid4().hex, 'send', {'document_id': transfer['id'], 'business_date': today})
    execute('cancel', {'document_id': transfer['id'], 'business_date': today}, '按审核意见取消被驳回的调拨草稿。')
    transfer = execute('create', transfer_payload | {'note': '已补充6件维修需求，先调3件再采购2件'}, '按补充依据重建3件调拨草稿。')
    call(system, f"/api/reviews/documents/{transfer['id']}/submit", {})
    call(system, f"/api/reviews/documents/{transfer['id']}/decision", {'approved': True}, role='manager')
    assert detail(system, transfer['id'])['status'] == 'draft' and state()[0] == before[0]

    purchase = propose('create', purchase_payload, '我选择该供应商采购剩余2件，请准备采购草稿提案。')
    quote(system, supplier, part, '125.00', 1)
    no_price_write = state()
    with pytest.raises(BusinessIssue):
        decide(engine, purchase['id'], 1, uid, True)
    assert state() == no_price_write
    purchase_payload['lines'][0].update(unit_price='125.00', quote_version=2)
    order = execute('create', purchase_payload, '按该供应商最新125元有效报价重建2件采购草稿。')
    call(system, f"/api/reviews/documents/{order['id']}/submit", {})
    call(system, f"/api/reviews/documents/{order['id']}/decision", {'approved': True}, role='manager')
    assert detail(system, order['id'])['status'] == 'draft' and state()[0] == before[0]
    execute('confirm', {'document_id': order['id'], 'business_date': today}, '业务审核已通过，请准备采购确认提案。')
    assert state()[0] == before[0]
    pending_send = propose('send', {'document_id': transfer['id'], 'business_date': today}, '请准备发出已审核的3件调拨。')

    quote(system, supplier, part, '140.00', 2)
    line = detail(system, order['id'])['lines'][0]
    # A concurrent manual receipt uses the same service without superseding this chat's pending proposal.
    call(system, f"/api/documents/{order['id']}/receipts", {'request_id': str(uuid4()), 'business_date': today,
        'lines': [{'purchase_line_id': line['id'], 'quantity': 2}]}, expected=201)
    assert detail(system, order['id'])['status'] == 'completed'
    assert state()[0][target['id']] == (3, Decimal('350.00'))  # Frozen 125, not the current 140 quote.
    after_receipt = state()
    with pytest.raises(BusinessIssue) as caught:
        decide(engine, pending_send['id'], 1, uid, True)
    assert caught.value.issue['code'] == 'proposal_stale' and state() == after_receipt

    sending = propose('send', {'document_id': transfer['id'], 'business_date': today}, '已重新核对采购到货，仍发出已审核的3件调拨。')
    from backend.app import documents
    original_finish = documents.finish

    def fail_after_stock_flush(session, document, payload, action, operator_id):
        result = original_finish(session, document, payload, action, operator_id)
        if action == f"transfer.send:{transfer['id']}":
            raise RuntimeError('simulated failure after quantity, value, ledger and request flush')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(documents, 'finish', fail_after_stock_flush)
        with pytest.raises(RuntimeError, match='after quantity'):
            decide(engine, sending['id'], 1, uid, True)
    assert state() == after_receipt
    with Session(engine) as session:
        row = session.get(AgentProposal, sending['id'])
        assert row.status == 'pending' and row.result is None

    path = tmp_path/'approval.sqlite3'
    approvals = ApprovalRuntime(engine, path)
    try:
        approvals.prepare(sending)

        def crash_after_commit(*args, **kwargs):
            decide(*args, **kwargs)
            raise RuntimeError('simulated crash after MySQL commit')

        with monkeypatch.context() as patch:
            patch.setattr('backend.app.agent_proposals.decide', crash_after_commit)
            with pytest.raises(RuntimeError, match='after MySQL commit'):
                approvals.resume(sending['id'], 1, uid, True)
    finally:
        approvals.close()
    sent_state = state()
    restored = ApprovalRuntime(engine, path)
    try:
        assert restored.resume(sending['id'], 1, uid, True)['status'] == 'executed'
        assert restored.resume(sending['id'], 1, uid, True)['status'] == 'executed'
    finally:
        restored.close()
    assert state() == sent_state
    transfer_line = detail(system, transfer['id'])['lines'][0]
    assert transfer_line['cost_amount'] == '300.00'
    transit = turn('核查两仓现有与调拨在途。', [('stock', {'part_id': part['id']})])['tool_trace'][0]['result']['data']['items']
    assert sum(row['quantity']+row['in_transit'] for row in transit) == 12
    assert sum(value for _, value in sent_state[0].values()) + Decimal(transfer_line['cost_amount']) == Decimal('1250.00')
    execute('receive', {'document_id': transfer['id'], 'business_date': today}, '已核对调拨3件全部实到，请准备整单收货。')
    assert state()[0] == {source['id']: (6, Decimal('600.00')), target['id']: (6, Decimal('650.00'))}

    paid = execute('create', issue_payload(target, part, 5), '维修用途已核准为收费5件，请准备收费领用草稿。')
    sale(system, part, '160.00', 1)
    execute('post', {'document_id': paid['id'], 'business_date': today}, '核对收费草稿后请准备过账5件。')
    warranty_payload = issue_payload(target, part) | {'issue_type': 'warranty', 'warranty_reason': '本次核准质保更换1件'}
    warranty = execute('create', warranty_payload, '另1件是核准的质保维修，请准备质保领用草稿。')
    execute('post', {'document_id': warranty['id'], 'business_date': today}, '核对质保草稿后请准备过账1件。')
    assert detail(system, paid['id'])['lines'][0]['sale_unit_price'] == '150.00'
    assert detail(system, warranty['id'])['lines'][0]['sale_unit_price'] == '0.00'
    assert state()[0] == {source['id']: (6, Decimal('600.00')), target['id']: (0, Decimal('0.00'))}
    final_state = state()
    profit = turn('查询今天该备件的收费毛利和质保成本。', [('profits', {'part_id': part['id'],
        'date_from': today, 'date_to': today})])['tool_trace'][0]['result']['data']
    assert {key: profit['totals'][key] for key in ['revenue', 'paid_cost', 'gross_profit', 'warranty_cost', 'contribution']} == {
        'revenue': '750.00', 'paid_cost': '541.67', 'gross_profit': '208.33', 'warranty_cost': '108.33', 'contribution': '100.00'}
    assert (profit['totals']['quantity'], profit['totals']['missing_lines'], profit['details_total']) == (6, 0, 2)
    assert state() == final_state
    with Session(engine) as session:
        ledger = list(session.scalars(select(StockLedger).where(StockLedger.part_id == part['id'])))
        assert sum(row.quantity_delta for row in ledger) == 6
        assert sum(row.value_delta for row in ledger) == Decimal('600.00')
        assert sum(row.value_delta for row in ledger if row.event in {'opening', 'receipt'}) == Decimal('1250.00')
        assert -sum(row.value_delta for row in ledger if row.event == 'issue') == Decimal('650.00')
        assert all(row.value_delta is not None for row in ledger)
        assert session.scalar(select(func.count()).select_from(StockLedger).where(
            StockLedger.document_line_id == transfer_line['id'], StockLedger.event == 'transfer_send')) == 1
