"""Real isolated MySQL: review authorization, state, concurrency and Agent bypass."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent_proposals import make_proposal
from backend.app.documents import transition
from backend.app.models import AuditLog, Document, StockLedger, User
from backend.app.schemas import ActionInput
from test_m1 import system, resources, opening_body
from test_m2 import call, detail


def order(system, kind='purchase', role='storekeeper'):
    warehouse, parts = resources(system,1)
    values={'lines':[{'part_id':parts[0]['id'],'quantity':2,'unit_price':'12.00' if kind=='purchase' else '0.00'}]}
    if kind=='purchase':
        supplier=call(system,'/api/suppliers',{'code':system[3]+uuid4().hex[:8].upper(),'name':'测试审核供应商'},role='admin',expected=201)
        values['supplier_id']=supplier['id']
        response=system[0].put(f"/api/pricing/suppliers/{supplier['id']}/parts/{parts[0]['id']}",headers=system[1]['admin'],
            json={'supply_price':'12.00','source':'测试审核采购报价','active':True,'expected_version':0})
        assert response.status_code==200,response.text
    else:
        target,_=resources(system,0); values['target_warehouse_id']=target['id']
        call(system,'/api/inventory/openings',opening_body(warehouse,parts))
    body={'request_id':str(uuid4()),'kind':kind,'warehouse_id':warehouse['id'],'business_date':str(date.today()),**values}
    return call(system,'/api/documents',body,role=role,expected=201)['id']


@pytest.mark.parametrize('kind,verb',[('purchase','confirm'),('transfer','send')])
def test_review_required_for_api_agent_and_shared_service_then_no_stock_during_review(system,kind,verb):
    did=order(system,kind);client,tokens,engine,prefix,_=system
    body={'request_id':str(uuid4())}
    response=client.post(f'/api/documents/{did}/{verb}',headers=tokens['storekeeper'],json=body)
    assert response.status_code==409 and '审核' in response.json()['detail']
    with Session(engine) as s:
        uid=s.scalar(select(User.id).where(User.username==prefix.lower()+'_storekeeper'))
        before=s.scalar(select(func.count()).select_from(StockLedger))
        with pytest.raises(HTTPException,match='审核'):transition(s,did,verb,ActionInput(request_id=uuid4()),uid)
    with pytest.raises(HTTPException,match='审核'):
        make_proposal(engine,uid,uuid4().hex,uuid4().hex,verb,{'document_id':did,'business_date':str(date.today())})
    first=call(system,f'/api/reviews/documents/{did}/submit',{})
    assert first['review_status']=='pending'
    assert call(system,f'/api/reviews/documents/{did}/submit',{})['replayed']
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='storekeeper',expected=403)
    assert call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='manager')['review_status']=='approved'
    assert call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='manager')['replayed']
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':False,'note':'覆盖结论'},role='admin',expected=409)
    with Session(engine) as s:
        assert s.get(Document,did).status=='draft'
        assert s.scalar(select(func.count()).select_from(StockLedger))==before
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id==did,AuditLog.action=='review.approve'))==1
    call(system,f'/api/documents/{did}/{verb}',body,role='manager',expected=403)
    call(system,f'/api/documents/{did}/{verb}',body)
    # A lost approval response can be retried even after the operator continued.
    assert call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='manager')['replayed']


def test_admin_cannot_approve_own_document_manager_can_and_only_creator_submits(system):
    did=order(system,role='admin')
    call(system,f'/api/reviews/documents/{did}/submit',{},expected=403)
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='manager',expected=409)
    call(system,f'/api/reviews/documents/{did}/submit',{},role='admin')
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='admin',expected=403)
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='manager')


def test_rejection_reason_terminal_decision_and_cancellation(system):
    did=order(system)
    call(system,f'/api/reviews/documents/{did}/submit',{})
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':False,'note':'  '},role='manager',expected=422)
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':False,'note':'数量依据不足'},role='manager')
    call(system,f'/api/reviews/documents/{did}/submit',{},expected=409)
    call(system,f'/api/documents/{did}/confirm',{'request_id':str(uuid4())},expected=409)
    call(system,f'/api/documents/{did}/cancel',{'request_id':str(uuid4())})
    call(system,f'/api/reviews/documents/{did}/decision',{'approved':True},role='admin',expected=409)
    assert detail(system,did)['review']['note']=='数量依据不足'


def test_concurrent_review_cannot_overwrite_conclusion_and_storekeeper_lists_own_only(system):
    did=order(system);other=order(system,role='admin')
    call(system,f'/api/reviews/documents/{did}/submit',{})
    def vote(role):
        return system[0].post(f'/api/reviews/documents/{did}/decision',headers=system[1][role],json={'approved':role=='manager','note':role})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(vote,['admin','manager']))
    assert sorted(r.status_code for r in results)==[200,409]
    listed=system[0].get('/api/reviews/documents',headers=system[1]['storekeeper'],params={'kind':'purchase','page_size':100}).json()['items']
    assert any(r['id']==did for r in listed) and all(r['id']!=other for r in listed)
    assert system[0].post(f'/api/reviews/documents/{other}/decision',headers=system[1]['manager'],json={'approved':'true'}).status_code==422
