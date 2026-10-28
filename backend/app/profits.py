from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Document, DocumentLine, Part, StockLedger, User, Warehouse
from backend.app.security import current_user, session_dependency

router = APIRouter(prefix='/api', tags=['备件毛利'])


def _totals():
    return {**{key: Decimal('0.00') for key in ('revenue', 'paid_cost', 'gross_profit', 'warranty_cost',
        'contribution', 'uncovered_revenue', 'uncovered_cost')}, 'gross_margin_rate': None,
        'quantity': 0, 'paid_quantity': 0, 'warranty_quantity': 0, 'missing_lines': 0, 'missing_quantity': 0}


def _finish(values):
    values['contribution'] = values['gross_profit'] - values['warranty_cost']
    if values['revenue'] and not values['missing_lines']:
        values['gross_margin_rate'] = format((values['gross_profit'] / values['revenue']).quantize(
            Decimal('0.000001'), rounding=ROUND_HALF_UP), '.6f')
    return {key: format(value, '.2f') if isinstance(value, Decimal) else value for key, value in values.items()}


def gross_profit(session, *, warehouse_id=None, part_id=None, category=None, date_from=None,
    date_to=None, sort_by='margin', page=1, page_size=7, details_page=1):
    end = date_to or date.today()
    start = date_from or end - timedelta(days=29)
    if start > end or end > date.today() or (end - start).days > 365:
        raise HTTPException(422, '统计日期需为不超过366天且不晚于今天的有效区间')
    if sort_by not in {'margin', 'margin_rate', 'quantity'} or page < 1 or details_page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, '统计排序或分页参数无效')
    if part_id and session.get(Part, part_id) is None:
        raise HTTPException(404, '备件不存在')
    if warehouse_id and session.get(Warehouse, warehouse_id) is None:
        raise HTTPException(404, '仓库不存在')
    statement = select(StockLedger, Document, DocumentLine, Part, Warehouse).join(DocumentLine,
        DocumentLine.id == StockLedger.document_line_id).join(Document, Document.id == DocumentLine.document_id).join(
        Part, Part.id == DocumentLine.part_id).join(Warehouse, Warehouse.id == StockLedger.warehouse_id).where(
        StockLedger.event == 'issue', Document.kind == 'issue', Document.status == 'posted',
        StockLedger.business_date >= start, StockLedger.business_date <= end)
    if warehouse_id:
        statement = statement.where(StockLedger.warehouse_id == warehouse_id)
    if part_id:
        statement = statement.where(DocumentLine.part_id == part_id)
    if category is not None:
        statement = statement.where(Part.category == category)
    statement = statement.order_by(StockLedger.business_date.desc(), StockLedger.id.desc())
    totals, groups, days, details = _totals(), {}, {}, []
    # ponytail: one pass over the filtered local dataset; move aggregation to SQL if volume warrants it.
    with localcontext() as context:
        context.prec = 50
        for ledger, document, line, part, warehouse in session.execute(statement):
            group = groups.setdefault(part.id, {'part_id': part.id, 'sku': part.sku, 'name': part.name,
                'unit': part.unit, **_totals()})
            day = days.setdefault(ledger.business_date, {'business_date': ledger.business_date, **_totals()})
            purpose = document.issue_type
            cost = line.cost_amount
            price = line.sale_unit_price
            revenue = (price * line.quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if purpose == 'paid_repair' and price is not None else Decimal('0.00') if purpose == 'warranty' else None
            complete = purpose in {'paid_repair', 'warranty'} and cost is not None and price is not None
            profit = revenue - cost if complete and purpose == 'paid_repair' else None
            for target in (totals, group, day):
                target['quantity'] += line.quantity
                if purpose == 'paid_repair':
                    target['paid_quantity'] += line.quantity
                    if revenue is not None:
                        target['revenue'] += revenue
                    if cost is not None:
                        target['paid_cost'] += cost
                    if profit is not None:
                        target['gross_profit'] += profit
                elif purpose == 'warranty':
                    target['warranty_quantity'] += line.quantity
                    if cost is not None:
                        target['warranty_cost'] += cost
                if not complete:
                    target['missing_lines'] += 1
                    target['missing_quantity'] += line.quantity
                    if revenue is not None:
                        target['uncovered_revenue'] += revenue
                    if cost is not None:
                        target['uncovered_cost'] += cost
            details.append({'document_id': document.id, 'business_date': ledger.business_date,
                'warehouse': warehouse.name, 'warehouse_id': warehouse.id, 'part_id': part.id,
                'name': part.name, 'sku': part.sku, 'issue_type': purpose, 'quantity': line.quantity,
                'sale_unit_price': format(price, '.2f') if price is not None else None,
                'revenue': format(revenue, '.2f') if revenue is not None else None,
                'cost_amount': format(cost, '.2f') if cost is not None else None,
                'gross_profit': format(profit, '.2f') if profit is not None else None,
                'pricing_source': line.pricing_source or document.pricing_source, 'complete': complete})
        ranking = [_finish(group) for group in groups.values()]
        if sort_by == 'quantity':
            ranking.sort(key=lambda row: (-row['quantity'], row['part_id']))
        elif sort_by == 'margin_rate':
            ranking.sort(key=lambda row: (row['gross_margin_rate'] is None,
                -Decimal(row['gross_margin_rate'] or '0'), row['part_id']))
        else:
            ranking.sort(key=lambda row: (-Decimal(row['gross_profit']), row['part_id']))
        result = {'date_from': start, 'date_to': end, 'warehouse_id': warehouse_id,
            'part_id': part_id, 'category': category, 'totals': _finish(totals),
            'ranking': ranking[(page - 1) * page_size:page * page_size], 'ranking_total': len(ranking),
            'trend': [_finish(days[key]) for key in sorted(days)],
            'details': details[(details_page - 1) * page_size:details_page * page_size], 'details_total': len(details),
            'page': page, 'details_page': details_page, 'page_size': page_size,
            'notice': '仅按已过账领用流水业务日期统计备件金额，成本按实际执行顺序分摊；不计人工、房租、税额拆分或厂家赔付，非净利润。缺失行保留已知金额，毛利仅汇总结算与成本完整的收费明细；缺失时毛利率暂无，贡献为已覆盖收费毛利减已知质保成本。明细保留模拟补录来源。'}
    return result


@router.get('/profits')
def profits(warehouse_id: int | None = Query(default=None, gt=0), part_id: int | None = Query(default=None, gt=0),
    category: str | None = Query(default=None, max_length=40), date_from: date | None = None,
    date_to: date | None = None, sort_by: Literal['margin', 'margin_rate', 'quantity'] = 'margin',
    page: int = Query(default=1, ge=1), page_size: int = Query(default=7, ge=1, le=100),
    details_page: int | None = Query(default=None, ge=1), user: User = Depends(current_user),
    session: Session = Depends(session_dependency)):
    return gross_profit(session, warehouse_id=warehouse_id, part_id=part_id, category=category,
        date_from=date_from, date_to=date_to, sort_by=sort_by, page=page, page_size=page_size,
        details_page=details_page if details_page is not None else page)
