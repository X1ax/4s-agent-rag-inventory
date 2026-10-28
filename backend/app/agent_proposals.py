"""One approval boundary shared by every Agent. Models never call execution."""
import sqlite3
import threading
import json
from datetime import timedelta
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.documents import create_document, lock_catalogs, priced_lines, receive_purchase, transition
from backend.app.knowledge import digest
from backend.app.models import AgentProposal, Document, DocumentLine, Part, Supplier, SupplierPrice, User, Warehouse, utcnow
from backend.app.queries import balance_statement
from backend.app.schemas import ActionInput, DocumentInput, ReceiptInput
from backend.app.business_issues import BusinessIssue, shortage
from backend.app.reviews import require_review


def writer(user):
    if user is None or not user.active or user.role not in {'admin', 'storekeeper'}:
        raise HTTPException(403, '当前账号没有业务办理权限')


def normalize(action, payload, request_id):
    values = dict(payload) | {'request_id': request_id}
    if action == 'create':
        parsed = DocumentInput.model_validate(values)
    elif action == 'receipt':
        parsed = ReceiptInput.model_validate({k: v for k, v in values.items() if k != 'document_id'})
    elif action in {'confirm', 'post', 'send', 'receive', 'cancel'}:
        parsed = ActionInput.model_validate({k: v for k, v in values.items() if k != 'document_id'})
    else:
        raise HTTPException(422, '不支持该业务动作')
    if action != 'create' and (type(payload.get('document_id')) is not int or payload['document_id'] <= 0):
        raise HTTPException(422, '必须指定有效单据编号')
    return parsed


def snapshot(session, action, payload, *, lock=False):
    parsed = normalize(action, payload, str(uuid4()))
    document = None
    if action == 'create':
        warehouse_ids = [parsed.warehouse_id] + ([parsed.target_warehouse_id] if parsed.target_warehouse_id else [])
        part_ids = [r.part_id for r in parsed.lines]
        supplier_ids = {r.supplier_id or parsed.supplier_id for r in parsed.lines} if parsed.kind == 'purchase' else set()
        lines = parsed.model_dump(mode='json')['lines']
        kind = parsed.kind
    else:
        statement = select(Document).where(Document.id == payload['document_id'])
        document = session.scalar(statement.with_for_update() if lock else statement)
        if document is None:
            raise HTTPException(404, '单据不存在')
        rows = list(session.scalars(select(DocumentLine).where(DocumentLine.document_id == document.id).order_by(DocumentLine.id)))
        warehouse_ids = [document.warehouse_id] + ([document.target_warehouse_id] if document.target_warehouse_id else [])
        part_ids, supplier_ids = [r.part_id for r in rows], {r.supplier_id for r in rows if r.supplier_id}
        lines = [{'line_id': r.id, 'part_id': r.part_id, 'quantity': r.quantity, 'received_quantity': r.received_quantity,
            'supplier_id': r.supplier_id, 'unit_price': str(r.unit_price), 'quote_version': r.quote_version,
            'sale_unit_price': str(r.sale_unit_price) if r.sale_unit_price is not None else None,
            'sale_price_version': r.sale_price_version, 'cost_amount': str(r.cost_amount) if r.cost_amount is not None else None} for r in rows]
        kind = document.kind
        states = {('purchase', 'confirm'): {'draft'}, ('issue', 'post'): {'draft'}, ('transfer', 'send'): {'draft'},
            ('transfer', 'receive'): {'sent'}, ('purchase', 'receipt'): {'confirmed', 'partial'}}
        allowed = {'draft', 'confirmed', 'partial'} if kind == 'purchase' and action == 'cancel' else {'draft'} if action == 'cancel' and kind in {'issue', 'transfer'} else states.get((kind, action), set())
        if document.status not in allowed:
            raise BusinessIssue('state_conflict', '当前单据状态不支持该操作', facts={'document_id': document.id, 'kind': kind, 'status': document.status, 'action': action}, options=[
                ('核查单据与关联记录', f'请查询单据#{document.id}的当前状态、明细和关联收货记录，说明允许的后续操作，不自动改状态。'),
                ('核查异常处理方式', f'请依据制度解释单据#{document.id}当前状态下的异常处理路径，不撤销已生效库存、不删除流水。')])
        if action in {'confirm','send'}:
            require_review(session, document)
        if action == 'receipt':
            by_id = {r.id: r for r in rows}
            if any(r.purchase_line_id not in by_id or r.quantity > by_id[r.purchase_line_id].quantity - by_id[r.purchase_line_id].received_quantity for r in parsed.lines):
                raise BusinessIssue('receipt_mismatch', '收货明细不属于本单或超过采购未收数量', facts={'document_id': document.id,
                    'lines': [{'line_id': r.id, 'part_id': r.part_id, 'ordered': r.quantity, 'received': r.received_quantity, 'remaining': r.quantity-r.received_quantity} for r in rows]}, options=[
                    ('核对实收与采购余量', f'请核对采购单#{document.id}的已收、未收及本批合格实收数量，等我确认后再整理入库提案。'),
                    ('处理超送或异常到货', '请说明超送、破损或待检到货应如何登记异常和核对采购依据；不要借用其他采购余量或将异常件先计可用库存。')])
    if lock:
        lock_catalogs(session, warehouse_ids, part_ids, supplier_ids)
    parts = list(session.scalars(select(Part).where(Part.id.in_(part_ids)).order_by(Part.id).execution_options(populate_existing=True)))
    warehouses = list(session.scalars(select(Warehouse).where(Warehouse.id.in_(warehouse_ids)).order_by(Warehouse.id).execution_options(populate_existing=True)))
    suppliers = list(session.scalars(select(Supplier).where(Supplier.id.in_(supplier_ids)).order_by(Supplier.id).execution_options(populate_existing=True)))
    if len(parts) != len(set(part_ids)) or len(warehouses) != len(set(warehouse_ids)) or len(suppliers) != len(supplier_ids) or any(not r.active for r in parts+warehouses+suppliers):
        raise BusinessIssue('catalog_unavailable', '提案涉及不存在或停用的资料', options=[
            ('核对有效资料', '请重新查询该业务的启用备件、仓库与合格供应商；无法确定对应资料时向我确认，不自行替换。'),
            ('联系资料维护人员', '请列出需要管理员核对的资料问题；不要自动启用、替换或修改基础资料。')])
    by_part, by_supplier = {r.id: r for r in parts}, {r.id: r for r in suppliers}
    if kind == 'purchase' and (action == 'create' or action == 'confirm'):
        for line in lines:
            supplier_id = line['supplier_id'] or payload.get('supplier_id')
            if supplier_id not in by_supplier or by_part[line['part_id']].category not in by_supplier[supplier_id].supply_categories:
                raise BusinessIssue('supplier_not_qualified', '所选供应商不具备该备件的供货范围', facts={'part_id': line['part_id'], 'supplier_id': supplier_id}, options=[
                    ('查询合格供应商', f"请查询备件ID {line['part_id']}的合格供应商、交期和参考信息，等我选择供应商及成交价后重新整理提案。"),
                    ('核对供应范围资料', '请列出需要管理员核查的供货范围依据，不自动扩大供应商的供货资格。')])
    balances = [dict(r) for r in session.execute(balance_statement().where(Warehouse.id.in_(warehouse_ids), Part.id.in_(part_ids)).order_by(Warehouse.id, Part.id)).mappings()]
    for row in balances:
        for key in ('quantity', 'available', 'purchase_pending', 'in_transit'):
            row[key] = int(row[key])
        row['stock_value'] = str(row['stock_value']) if row['stock_value'] is not None else None
    source_id = parsed.warehouse_id if action == 'create' else document.warehouse_id
    if (action == 'create' and kind in {'issue', 'transfer'}) or action in {'post', 'send'}:
        shortage(lines, balances, source_id, by_part, action)
    if action == 'create':
        try:
            lines = json.loads(json.dumps(priced_lines(session, parsed), default=str))
        except HTTPException as error:
            raise BusinessIssue('pricing_unavailable', error.detail, options=[
                ('核对供货价与门店定价', '请查询所选备件的供应商有效报价和门店定价，列出需由管理员或经理维护的缺失项，不自动修改价格。'),
                ('刷新价格后重新办理', '请重新查询最新价格和版本，在我确认用途及供应商后再整理业务提案。')]) from error
    if action == 'post' and (document.issue_type not in {'paid_repair','warranty'} or any(r.sale_unit_price is None for r in rows)):
        raise BusinessIssue('pricing_unavailable', '旧出库草稿缺少用途或结算价格，请取消后重新制单')
    quotes = list(session.scalars(select(SupplierPrice).where(SupplierPrice.part_id.in_(part_ids), SupplierPrice.supplier_id.in_(supplier_ids)).order_by(SupplierPrice.supplier_id, SupplierPrice.part_id)))
    return {'action': action, 'kind': kind, 'document_id': document.id if document else None,
        'document_status': document.status if document else None, 'lines': lines,
        'issue_type': parsed.issue_type if action == 'create' else document.issue_type,
        'warranty_reason': parsed.warranty_reason if action == 'create' else document.warranty_reason,
        'work_order': parsed.work_order if action == 'create' else document.work_order,
        'quotes': [{'part_id': r.part_id, 'supplier_id': r.supplier_id, 'supply_price': str(r.supply_price), 'version': r.version, 'active': r.active} for r in quotes],
        'parts': [{'id': r.id, 'sku': r.sku, 'name': r.name, 'unit': r.unit, 'specification': r.specification,
            'category': r.category, 'safety_stock': r.safety_stock, 'reference_price': str(r.reference_price),
            'sale_price': str(r.sale_price) if r.sale_price is not None else None, 'sale_price_version': r.sale_price_version} for r in parts],
        'warehouses': [{'id': r.id, 'name': r.name, 'code': r.code} for r in warehouses],
        'suppliers': [{'id': r.id, 'name': r.name, 'lead_time_days': r.lead_time_days, 'supply_categories': r.supply_categories} for r in suppliers],
        'balances': balances}


def view(row):
    return {'id': row.id, 'turn_id': row.turn_id, 'version': row.version, 'action': row.action, 'payload': row.payload,
        'summary': row.summary, 'status': row.status, 'result': row.result, 'expires_at': row.expires_at.isoformat()+'Z'}


def make_proposal(engine, user_id, thread_id, turn_id, action, payload):
    with Session(engine) as session, session.begin():
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        writer(user)
        previous = session.scalar(select(AgentProposal).where(AgentProposal.thread_id == thread_id, AgentProposal.turn_id == turn_id))
        if previous:
            if previous.user_id != user_id or previous.action != action or previous.payload != payload:
                raise HTTPException(409, '本轮已有不同提案，请在新一轮修改')
            return view(previous)
        summary = snapshot(session, action, payload, lock=True)
        for old in session.scalars(select(AgentProposal).where(AgentProposal.user_id == user_id, AgentProposal.thread_id == thread_id, AgentProposal.status == 'pending').with_for_update()):
            old.status = 'superseded'
        row = AgentProposal(id=str(uuid4()), user_id=user_id, thread_id=thread_id, turn_id=turn_id, version=1,
            action=action, payload=payload, summary=summary, snapshot_hash=digest(summary), status='pending', expires_at=utcnow()+timedelta(minutes=30))
        session.add(row)
        session.flush()
        return view(row)


def decide(engine, proposal_id, version, user_id, approved):
    with Session(engine) as session, session.begin():
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        writer(user)
        row = session.scalar(select(AgentProposal).where(AgentProposal.id == proposal_id, AgentProposal.user_id == user_id).with_for_update())
        if row is None:
            raise HTTPException(404, '提案不存在或无权访问')
        if row.version != version:
            raise HTTPException(409, '提案版本已变化，请重新核对')
        if row.status in {'executed', 'rejected'}:
            return view(row)
        if row.status != 'pending' or row.expires_at < utcnow():
            raise BusinessIssue('proposal_expired', '提案已过期或失效，请重新生成', options=[('重新核对并整理提案', '请重新查询刚才业务所需的实时数据，按原需求重新整理待审核提案，不沿用已过期的确认。')])
        if not approved:
            row.status = 'rejected'
            return view(row)
        summary = snapshot(session, row.action, row.payload, lock=True)
        if digest(summary) != row.snapshot_hash:
            raise BusinessIssue('proposal_stale', '库存、单据或资料已变化，请重新生成提案后确认', options=[('重新核对业务数据', '请重新核对刚才提案涉及的库存、单据与供应范围，说明变化后重新整理提案，不静默修改原参数。')])
        key = str(uuid5(NAMESPACE_URL, f'4s-agent:{row.id}:{row.version}'))
        parsed = normalize(row.action, row.payload, key)
        if row.action == 'create':
            result = create_document(session, parsed, user_id)
        elif row.action == 'receipt':
            result = receive_purchase(session, row.payload['document_id'], parsed, user_id)
        else:
            result = transition(session, row.payload['document_id'], row.action, parsed, user_id)
        row.status, row.result = 'executed', result
        session.flush()
        return view(row)


class ApprovalState(TypedDict, total=False):
    proposal_id: str
    version: int
    user_id: int
    approved: bool
    result: dict


class ApprovalRuntime:
    def __init__(self, engine, path):
        self.engine = engine
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()  # ponytail: one local graph writer, no distributed worker in this edition.
        saver = SqliteSaver(self.connection)
        graph = StateGraph(ApprovalState)
        def review(state):
            response = interrupt({'proposal_id': state['proposal_id'], 'version': state['version'], 'requires_human': True})
            if not isinstance(response, dict) or type(response.get('approved')) is not bool or response.get('user_id') != state['user_id']:
                raise HTTPException(403, '确认身份或格式不匹配')
            return {'approved': response['approved']}
        def execute(state):
            return {'result': decide(self.engine, state['proposal_id'], state['version'], state['user_id'], state['approved'])}
        graph.add_node('review', review)
        graph.add_node('execute', execute)
        graph.add_edge(START, 'review')
        graph.add_edge('review', 'execute')
        graph.add_edge('execute', END)
        self.graph = graph.compile(checkpointer=saver)

    def resume(self, proposal_id, version, user_id, approved):
        # Check server ownership before even loading a checkpoint.
        with Session(self.engine) as session:
            row = session.get(AgentProposal, proposal_id)
            if row is None or row.user_id != user_id:
                raise HTTPException(404, '提案不存在或无权访问')
            writer(session.get(User, user_id))
            if row.version != version:
                raise HTTPException(409, '提案版本不匹配')
            if row.status in {'executed', 'rejected'}:
                return view(row)
        config = {'configurable': {'thread_id': proposal_id}}
        with self.lock:
            if not self.graph.get_state(config).values:
                self.graph.invoke({'proposal_id': proposal_id, 'version': version, 'user_id': user_id}, config)
            result = self.graph.invoke(Command(resume={'approved': approved, 'user_id': user_id}), config)
            return result['result']

    def prepare(self, proposal):
        with Session(self.engine) as session:
            row = session.get(AgentProposal, proposal['id'])
            user_id = row.user_id
        config = {'configurable': {'thread_id': proposal['id']}}
        with self.lock:
            if not self.graph.get_state(config).values:
                self.graph.invoke({'proposal_id': proposal['id'], 'version': proposal['version'], 'user_id': user_id}, config)

    def close(self):
        self.connection.close()
