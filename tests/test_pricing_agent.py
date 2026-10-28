import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent_proposals import decide, make_proposal
from backend.app.agent_tools import ToolContext
from backend.app.agent_tools import ProfitArgs
from backend.app.business_issues import BusinessIssue
from backend.app.models import Document, Part, StockBalance
from test_agent import actor
from test_m1 import resources, system
from test_m2 import action, call, create
from test_pricing_workflow import quote, sale, setup_stock


def test_profit_tool_accepts_explicit_iso_date_strings_without_changing_range():
    args = ProfitArgs.model_validate({'date_from':'2026-09-01','date_to':'2026-09-15'})
    assert args.date_from == date(2026,9,1)
    assert args.date_to == date(2026,9,15)
    for invalid in ['2026-02-30', 1234, '2026-09-01T00:00:00']:
        with pytest.raises(ValueError):
            ProfitArgs.model_validate({'date_from':invalid})


def test_incomplete_issue_preflight_returns_citable_structured_feedback(system):
    warehouse,part,_=setup_stock(system)
    runtime=type('Runtime',(),{'root':Path(__file__).resolve().parents[1],'settings':system[4]})()
    context=ToolContext(runtime,system[2],actor(system),uuid4().hex,uuid4().hex,'领用1件')
    result=asyncio.run(context.call('assess_request',{'action':'create','payload':{
        'kind':'issue','warehouse_id':warehouse['id'],'business_date':str(date.today()),
        'lines':[{'part_id':part['id'],'quantity':1}]}},'business'))
    assert result['eligible'] is False
    assert result['business_issue']['code']=='incomplete_request'
    assert '收费维修或质保维修' in str(result['business_issue']['facts'])
    assert context.issues and not context.proposals


def test_changed_retail_or_supplier_quote_invalidates_confirmation_without_writes(system):
    warehouse, part, supplier = setup_stock(system)
    uid = actor(system)
    payload = {'kind':'issue','warehouse_id':warehouse['id'],'business_date':str(date.today()),'issue_type':'paid_repair',
        'lines':[{'part_id':part['id'],'quantity':1}]}
    proposal = make_proposal(system[2],uid,uuid4().hex,uuid4().hex,'create',payload)
    sale(system,part,'160.00',1)
    with pytest.raises(BusinessIssue) as caught:
        decide(system[2],proposal['id'],1,uid,True)
    assert caught.value.issue['code']=='proposal_stale'
    payload.update(kind='purchase',lines=[{'part_id':part['id'],'supplier_id':supplier['id'],'quantity':1,'unit_price':'120.00','quote_version':1}])
    payload.pop('issue_type')
    proposal=make_proposal(system[2],uid,uuid4().hex,uuid4().hex,'create',payload)
    quote(system,supplier,part,'125.00',1)
    with pytest.raises(BusinessIssue):
        decide(system[2],proposal['id'],1,uid,True)
    with Session(system[2]) as session:
        assert session.scalar(select(func.count()).select_from(Document).where(Document.warehouse_id==warehouse['id']))==0


def test_profit_tool_is_read_only_and_unavailable_to_diagnostic_agent(system):
    warehouse,part,_=setup_stock(system)
    issue=create(system,'issue',warehouse,[part],issue_type='paid_repair',lines=[{'part_id':part['id'],'quantity':1}])
    action(system,issue['id'],'post')
    runtime=type('Runtime',(),{'root':Path(__file__).resolve().parents[1],'settings':system[4]})()
    context=ToolContext(runtime,system[2],actor(system),uuid4().hex,uuid4().hex,'查询毛利')
    result=asyncio.run(context.call('profits',{'warehouse_id':warehouse['id']},'business'))
    assert result['totals']['gross_profit']=='50.00'
    assert result['totals']['warranty_cost']=='0.00'
    with pytest.raises(HTTPException):
        asyncio.run(context.call('profits',{},'diagnostics'))


def test_multi_line_cost_failure_rolls_back_all_quantities_and_values(system):
    warehouse,parts=resources(system,2)
    call(system,'/api/inventory/openings',{'request_id':str(uuid4()),'warehouse_id':warehouse['id'],'business_date':str(date.today()),
        'lines':[{'part_id':p['id'],'quantity':3,'unit_cost':'10.00','cost_source':'测试期初'} for p in parts]})
    for p in parts:
        sale(system,p,'20.00')
    issue=create(system,'issue',warehouse,parts,issue_type='paid_repair',lines=[{'part_id':p['id'],'quantity':1} for p in parts])
    with Session(system[2]) as session,session.begin():
        session.scalar(select(StockBalance).where(StockBalance.warehouse_id==warehouse['id'],StockBalance.part_id==parts[1]['id'])).stock_value=None
    call(system,f"/api/documents/{issue['id']}/post",{'request_id':str(uuid4()),'business_date':str(date.today())},expected=409)
    with Session(system[2]) as session:
        rows=list(session.scalars(select(StockBalance).where(StockBalance.warehouse_id==warehouse['id'])))
        assert [r.quantity for r in rows]==[3,3]
        assert next(r for r in rows if r.part_id==parts[0]['id']).stock_value==Decimal('30.00')
