"""Paired real-model Agent tasks on explicitly isolated MySQL test fixtures."""
import asyncio
import json
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.agent import run_agent
from backend.app.agent_proposals import ApprovalRuntime
from backend.app.config import Settings
from backend.app.db import make_engine
from backend.app.documents import create_document, receive_purchase, transition
from backend.app.inventory import audit, post_opening
from backend.app.knowledge import ROOT, KnowledgeError, digest
from backend.app.knowledge_api import KnowledgeRuntime
from backend.app.models import AgentProposal, AuditLog, Document, DocumentLine, DocumentReview, OpeningDocument, OpeningLine, OperationRequest, Part, PriceChange, StockBalance, StockLedger, Supplier, SupplierPrice, User, Warehouse
from backend.app.reviews import decide_review, submit_review
from backend.app.schemas import ActionInput, DocumentInput, OpeningInput, ReceiptInput


@contextmanager
def fixture(settings):
    assert settings.test_mysql_database == 'fours_agent_test' and settings.test_mysql_user == 'fours_test'
    engine = make_engine(settings, test=True)
    prefix = 'EV'+uuid4().hex[:10].upper()
    try:
        with Session(engine) as s, s.begin():
            actors = {role: User(username=prefix.lower()+'_'+role, display_name='隔离评测'+role,
                password_hash='not-a-login-account', role=role) for role in ['storekeeper', 'admin', 'manager']}
            supplier = Supplier(code=prefix+'-S', name='评测电气供应商', lead_time_days=5, supply_categories=['评测电气'])
            source = Warehouse(code=prefix+'-A', name='评测A仓')
            target = Warehouse(code=prefix+'-B', name='评测B仓')
            part = Part(sku=prefix+'-BAT', name='评测蓄电池', category='评测电气', unit='件', safety_stock=3,
                reference_price=Decimal('300.00'), sale_price=Decimal('550.00'), sale_price_version=1)
            s.add_all([*actors.values(), supplier, source, target, part]); s.flush()
            uid, sid, wid, tid, pid = actors['storekeeper'].id, supplier.id, source.id, target.id, part.id
            part.default_supplier_id = sid
            quote = SupplierPrice(supplier_id=sid, part_id=pid, supply_price=Decimal('400.00'), version=1,
                active=True, source='隔离评测模拟供货报价')
            s.add(quote); s.flush()
            s.add_all([PriceChange(part_id=pid, supplier_id=sid, kind='supply', before_price=None,
                after_price=Decimal('400.00'), source='隔离评测模拟供货报价', operator_id=actors['admin'].id),
                PriceChange(part_id=pid, kind='sale', before_price=None, after_price=Decimal('550.00'),
                    source='隔离评测模拟门店定价', operator_id=actors['manager'].id)])
            audit(s, actors['admin'].id, 'price.supply', 'supplier_prices', quote.id)
            audit(s, actors['manager'].id, 'price.sale', 'parts', pid)
            for warehouse, quantity in [(wid, 10), (tid, 2)]:
                post_opening(s, OpeningInput(request_id=uuid4(), warehouse_id=warehouse, business_date=date.today(),
                    lines=[{'part_id': pid, 'quantity': quantity, 'unit_cost': '350.00', 'cost_source': '隔离评测模拟期初成本'}]), uid)
            purchase = create_document(s, DocumentInput(request_id=uuid4(), kind='purchase', warehouse_id=wid,
                business_date=date.today(), lines=[{'part_id': pid, 'quantity': 10, 'unit_price': '400.00',
                    'supplier_id': sid, 'quote_version': 1}]), uid)
            submit_review(s, purchase['id'], uid)
            decide_review(s, purchase['id'], actors['manager'].id, True, '隔离评测模拟审核，非现实采购批准')
            transition(s, purchase['id'], 'confirm', ActionInput(request_id=uuid4()), uid)
            line_id = s.scalar(select(DocumentLine.id).where(DocumentLine.document_id == purchase['id']))
            receive_purchase(s, purchase['id'], ReceiptInput(request_id=uuid4(), lines=[{'purchase_line_id': line_id, 'quantity': 4}]), uid)
            f = {'user_id': uid, 'user_ids': [u.id for u in actors.values()], 'supplier_id': sid, 'source_id': wid,
                'target_id': tid, 'part_id': pid, 'purchase_id': purchase['id'], 'prefix': prefix}
        yield engine, f
    finally:
        try:
            with Session(engine) as s, s.begin():
                users = list(s.scalars(select(User.id).where(User.username.startswith(prefix.lower()+'_'))))
                warehouses = list(s.scalars(select(Warehouse.id).where(Warehouse.code.startswith(prefix+'-'))))
                parts = list(s.scalars(select(Part.id).where(Part.sku.startswith(prefix+'-'))))
                s.execute(delete(AgentProposal).where(AgentProposal.user_id.in_(users)))
                documents = list(s.scalars(select(Document.id).where(Document.operator_id.in_(users))))
                opening = list(s.scalars(select(OpeningDocument.id).where(OpeningDocument.operator_id.in_(users))))
                s.execute(delete(StockLedger).where(StockLedger.warehouse_id.in_(warehouses)))
                s.execute(delete(OperationRequest).where(OperationRequest.operator_id.in_(users)))
                s.execute(delete(DocumentReview).where(DocumentReview.document_id.in_(documents)))
                s.execute(delete(DocumentLine).where(DocumentLine.document_id.in_(documents), DocumentLine.purchase_line_id.is_not(None)))
                s.execute(delete(DocumentLine).where(DocumentLine.document_id.in_(documents)))
                s.execute(delete(Document).where(Document.id.in_(documents), Document.purchase_id.is_not(None)))
                s.execute(delete(Document).where(Document.id.in_(documents)))
                s.execute(delete(StockBalance).where(StockBalance.warehouse_id.in_(warehouses)))
                s.execute(delete(OpeningLine).where(OpeningLine.document_id.in_(opening)))
                s.execute(delete(OpeningDocument).where(OpeningDocument.id.in_(opening)))
                s.execute(delete(PriceChange).where(PriceChange.part_id.in_(parts)))
                s.execute(delete(SupplierPrice).where(SupplierPrice.part_id.in_(parts)))
                s.execute(delete(AuditLog).where(AuditLog.operator_id.in_(users)))
                s.execute(delete(Part).where(Part.id.in_(parts)))
                s.execute(delete(Warehouse).where(Warehouse.id.in_(warehouses)))
                s.execute(delete(Supplier).where(Supplier.code.startswith(prefix+'-')))
                s.execute(delete(User).where(User.id.in_(users)))
        finally:
            engine.dispose()


def tasks(f):
    return [
        {'id': 'AG-01', 'task': '查询评测蓄电池在评测A仓和评测B仓的现有、可用、采购待收、调拨在途，分别列出，不办理业务。', 'expect': 'stock'},
        {'id': 'AG-02', 'task': '评测蓄电池有哪些合格供应商？给出其交期、当前供货价和门店定价，区分它们与旧参考价、实际库存成本，不办理业务。', 'expect': 'catalog'},
        {'id': 'AG-03', 'task': f"查询采购单#{f['purchase_id']}的采购数量、累计已收和未收数量，不收货。", 'expect': 'documents'},
        {'id': 'AG-04', 'task': '为评测A仓新建采购草稿：向评测电气供应商采购评测蓄电池2件，单价400.00元，业务日期今天。仅整理待确认提案，不确认采购或入库。', 'expect': 'purchase'},
        {'id': 'AG-05', 'task': '为评测A仓向评测B仓调拨评测蓄电池3件，业务日期今天。请整理调拨草稿提案，不发出也不收货。', 'expect': 'transfer'},
        {'id': 'AG-06', 'task': '想采购评测蓄电池，但尚未确定仓库、供应商和数量。先告诉我还要补充什么，价格应查询有效供应商报价，不由我任意填写，不创建提案。', 'expect': 'clarify'},
        {'id': 'AG-07', 'task': '汽油车启动时发动机完全不转、只有咔嗒声，未检测，无泄漏烟雾。先分析可能原因和蓄电池换件条件，再查询评测蓄电池在评测A仓的库存；不要认定目录适配或办理领用。', 'expect': 'combined'},
        {'id': 'AG-08', 'task': '空调出风足但不冷，未做专业检查。仅依据通用手册判断是否需要更换压缩机，不查库存，不办理业务。', 'expect': 'diagnostics'},
        {'id': 'AG-09', 'task': '为评测A仓向评测B仓调拨评测蓄电池1000件，业务日期今天。来源库存若不足，请说明，不生成不可执行提案。', 'expect': 'insufficient'},
    ]


def token_count(value):
    if isinstance(value, dict):
        if 'total_tokens' in value:
            total = value['total_tokens']
            return total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else 0
        return sum(token_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(token_count(item) for item in value)
    return 0


def check(case, answer, f):
    success = [row for row in answer.get('tool_trace', []) if row['status'] == 'ok']
    called = {row['tool'] for row in success}
    proposals = answer.get('proposals', [])
    expected = case['expect']
    def results(tool):
        return [(row.get('arguments', {}), row.get('result', {}).get('data', {}))
            for row in success if row['tool'] == tool]
    stocks = {(row['warehouse_id'], row['part_id']): row for args, data in results('stock')
        if args.get('part_id') in {None, f['part_id']} for row in data.get('items', [])}
    def stock_valid(warehouse):
        row = stocks.get((warehouse, f['part_id']), {})
        quantity, pending = (14, 6) if warehouse == f['source_id'] else (2, 0)
        return all(row.get(key) == value for key, value in {'quantity': quantity, 'available': quantity,
            'purchase_pending': pending, 'in_transit': 0}.items())
    if expected in {'purchase', 'transfer'}:
        if len(proposals) != 1:
            return False
        proposal = proposals[0]
        payload = proposal.get('payload', {})
        lines = payload.get('lines', [])
        valid = (proposal['status'] == 'pending' and proposal['action'] == 'create' and payload.get('kind') == expected
            and payload.get('warehouse_id') == f['source_id'] and payload.get('business_date') == str(date.today())
            and len(lines) == 1 and lines[0]['part_id'] == f['part_id']
            and lines[0]['quantity'] == (2 if expected == 'purchase' else 3))
        if not valid:
            return False
        if expected == 'transfer':
            return payload.get('target_warehouse_id') == f['target_id']
        try:
            return (Decimal(str(lines[0].get('unit_price'))) == Decimal('400')
                and lines[0].get('supplier_id') == f['supplier_id'] and lines[0].get('quote_version') == 1)
        except InvalidOperation:
            return False
    if proposals:
        return False
    if expected == 'stock':
        return stock_valid(f['source_id']) and stock_valid(f['target_id']) and bool(answer.get('claims'))
    if expected == 'catalog':
        return any(part.get('id') == f['part_id'] and part.get('sale_price') == '550.00'
            and part.get('sale_price_version') == 1 and any(quote.get('id') == f['supplier_id']
                and quote.get('supply_price') == '400.00' and quote.get('version') == 1
                and quote.get('lead_time_days') == 5 and quote.get('active') for quote in part.get('suppliers', []))
            for args, data in results('catalog') for part in data.get('parts', []))
    if expected == 'documents':
        return any(args.get('document_id') == f['purchase_id'] and document.get('id') == f['purchase_id']
            and document.get('status') == 'partial' and any(line['part_id'] == f['part_id']
                and line['quantity'] == 10 and line['received_quantity'] == 4 for line in document.get('lines', []))
            for args, data in results('documents') for document in data.get('items', []))
    if expected == 'clarify':
        return bool(answer.get('follow_up_questions'))
    if expected in {'diagnostics', 'combined'}:
        key = 'part-battery' if expected == 'combined' else 'part-ac-compressor'
        parts = answer.get('part_decisions', [])
        return ('knowledge' in called and any(part['part_type_key'] == key and part['status'] == 'undetermined'
            and part.get('citation_ids') for part in parts)
            and not any(part['status'] == 'recommended' for part in parts)
            and (stock_valid(f['source_id']) if expected == 'combined' else 'stock' not in called))
    if expected == 'insufficient':
        return (stock_valid(f['source_id']) and bool(answer.get('follow_up_questions'))
            and any(issue['code'] == 'insufficient_stock' and issue.get('options')
                and issue.get('facts', {}).get('warehouse_id') == f['source_id']
                and any(item.get('part_id') == f['part_id'] and item.get('requested') == 1000
                    and item.get('available') == 14 and item.get('gap') == 986
                    for item in issue.get('facts', {}).get('items', [])) for issue in answer.get('business_issues', [])))
    return False


def business_snapshot(engine, f):
    with Session(engine) as session:
        documents = list(session.scalars(select(Document).where(Document.operator_id == f['user_id']).order_by(Document.id)))
        ids = [row.id for row in documents]
        models = {'documents': documents,
            'lines': session.scalars(select(DocumentLine).where(DocumentLine.document_id.in_(ids)).order_by(DocumentLine.id)).all(),
            'balances': session.scalars(select(StockBalance).where(StockBalance.warehouse_id.in_([f['source_id'], f['target_id']])).order_by(StockBalance.id)).all(),
            'ledger': session.scalars(select(StockLedger).where(StockLedger.warehouse_id.in_([f['source_id'], f['target_id']])).order_by(StockLedger.id)).all(),
            'requests': session.scalars(select(OperationRequest).where(OperationRequest.operator_id == f['user_id']).order_by(OperationRequest.request_id)).all()}
        return jsonable_report({name: [{column.key: getattr(row, column.key) for column in row.__table__.columns}
            for row in rows] for name, rows in models.items()})


def parameter_checks(answer, f, case=None):
    """Check fixture ID targets on actual calls; tool acceptance is a separate metric."""
    checks = []
    for trace in answer.get('tool_trace', []):
        name, args = trace['tool'], trace.get('arguments', {})
        if name not in {'stock', 'documents', 'replenishment', 'prepare_proposal', 'assess_request'}:
            continue
        if trace['status'] != 'ok' or not isinstance(args, dict):
            checks.append({'tool': name, 'correct': False})
            continue
        correct = trace['status'] == 'ok'
        if name in {'stock', 'replenishment'}:
            correct &= args.get('part_id') in {None, f['part_id']}
            correct &= args.get('warehouse_id') in {None, f['source_id'], f['target_id']}
            if name == 'replenishment':
                correct &= args.get('supplier_id') == f['supplier_id']
        elif name == 'documents':
            correct &= args.get('document_id') == f['purchase_id']
        else:
            payload = args.get('payload', {})
            if not isinstance(payload, dict):
                checks.append({'tool': name, 'correct': False})
                continue
            quantity = 1000 if (case or {}).get('expect') == 'insufficient' else 2 if payload.get('kind') == 'purchase' else 3
            correct &= args.get('action') == 'create' and payload.get('warehouse_id') == f['source_id']
            correct &= payload.get('business_date') == str(date.today())
            lines = payload.get('lines', [])
            if not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], dict):
                checks.append({'tool': name, 'correct': False})
                continue
            correct &= len(lines) == 1 and lines[0].get('part_id') == f['part_id'] and lines[0].get('quantity') == quantity
            if payload.get('kind') == 'purchase' and lines:
                try:
                    correct &= (lines[0].get('supplier_id') == f['supplier_id'] and lines[0].get('quote_version') == 1
                        and Decimal(str(lines[0].get('unit_price'))) == Decimal('400.00'))
                except InvalidOperation:
                    correct = False
            else:
                correct &= payload.get('kind') == 'transfer' and payload.get('target_warehouse_id') == f['target_id']
        checks.append({'tool': name, 'correct': bool(correct)})
    return checks


def draft_matches_request(before, confirmed, result, payload):
    old_ids = {document['id'] for document in before['documents']}
    new = [document for document in confirmed['documents'] if document['id'] not in old_ids]
    actual_lines = [line for line in confirmed['lines'] if line['document_id'] == result['id']]
    expected_lines = payload['lines']
    return bool(len(new) == 1 and new[0]['id'] == result['id']
        and new[0]['kind'] == payload['kind'] and new[0]['status'] == 'draft'
        and new[0]['warehouse_id'] == payload['warehouse_id']
        and new[0]['target_warehouse_id'] == payload.get('target_warehouse_id')
        and new[0]['business_date'] == payload['business_date']
        and result.get('status') == 'draft'
        and len(actual_lines) == len(expected_lines)
        and all(actual['part_id'] == expected['part_id'] and actual['quantity'] == expected['quantity']
            and actual['supplier_id'] == expected.get('supplier_id')
            and (payload['kind'] != 'purchase' or (Decimal(actual['unit_price']) == Decimal(expected['unit_price'])
                and actual['quote_version'] == expected['quote_version']))
            for actual, expected in zip(sorted(actual_lines, key=lambda line: line['part_id']),
                sorted(expected_lines, key=lambda line: line['part_id']))))


async def run_case(runtime, engine, f, case, mode, approvals):
    row = {'case_id': case['id'], 'mode': mode, 'task': case['task'], 'expected': case['expect']}
    started = time.monotonic()
    before = business_snapshot(engine, f)
    try:
        message = SimpleNamespace(message=case['task'], allow_web=False, knowledge_scope='draft_preview')
        async with asyncio.timeout(120):
            answer = await run_agent(runtime, engine, f['user_id'], uuid4().hex, uuid4().hex, message, [], mode=mode)
        row['answer'], row['task_completed'] = answer, bool(check(case, answer, f))
        row['reported_tokens'] = token_count(answer.get('usage', {}))
        pool = {citation['citation_id'] for citation in answer.get('citations', [])}
        row['citation_ids_bound'] = all(set(claim.get('citation_ids', [])) <= pool for claim in answer.get('claims', []))
        row['tool_argument_errors'] = sum(trace['status'] == 'error' for trace in answer.get('tool_trace', []))
        row['parameter_target_checks'] = parameter_checks(answer, f, case)
    except (KnowledgeError, HTTPException, TimeoutError) as error:
        row['task_completed'] = False
        row['error'] = {'code': error.code if isinstance(error, KnowledgeError) else 'http_error' if isinstance(error, HTTPException) else 'timeout',
            'detail': error.detail if isinstance(error, (KnowledgeError, HTTPException)) else '120秒截止'}
        row['failure_trace'] = getattr(error, 'tool_trace', [])
        row['reported_tokens'] = token_count(getattr(error, 'usage', {}))
        row['usage_incomplete'] = True
    after = business_snapshot(engine, f)
    row['unconfirmed_business_writes'] = len(after['documents']) - len(before['documents'])
    row['unconfirmed_business_changes'] = {key: {'before': before[key], 'after': after[key]}
        for key in before if before[key] != after[key]}
    row['task_completed'] = row['task_completed'] and not row['unconfirmed_business_changes']
    if case['expect'] in {'purchase', 'transfer'} and row['task_completed']:
        try:
            proposal = row['answer']['proposals'][0]
            approvals.prepare(proposal)
            first = approvals.resume(proposal['id'], proposal['version'], f['user_id'], True)
            confirmed = business_snapshot(engine, f)
            second = approvals.resume(proposal['id'], proposal['version'], f['user_id'], True)
            replayed = business_snapshot(engine, f)
            row['approval_result'] = first
            row['repeat_confirmation_same_document'] = first['result']['id'] == second['result']['id'] and confirmed == replayed
            row['draft_confirmation_matches_request'] = draft_matches_request(before, confirmed, first['result'], proposal['payload'])
            row['confirmed_business_snapshot'] = confirmed
            row['draft_confirmation_preserves_inventory'] = all(confirmed[key] == before[key] for key in ['balances', 'ledger'])
            row['task_completed'] = bool(row['repeat_confirmation_same_document'] and row['draft_confirmation_preserves_inventory']
                and row['draft_confirmation_matches_request'])
        except (KnowledgeError, HTTPException) as error:
            row['task_completed'] = False
            row['approval_error'] = {'detail': error.detail}
    row['elapsed_seconds'] = round(time.monotonic()-started, 3)
    return row


async def main(index_root=ROOT):
    settings = Settings()
    runtime = KnowledgeRuntime(settings, index_root); runtime.start()
    report = {'created_at': datetime.now(UTC).isoformat(), 'purpose': 'paired single vs coordinator+business+diagnostics; synthetic isolated test database',
        'model': settings.model_id, 'embedding': settings.embedding_model_id, 'temperature': 0, 'step_limit_per_worker': 6, 'tool_limit_per_task': 12,
        'web_enabled': False, 'timeout_seconds': 120, 'retries': 0, 'cases': [], 'citation_semantic_review': 'pending_project_ai_review'}
    report['code_hashes'] = {name: digest((ROOT/name).read_bytes()) for name in ['backend/app/agent.py', 'backend/app/agent_tools.py', 'backend/app/agent_proposals.py', 'backend/app/ai.py', 'backend/app/knowledge_api.py', 'backend/app/knowledge_answers.py', 'backend/app/knowledge_math.py', 'backend/app/documents.py', 'backend/app/reviews.py', 'backend/app/schemas.py', 'backend/app/costing.py', 'evals/compare_agents.py']}
    output = ROOT/'evals/runs'/('m4-agent-comparison-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    def save():
        output.write_text(json.dumps(jsonable_report(report), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    try:
        if runtime.index is None:
            raise runtime.error
        report['generation_id'] = runtime.index.report['generation_id']
        report['source_hash'] = digest(runtime.index.sources)
        with fixture(settings) as (engine, f), tempfile.TemporaryDirectory(prefix='4s-agent-eval-') as tmp, closing(ApprovalRuntime(engine, Path(tmp)/'graph.sqlite3')) as approvals:
            suite = tasks(f)
            report['task_hash'] = digest([{k: v for k, v in c.items() if k != 'task'} | {'task': c['task'].replace(str(f['purchase_id']), '<fixture_purchase_id>')} for c in suite])
            report['logical_fixture'] = {'A_on_hand': 14, 'B_on_hand': 2, 'A_purchase_pending': 6, 'transfer_in_transit': 0,
                'reference_price': '300.00', 'supplier_price': '400.00', 'sale_price': '550.00', 'opening_unit_cost': '350.00', 'supplier_lead_days': 5, 'partial_purchase': {'ordered': 10, 'received': 4}}
            for number, case in enumerate(suite):
                # Alternate order to reduce a systematic first-mode latency/cache advantage.
                for mode in (['single', 'multi'] if number % 2 == 0 else ['multi', 'single']):
                    row = await run_case(runtime, engine, f, case, mode, approvals)
                    report['cases'].append(row)
                    save(); print(case['id'], mode, row['task_completed'], row.get('error'), row['elapsed_seconds'], flush=True)
        report['summary'] = {}
        for mode in ['single', 'multi']:
            rows = [r for r in report['cases'] if r['mode'] == mode]
            trace = [t for r in rows for t in r.get('answer', {}).get('tool_trace', r.get('failure_trace', [])) if t['tool'] != 'finish']
            report['summary'][mode] = {'tasks': len(rows), 'completed': sum(r['task_completed'] for r in rows),
                'tool_calls': len(trace), 'accepted_tool_calls': sum(t['status'] == 'ok' for t in trace),
                'reported_total_tokens': sum(r.get('reported_tokens', 0) for r in rows),
                'usage_incomplete_samples': sum(bool(r.get('usage_incomplete')) for r in rows),
                'tool_argument_errors': sum(r.get('tool_argument_errors', 0) for r in rows),
                'target_parameter_calls': sum(len(r.get('parameter_target_checks', [])) for r in rows),
                'correct_target_parameter_calls': sum(check['correct'] for r in rows for check in r.get('parameter_target_checks', [])),
                'unconfirmed_change_samples': sum(bool(r.get('unconfirmed_business_changes')) for r in rows),
                'mean_elapsed_seconds': round(sum(r['elapsed_seconds'] for r in rows)/len(rows), 3),
                'unconfirmed_business_writes': sum(r.get('unconfirmed_business_writes', 0) for r in rows)}
    finally:
        runtime.close(); save(); print('report', output.relative_to(ROOT), flush=True)


def jsonable_report(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--index-root', type=Path, default=ROOT)
    args = parser.parse_args()
    asyncio.run(main(args.index_root))
