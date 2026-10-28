"""Regression scenarios discovered in M3/M4; these are not independent scores."""
import asyncio
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent_proposals import snapshot
from backend.app.agent_tools import CatalogArgs, StockArgs, ToolContext, query_tool
from backend.app.assistant_store import professional_labels
from backend.app.business_issues import BusinessIssue
from backend.app.documents import create_document, transition
from backend.app.knowledge_answers import bind_answer
from backend.app.knowledge_math import CalculationInputError, calculate_example
from backend.app.models import Document, DocumentLine, Part, Supplier, SupplierPrice
from backend.app.schemas import ActionInput, DocumentInput
from test_agent import actor, issue_payload
from test_knowledge_answers import calculation, quoted
from test_m1 import system, resources, opening_body
from test_m2 import call


def test_diagnostic_catalog_mapping_uses_the_same_packaged_piece_unit():
    mapping = json.loads((Path(__file__).resolve().parents[1] / 'data/knowledge/diagnostics/part-type-map.json').read_text(encoding='utf-8'))
    assert len(mapping['parts']) == 29
    assert {row['unit'] for row in mapping['parts']} == {'件'}


def test_business_preflight_shortage_choice_and_no_automatic_alternative(system):
    warehouse, parts = resources(system,1)
    call(system,'/api/inventory/openings',opening_body(warehouse,parts))
    runtime = type('Runtime',(),{'root':Path(__file__).resolve().parents[1],'settings':system[4]})()
    context = ToolContext(runtime,system[2],actor(system),uuid4().hex,uuid4().hex,'请帮我领用9件',allow_web=False)
    with Session(system[2]) as session: before = session.scalar(select(func.count()).select_from(Document))
    result = asyncio.run(context.call('assess_request',{'action':'create','payload':issue_payload(warehouse,parts[0],9)},'business'))
    issue = result['business_issue']
    assert not result['eligible'] and issue['code'] == 'insufficient_stock'
    assert issue['facts']['items'][0]['gap'] == 6
    assert len(issue['options']) == 3 and '采购' in str(issue['options'])
    with pytest.raises(HTTPException,match='员工选择'):
        asyncio.run(context.call('prepare_proposal',{'action':'create','payload':issue_payload(warehouse,parts[0],2),'instruction_quote':'请帮我领用9件'},'business'))
    with Session(system[2]) as session: assert session.scalar(select(func.count()).select_from(Document)) == before
    # Transfer shortage uses the same facts and approval boundary.
    destination, _ = resources(system,1)
    payload = issue_payload(warehouse,parts[0],9) | {'kind':'transfer','target_warehouse_id':destination['id']}
    payload.pop('issue_type')
    with Session(system[2]) as session, pytest.raises(BusinessIssue) as caught: snapshot(session,'create',payload)
    assert caught.value.issue['code'] == 'insufficient_stock'


def test_supplier_inactive_and_state_receipt_exceptions_have_recovery_options(system):
    warehouse, parts = resources(system,1)
    engine, uid = system[2], actor(system)
    with Session(engine) as session, session.begin():
        supplier = Supplier(code=system[3]+'-S'+uuid4().hex[:6],name='测试油液供应商',lead_time_days=3,supply_categories=['油液'])
        session.add(supplier);session.flush();supplier_id=supplier.id
    payload = issue_payload(warehouse,parts[0]) | {'kind':'purchase','lines':[{'part_id':parts[0]['id'],'supplier_id':supplier_id,'quantity':2,'unit_price':'10.00'}]}
    payload.pop('issue_type')
    with Session(engine) as session, pytest.raises(BusinessIssue) as caught: snapshot(session,'create',payload)
    assert caught.value.issue['code'] == 'supplier_not_qualified' and caught.value.issue['options']
    with Session(engine) as session, session.begin(): session.get(Part,parts[0]['id']).active=False
    with Session(engine) as session, pytest.raises(BusinessIssue) as caught: snapshot(session,'create',issue_payload(warehouse,parts[0]))
    assert caught.value.issue['code'] == 'catalog_unavailable'
    with Session(engine) as session, session.begin():
        session.get(Part,parts[0]['id']).active=True
        session.get(Supplier,supplier_id).supply_categories=['']
        session.add(SupplierPrice(supplier_id=supplier_id,part_id=parts[0]['id'],supply_price=10,version=1,active=True,source='测试供应商报价'))
    with Session(engine) as session, session.begin():
        document = create_document(session,DocumentInput.model_validate(payload|{'request_id':str(uuid4())}),uid)
        did=document['id']
        line_id=session.scalar(select(DocumentLine.id).where(DocumentLine.document_id==did))
        with pytest.raises(BusinessIssue) as caught: snapshot(session,'receipt',{'document_id':did,'lines':[{'purchase_line_id':line_id,'quantity':1}]})
        assert caught.value.issue['code'] == 'state_conflict'
        # Complete independent review in this transaction before stock actions.
        from backend.app.reviews import submit_review, decide_review
        from backend.app.models import User
        submit_review(session,did,uid)
        manager_id=session.scalar(select(User.id).where(User.username==system[3].lower()+'_manager'))
        decide_review(session,did,manager_id,True)
        transition(session,did,'confirm',ActionInput(request_id=uuid4()),uid)
        with pytest.raises(BusinessIssue) as caught: snapshot(session,'receipt',{'document_id':did,'lines':[{'purchase_line_id':line_id,'quantity':3}]})
        assert caught.value.issue['code'] == 'receipt_mismatch' and caught.value.issue['facts']['lines'][0]['remaining'] == 2


def test_uploaded_images_can_be_saved_for_all_three_catalogs(system):
    client,tokens,_,prefix,_ = system
    raw=io.BytesIO();Image.new('RGB',(12,12),'white').save(raw,format='PNG')
    uploaded=client.post('/api/media/images',headers=tokens['admin']|{'Content-Type':'image/png'},content=raw.getvalue()).json()
    key=uploaded['image_key']
    for path,body in [('parts',{'sku':prefix+'-IMG','name':'测试图片备件','unit':'件'}),('suppliers',{'code':prefix+'-IMG','name':'测试图片供应商'}),('warehouses',{'code':prefix+'-IMG','name':'测试图片仓库'})]:
        response=client.post('/api/'+path,headers=tokens['admin'],json=body|{'image_key':key})
        assert response.status_code == 201,response.text
        row=response.json(); assert row['image_key'] == key
        response=client.put(f"/api/{path}/{row['id']}",headers=tokens['admin'],json=body|{'image_key':''})
        assert response.status_code == 200 and response.json()['image_key'] == ''
    assert client.post('/api/parts',headers=tokens['admin'],json={'sku':prefix+'-BADIMG','name':'无效图片','image_key':'upload-'+'a'*32}).status_code == 422


def test_numeric_catalog_query_and_timezone(system):
    warehouse,parts=resources(system,1)
    with Session(system[2]) as session:
        result=query_tool(session,'catalog',CatalogArgs(query=str(parts[0]['id'])))
        assert [r['id'] for r in result['parts']] == [parts[0]['id']]
        stock=query_tool(session,'stock',StockArgs(part_id=parts[0]['id']))
        assert stock['as_of'].endswith('+08:00') and stock['time_zone'] == 'Asia/Shanghai'


def test_legacy_answer_labels_do_not_rewrite_user_or_sources():
    answer={'message':'待真人审核','claims':[{'text':'需真人确认','citation_ids':['c1']}],'citations':[{'excerpt':'原文含真人'}],'follow_up_questions':['真人是否确认？']}
    normalized=professional_labels(answer)
    assert '真人' not in normalized['message']+normalized['claims'][0]['text']
    assert normalized['citations'][0]['excerpt'] == '原文含真人' and answer['message'] == '待真人审核'


def test_diagnostic_record_normalization_cannot_leave_contradictory_narrative():
    pool={'c1':{'excerpt':'按检查条件判断换件','unit_id':'CASE-1'}}
    types={'part':{'catalog_sku':'x','catalog_name':'部件','unit':'件','fitment_status':'unknown','required_quantity':None,'case_ids':['CASE-1']}}
    data={'answer_status':'answered','claims':[{'text':'这个部件必须更换。','citation_ids':['c1']}], 'part_decisions':[{'part_type_key':'part','status':'recommended','text':'建议更换','condition':'需检测记录','citation_ids':['c1']}]}
    result=bind_answer(json.dumps(data),pool,types,'没有检测记录','general_diagnostics')
    assert result['part_decisions'][0]['status'] == 'undetermined' and result['claims'] == []
    data['part_decisions'][0]['check_record']={'quote':'负载试验已失败，标准为维修手册限值','test':'负载试验','observed':'已失败','criterion':'维修手册限值'}
    result=bind_answer(json.dumps(data),pool,types,'检测记录：负载试验已失败，标准为维修手册限值','general_diagnostics')
    assert result['part_decisions'][0]['status'] == 'recommended'


def test_transfer_alias_mapping_and_default_allocation_keep_numeric_provenance():
    question='假设A仓现有15件，B仓现有3件，A仓向B仓调拨6件'
    inputs={'source':quoted(15,'A仓现有15件'),'target':quoted(3,'B仓现有3件'),'quantity':quoted(6,'调拨6件')}
    result=calculate_example(calculation('transfer_conservation',inputs),question)
    assert result['result']['after_receive']['source'] == 9 and result['result']['after_receive']['target'] == 9
    with pytest.raises(CalculationInputError): calculate_example(calculation('transfer_conservation',inputs),question.replace('A仓向B仓','B仓向A仓'))
    inputs={'sources':[{'code':quoted('A','A仓'),'available':quoted(9,'A仓可用9件'),'target':quoted(5,'A仓目标5件')}], 'demands':[{'code':quoted('B','B仓'),'gap':quoted(6,'B仓缺口6件')}], 'order_quote':''}
    question='假设A仓可用9件，A仓目标5件，B仓缺口6件'
    result=calculate_example(calculation('allocation',inputs),question)
    assert result['result']['purchase_gaps']['B'] == 2
    with pytest.raises(CalculationInputError): calculate_example(calculation('allocation',inputs),question+'，优先B仓')


def test_hypothetical_complete_history_declares_posted_quantity_assumption():
    inputs={'issue_30d':quoted(60,'完整30日领用60件'),'days':quoted(30,'完整30日'),'lead_days':quoted(4,'交期4天'),
        'safety':quoted(3,'安全库存3件'),'available':quoted(1,'可用1件'),'purchase_pending':quoted(2,'采购待收2件'),'transfer_inbound':quoted(0,'调拨在途0件')}
    question='假设，'+'，'.join(r['quote'] for r in inputs.values())
    result=calculate_example(calculation('replenishment',inputs),question)
    assert result['result']['gap'] == 8 and '假定' in result['steps'][0]
    inputs['issue_30d']=quoted(60,'不足30日领用60件')
    with pytest.raises(CalculationInputError): calculate_example(calculation('replenishment',inputs),question+'，不足30日领用60件')


def test_wrong_knowledge_domain_returns_no_diagnostic_fact():
    data={'answer_status':'answered','fault_types':[{'text':'未经检索的诊断结论','citation_ids':['c1']}]}
    result=bind_answer(json.dumps(data),{'c1':{'excerpt':'库存制度'}},{},'制动异响是什么故障','inventory_policies')
    assert result['answer_status'] == 'outside_scope' and result['fault_types'] == [] and not result['citations']


def test_common_rule_citation_cannot_support_specific_part_verdict():
    data={'answer_status':'answered','part_decisions':[{'part_type_key':'part','status':'undetermined','text':'判断','condition':'须检查','citation_ids':['common']}]}
    types={'part':{'catalog_sku':'x','catalog_name':'部件','unit':'件','fitment_status':'unknown','required_quantity':None,'case_ids':['CASE-1']}}
    result=bind_answer(json.dumps(data),{'common':{'excerpt':'先进行安全分流','unit_id':'RULE-1'}},types,'症状','general_diagnostics')
    assert result['answer_status'] == 'insufficient_evidence' and not result['part_decisions'] and not result['citations']


def test_terminal_object_completion_never_invents_a_string_or_value():
    from backend.app.knowledge import KnowledgeError
    data={'answer_status':'answered','claims':[{'text':'有依据的结论','citation_ids':['c1']}]}
    raw=json.dumps(data,ensure_ascii=False)
    assert bind_answer(raw[:-1],{'c1':{'excerpt':'有依据的结论'}},{},'问题','inventory_policies')['claims']
    with pytest.raises(KnowledgeError):bind_answer('{"answer_status":"answered","claims":[{"text":"未结束',{}, {},'问题','inventory_policies')


def test_record_can_cite_manual_criterion_but_not_hypothetical_result():
    types={'part':{'catalog_sku':'x','catalog_name':'部件','unit':'件','fitment_status':'unknown','required_quantity':None,'case_ids':['CASE-1']}}
    pool={'c1':{'unit_id':'CASE-1','excerpt':'本体开裂且需更换时才支持换件'}}
    quote='确认部件本体开裂且需更换'
    data={'answer_status':'answered','part_decisions':[{'part_type_key':'part','status':'recommended','text':'按报告支持更换','condition':None,'citation_ids':['c1'],
        'check_record':{'quote':quote,'test':'对部件进行本体检查','observed':'本体开裂','criterion':'本体开裂且需更换'}}]}
    result=bind_answer(json.dumps(data),pool,types,'今天的检查记录：'+quote,'general_diagnostics')
    assert result['part_decisions'][0]['status']=='recommended' and result['part_decisions'][0]['check_record']['test']==quote
    result=bind_answer(json.dumps(data),pool,types,'假设未来检查记录写着'+quote+'，现在还没有检查。','general_diagnostics')
    assert result['part_decisions'][0]['status']=='undetermined'


def test_hypothetical_check_plan_is_conditional_and_safety_claim_survives_bad_part_citation():
    types={'part':{'catalog_sku':'x','catalog_name':'部件','unit':'件','fitment_status':'unknown','required_quantity':None,'case_ids':['CASE-1']}}
    question='假设专业检查排除控制因素后证实本体故障，目前只是检查方案。'
    data={'answer_status':'answered','part_decisions':[{'part_type_key':'part','status':'conditional','text':'检查后才判断','condition':None,'citation_ids':['c1'],'basis_quote':question}]}
    result=bind_answer(json.dumps(data),{'c1':{'unit_id':'CASE-1','excerpt':'须检查后判断'}},types,question,'general_diagnostics')
    assert result['part_decisions'][0]['status']=='conditional'
    data['claims']=[{'text':'危险时应安全停车并联系救援','citation_ids':['safe']}]
    result=bind_answer(json.dumps(data),{'c1':{'unit_id':'OTHER','excerpt':'公共规则'},'safe':{'unit_id':'RULE','excerpt':'危险时应安全停车并联系救援'}},types,question,'general_diagnostics')
    assert not result['part_decisions'] and result['claims'][0]['text'].endswith('救援')


def test_allocation_accepts_declared_default_and_does_not_swap_numbers():
    inputs={'sources':[{'code':'A','available':quoted(13,'A仓可用13件、目标库存7件'),'target':quoted(7,'A仓可用13件、目标库存7件')}],
        'demands':[{'code':'C','gap':quoted(8,'C仓缺口8件')}],'order_quote':'按制度默认规则'}
    question='假设同SKU同单位，A仓可用13件、目标库存7件；C仓缺口8件，按制度默认规则。'
    spec=calculation('allocation',inputs)
    assert calculate_example(spec,question)['result']['purchase_gaps']['C']==2
    inputs['sources'][0]['available']['value']=7
    with pytest.raises(CalculationInputError):calculate_example(spec,question)


def test_default_retrieval_respects_domain_and_explicit_override():
    from backend.app.knowledge_api import KnowledgeQuery
    assert KnowledgeQuery(question='制度',knowledge_base='inventory_policies').retrieval_mode=='hybrid'
    assert KnowledgeQuery(question='故障',knowledge_base='general_diagnostics').retrieval_mode=='dense'
    assert KnowledgeQuery(question='制度',knowledge_base='inventory_policies',retrieval_mode='dense').retrieval_mode=='dense'


def test_shared_demand_quantity_keeps_source_and_destination_provenance():
    inputs={'sources':[{'code':'S','available':quoted(20,'S可用20件'),'target':quoted(10,'目标库存10件')}],
        'demands':[{'code':code,'gap':quoted(6,'各6件')} for code in ['X','Y']], 'order_quote':''}
    question='假设依次处理X和Y缺口，各6件；来源S可用20件、目标库存10件。'
    assert calculate_example(calculation('allocation',inputs),question)['result']['purchase_gaps'] == {'X':0,'Y':2}
    with pytest.raises(CalculationInputError):
        calculate_example(calculation('allocation',inputs),question.replace('缺口','库存'))
    inputs['demands'][1]['code']='Z'
    with pytest.raises(CalculationInputError):
        calculate_example(calculation('allocation',inputs),question+'另有Z仓待核实。')


def test_refusal_is_not_relabelled_when_model_includes_discarded_part_rows():
    data={'answer_status':'refused','part_decisions':[{'part_type_key':None,'status':'undetermined','text':'不能强行换件','citation_ids':['c1']}], 'limitations':['拒绝无检测强行换件']}
    result=bind_answer(json.dumps(data),{'c1':{'excerpt':'不得仅凭症状换件'}},{},'强行判成必须换','general_diagnostics')
    assert result['answer_status']=='refused' and not result['part_decisions']


def test_localized_hose_damage_does_not_prove_an_unexamined_pump_healthy():
    types={'pump':{'catalog_sku':'x','catalog_name':'水泵','unit':'件','fitment_status':'unknown','required_quantity':None,'case_ids':['CASE-1']}}
    question='维修检查记录：确认水管本体开裂漏液。'
    data={'answer_status':'answered','part_decisions':[{'part_type_key':'pump','status':'not_indicated',
        'text':'记录未指向水泵，本次不支持更换水泵。','citation_ids':['c1'],'basis_quote':'确认水管本体开裂漏液'}]}
    result=bind_answer(json.dumps(data),{'c1':{'unit_id':'CASE-1','excerpt':'未定位本体故障时证据不足'}},types,question,'general_diagnostics')
    assert result['part_decisions'][0]['status']=='undetermined'

