"""Shared typed tool whitelist. Query tools never mutate business data."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from math import ceil
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, PositiveInt, ValidationError, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.app.agent_proposals import make_proposal, snapshot
from backend.app.business_issues import BusinessIssue
from backend.app.knowledge import ROOT, KnowledgeError
from backend.app.knowledge_answers import StrictModel
from backend.app.knowledge_api import KnowledgeQuery, run_query
from backend.app.models import Document, DocumentLine, Part, StockLedger, Supplier, User, Warehouse
from backend.app.queries import balance_statement, ledger_statement
from backend.app.web_search import tavily_search


class CatalogArgs(StrictModel):
    query: str = Field(default='', max_length=100)
    part_id: PositiveInt | None = None


class StockArgs(StrictModel):
    part_id: PositiveInt | None = None
    warehouse_id: PositiveInt | None = None


class HistoryArgs(StrictModel):
    part_id: PositiveInt
    warehouse_id: PositiveInt
    days: int = Field(default=30, ge=1, le=90)


class DocumentArgs(StrictModel):
    document_id: PositiveInt | None = None
    kind: Literal['purchase', 'receipt', 'issue', 'transfer'] | None = None
    warehouse_id: PositiveInt | None = None


class ProfitArgs(StrictModel):
    warehouse_id: PositiveInt | None = None
    part_id: PositiveInt | None = None
    category: str | None = Field(default=None, max_length=40)
    date_from: date | None = None
    date_to: date | None = None
    sort_by: Literal['margin','margin_rate','quantity'] = 'margin'
    page: int = Field(default=1, ge=1)

    @field_validator('date_from', 'date_to', mode='before')
    @classmethod
    def iso_date(cls, value):
        return date.fromisoformat(value) if isinstance(value, str) else value


class ReplenishArgs(StrictModel):
    part_id: PositiveInt
    warehouse_id: PositiveInt
    supplier_id: PositiveInt


class KnowledgeArgs(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    knowledge_base: Literal['inventory_policies', 'general_diagnostics']


class WebArgs(StrictModel):
    query: str = Field(min_length=1, max_length=300)


class RequestArgs(StrictModel):
    action: Literal['create', 'confirm', 'post', 'send', 'receive', 'cancel', 'receipt']
    payload: dict


class ProposalArgs(RequestArgs):
    instruction_quote: str = Field(min_length=1, max_length=2000)


class FinalClaim(StrictModel):
    text: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class FinishArgs(StrictModel):
    claims: list[FinalClaim] = Field(default_factory=list, max_length=10)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=3)
    message: str = Field(default='', max_length=1000, description='仅问候、追问或能力说明；业务事实必须放claims并引用工具证据')


TOOL_MODELS = {
    'catalog': (CatalogArgs, '查询备件名称/别名/SKU、仓库、合格供应商逐件供货报价和版本、门店定价及版本。默认供应商优先；缺报价不是0元。'),
    'profits': (ProfitArgs, '按实际过账成本查询收费维修毛利、质保消耗成本、排行和明细。不是净利润；明确时间/仓库/缺失与模拟来源。'),
    'stock': (StockArgs, '查询各仓实时现有、可用、采购待收和调拨在途，留空查全部，最多100行。'),
    'history': (HistoryArgs, '查询最近完整自然日已过账领用和库存流水；记录覆盖不足必须明确，不用调拨当消耗。'),
    'documents': (DocumentArgs, '查询单据及明细，指定编号查看详情，否则返回最近20单。'),
    'replenishment': (ReplenishArgs, '确定性计算30日消耗、目标量、库存位置、可调余量和剩余采购缺口。历史覆盖不足不计算目标。'),
    'knowledge': (KnowledgeArgs, '检索库存制度或通用故障资料并返回引用问答。只以用户提供症状/检测为条件，不虚构检测。'),
    'web_search': (WebArgs, '仅查询必要公开信息，不能发送内部库存、凭据或整份对话。'),
    'prepare_proposal': (ProposalArgs, '仅保存待人工确认的提案，不创建业务单据。每轮最多一个；修改在新轮生成，旧提案失效。'),
    'assess_request': (RequestArgs, '只读核对采购、入库、领用、调拨等业务请求，返回可执行条件或异常事实及可选处理方式；不会保存提案或执行。'),
    'finish': (FinishArgs, '结束本任务。业务结论用claims引用实际工具evidence_id；不能声称待确认提案已执行。'),
}

ROLE_TOOLS = {
    'diagnostics': ['knowledge', 'web_search', 'finish'],
    'business': ['catalog', 'stock', 'history', 'documents', 'profits', 'replenishment', 'assess_request', 'prepare_proposal', 'finish'],
    'single': ['catalog', 'stock', 'history', 'documents', 'profits', 'replenishment', 'knowledge', 'web_search', 'assess_request', 'prepare_proposal', 'finish'],
}


def definitions(names):
    return [{'type': 'function', 'function': {'name': name, 'description': TOOL_MODELS[name][1],
        'parameters': TOOL_MODELS[name][0].model_json_schema()}} for name in names]


def jsonable(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def complete_history(root, part, warehouse, start, end):
    path = root/'data/catalog/random-history-manifest.json'
    if not path.exists():
        return False
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if not (manifest['consumption_from'] <= str(start) <= str(end) <= manifest['consumption_to']):
        return False
    return any(r['part_id'] == part.id and r['sku'] == part.sku and r['warehouse_id'] == warehouse.id and r['warehouse_code'] == warehouse.code for r in manifest['plan'])


def query_tool(session, name, args, root=ROOT, *, allocated=None):
    if name == 'profits':
        from backend.app.profits import gross_profit
        return jsonable(gross_profit(session, **args.model_dump(), page_size=7))
    if name == 'catalog':
        statement = select(Part).where(Part.active.is_(True))
        if args.part_id:
            statement = statement.where(Part.id == args.part_id)
        elif args.query.isdecimal():
            statement = statement.where(Part.id == int(args.query))
        elif args.query:
            statement = statement.where(or_(Part.name.contains(args.query), Part.sku.contains(args.query), Part.aliases.contains(args.query)))
        parts = list(session.scalars(statement.order_by(Part.id).limit(100)))
        suppliers = list(session.scalars(select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.id)))
        from backend.app.pricing_api import pricing_parts
        prices = {r['part_id']: r for r in pricing_parts(session)['items']}
        return {'parts': [{'id': p.id, 'sku': p.sku, 'name': p.name, 'aliases': p.aliases, 'category': p.category, 'unit': p.unit,
            'specification': p.specification, 'reference_price': str(p.reference_price), 'safety_stock': p.safety_stock,
            'sale_price': prices[p.id]['sale_price'], 'sale_price_version': p.sale_price_version, 'default_supplier_id': p.default_supplier_id,
            'suppliers': [r | {'lead_time_days': next(s.lead_time_days for s in suppliers if s.id==r['id'])} for r in prices[p.id]['suppliers']]} for p in parts],
            'warehouses': [{'id': w.id, 'code': w.code, 'name': w.name} for w in session.scalars(select(Warehouse).where(Warehouse.active.is_(True)).order_by(Warehouse.id))],
            'notice': '供货价用于采购，门店定价用于收费维修；只有实际入库形成成本。reference_price仅为旧参考字段；数量均为件，项目模拟报价不是外部真实报价。', 'limit': 100}
    if name == 'stock':
        statement = balance_statement().where(Part.active.is_(True), Warehouse.active.is_(True))
        if args.part_id:
            statement = statement.where(Part.id == args.part_id)
        if args.warehouse_id:
            statement = statement.where(Warehouse.id == args.warehouse_id)
        items = [dict(r) for r in session.execute(statement.order_by(Warehouse.id, Part.id).limit(100)).mappings()]
        for row in items:
            for key in ('quantity', 'available', 'purchase_pending', 'in_transit'):
                row[key] = int(row[key])
        return jsonable({'items': items,
            'limit': 100, 'as_of': datetime.now(timezone(timedelta(hours=8))).isoformat(), 'time_zone': 'Asia/Shanghai', 'reserved': 0,
            'notice': '当前业务快照；预留未实现，可用量等于现有量；尚未建账不代表已盘点实物。'})
    if name == 'documents':
        from backend.app.reviews import review_view
        statement = select(Document).order_by(Document.id.desc())
        if args.document_id:
            statement = statement.where(Document.id == args.document_id)
        if args.kind:
            statement = statement.where(Document.kind == args.kind)
        if args.warehouse_id:
            statement = statement.where(or_(Document.warehouse_id == args.warehouse_id, Document.target_warehouse_id == args.warehouse_id))
        rows = []
        for d in session.scalars(statement.limit(20)):
            rows.append({'id': d.id, 'kind': d.kind, 'status': d.status, 'review': review_view(session, d), 'warehouse_id': d.warehouse_id,
                'target_warehouse_id': d.target_warehouse_id, 'business_date': str(d.business_date), 'issue_type': d.issue_type,
                'warranty_reason': d.warranty_reason, 'work_order': d.work_order,
                'lines': [{'id': l.id, 'part_id': l.part_id, 'quantity': l.quantity, 'received_quantity': l.received_quantity,
                    'supplier_id': l.supplier_id, 'unit_price': str(l.unit_price), 'sale_unit_price': str(l.sale_unit_price) if l.sale_unit_price is not None else None,
                    'cost_amount': str(l.cost_amount) if l.cost_amount is not None else None, 'quote_version': l.quote_version,
                    'pricing_source': l.pricing_source} for l in session.scalars(select(DocumentLine).where(DocumentLine.document_id == d.id).order_by(DocumentLine.id))]})
        return {'items': rows, 'limit': 20}
    if name in {'history', 'replenishment'}:
        part, warehouse = session.get(Part, args.part_id), session.get(Warehouse, args.warehouse_id)
        if part is None or warehouse is None or not part.active or not warehouse.active:
            raise HTTPException(404, '备件或仓库不存在或已停用')
        days = args.days if name == 'history' else 30
        end = date.today()-timedelta(days=1)
        start = end-timedelta(days=days-1)
        quantity = session.scalar(select(func.coalesce(func.sum(DocumentLine.quantity), 0)).join(Document, Document.id == DocumentLine.document_id).where(
            Document.kind == 'issue', Document.status == 'posted', Document.warehouse_id == warehouse.id,
            DocumentLine.part_id == part.id, Document.business_date.between(start, end)))
        result = {'part_id': part.id, 'sku': part.sku, 'warehouse_id': warehouse.id, 'start': str(start), 'end': str(end),
            'days': days, 'posted_issue_quantity': int(quantity), 'full_window_declared': complete_history(root, part, warehouse, start, end),
            'source': 'project_simulated_business_records', 'notice': '已记录模拟业务；完整覆盖仅按显式计划声明，不能以最早一笔记录推断完整历史。'}
        if name == 'history':
            result['ledger'] = [dict(r) for r in session.execute(ledger_statement().where(Part.id == part.id, Warehouse.id == warehouse.id).order_by(StockLedger.id.desc()).limit(30)).mappings()]
            return jsonable(result)
        supplier = session.get(Supplier, args.supplier_id)
        if supplier is None or not supplier.active or part.category not in supplier.supply_categories:
            raise HTTPException(409, '供应商不具备供货资格')
        result['supplier'] = {'id': supplier.id, 'name': supplier.name, 'lead_time_days': supplier.lead_time_days}
        if not result['full_window_declared']:
            return result | {'calculation_status': 'insufficient_history', 'target': None, 'purchase_gap': None}
        balances = query_tool(session, 'stock', StockArgs(part_id=part.id))['items']
        targets, daily = {}, Fraction(int(quantity), 30)
        for row in balances:
            other = session.get(Warehouse, row['warehouse_id'])
            if not complete_history(root, part, other, start, end):
                continue
            used = session.scalar(select(func.coalesce(func.sum(DocumentLine.quantity), 0)).join(Document, Document.id == DocumentLine.document_id).where(Document.kind == 'issue', Document.status == 'posted', Document.warehouse_id == other.id, DocumentLine.part_id == part.id, Document.business_date.between(start, end)))
            targets[other.id] = ceil(Fraction(int(used), 30)*supplier.lead_time_days+part.safety_stock)
        here = next(r for r in balances if r['warehouse_id'] == warehouse.id)
        position = here['available']+here['purchase_pending']+here['in_transit']
        gap = max(0, targets[warehouse.id]-position)
        transfers = []
        for row in sorted(balances, key=lambda r: (-max(0, r['available']-targets.get(r['warehouse_id'], r['available'])), r['warehouse_id'])):
            if row['warehouse_id'] == warehouse.id or row['warehouse_id'] not in targets:
                continue
            committed = (allocated or {}).get((part.id, row['warehouse_id']), 0)
            take = min(gap, max(0, row['available']-targets[row['warehouse_id']]-committed))
            if take:
                transfers.append({'source_warehouse_id': row['warehouse_id'], 'target_warehouse_id': warehouse.id, 'part_id': part.id, 'quantity': take})
                gap -= take
        return result | {'calculation_status': 'calculated', 'daily_consumption': str(daily), 'target': targets[warehouse.id],
            'position': position, 'gross_gap': max(0, targets[warehouse.id]-position), 'transfers': transfers, 'purchase_gap': gap,
            'formula': 'ceil(30日已过账领用/30×所选供应商交期+安全库存)；库存位置=可用+采购待收+调入在途',
            'allocation_order': '可调余量降序，同余量仓库ID升序；每次扣减剩余缺口。未声明完整历史的来源仓不建议调出。'}
    raise HTTPException(422, '未知查询工具')


class ToolContext:
    def __init__(self, runtime, engine, user_id, thread_id, turn_id, user_text, *, allow_web=True, scope='draft_preview'):
        self.runtime, self.engine, self.user_id = runtime, engine, user_id
        self.thread_id, self.turn_id, self.user_text = thread_id, turn_id, user_text
        self.allow_web, self.scope = allow_web, scope
        self.trace, self.proposals, self.knowledge_answers, self.issues = [], [], [], []
        self.allocations, self.replenishments = {}, {}

    async def call(self, name, arguments, role):
        if name not in ROLE_TOOLS.get(role, []):
            raise HTTPException(403, '当前角色无权调用该工具')
        model = TOOL_MODELS.get(name)
        if model is None:
            raise KnowledgeError('unknown_tool', '工具未在白名单中')
        args = model[0].model_validate(arguments)
        if name == 'knowledge':
            if role == 'diagnostics' and args.knowledge_base != 'general_diagnostics':
                raise HTTPException(403, '故障Agent只能检索故障知识域')
            query = KnowledgeQuery(question=args.question, knowledge_base=args.knowledge_base, knowledge_scope=self.scope)
            # A subtask can omit the stock question, but test-record provenance is
            # always checked against original user messages, never the model rewrite.
            result = await run_query(self.runtime, query, ask=True, search_question=args.question,
                conversation_context=[{'role': 'user', 'content': self.user_text}], user_evidence=self.user_text)
            self.knowledge_answers.append(result)
            return result
        if name == 'web_search':
            if not self.allow_web:
                raise HTTPException(409, '联网搜索已关闭')
            sources, usage = await tavily_search(self.runtime.settings, args.query)
            return {'sources': list(sources.values()), 'usage': {'search': usage}, 'notice': '公开网页不构成内部审批或库存事实'}
        if name == 'prepare_proposal':
            if role == 'diagnostics':
                raise HTTPException(403, '故障Agent不能生成业务提案')
            if args.instruction_quote not in self.user_text:
                raise HTTPException(422, '办理意图必须有用户原文出处')
            if self.issues:
                raise HTTPException(409, '本轮业务存在待处理异常，请先由员工选择处理方式，再在下一轮整理提案；不能自动更改原需求。')
            result = await asyncio.to_thread(make_proposal, self.engine, self.user_id, self.thread_id, self.turn_id, args.action, args.payload)
            self.proposals.append(result)
            return result | {'notice': '仅待人工核对确认；尚未创建业务单据或变更库存。供货价、门店定价与用途已按服务端规则核对。'}
        if name == 'finish':
            return args.model_dump()
        def read():
            with Session(self.engine) as session:
                user = session.get(User, self.user_id)
                if user is None or not user.active:
                    raise HTTPException(401, '登录身份已失效')
                if name == 'assess_request':
                    try:
                        return {'eligible': True, 'snapshot': snapshot(session, args.action, args.payload), 'notice': '当前条件可供整理提案，执行前仍需重新核对及人工审核。'}
                    except ValidationError as error:
                        issue = BusinessIssue('incomplete_request', '业务参数不完整，请补齐必要条件后重新核对',
                            facts={'validation_errors': [{'field': '.'.join(map(str, e['loc'])) or 'request', 'message': e['msg']}
                                for e in error.errors(include_input=False)]},
                            options=[('补齐业务条件', '请列出刚才请求缺少的必要字段，等我补充后重新核对，不默认选择维修用途或改写业务参数。')])
                        self.issues.append(issue.issue)
                        return {'eligible': False, 'business_issue': issue.issue}
                    except BusinessIssue as error:
                        self.issues.append(error.issue)
                        return {'eligible': False, 'business_issue': error.issue}
                if name == 'replenishment':
                    key = (args.part_id, args.warehouse_id)
                    if key in self.replenishments:
                        old_supplier, old_result = self.replenishments[key]
                        if old_supplier != args.supplier_id:
                            raise HTTPException(409, '本轮该目标已按另一供应商测算；修改条件请在新一轮重新计算')
                        return old_result
                result = query_tool(session, name, args, self.runtime.root, allocated=self.allocations)
                if name == 'replenishment':
                    for transfer in result.get('transfers', []):
                        key = (args.part_id, transfer['source_warehouse_id'])
                        self.allocations[key] = self.allocations.get(key, 0)+transfer['quantity']
                    self.replenishments[(args.part_id, args.warehouse_id)] = (args.supplier_id, result)
                return result
        return await asyncio.to_thread(read)
