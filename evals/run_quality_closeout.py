"""Real-model development regression; never overwrite fixed historical baselines."""
import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from backend.app.agent import run_agent
from backend.app.assistant_api import ChatMessage
from backend.app.config import Settings
from backend.app.knowledge import ROOT, KnowledgeError
from backend.app.knowledge_api import KnowledgeQuery, KnowledgeRuntime, run_query
from evals.compare_agents import fixture


async def main(index_root=ROOT):
    settings = Settings()
    runtime = KnowledgeRuntime(settings,index_root)
    runtime.start()
    if runtime.index is None: raise runtime.error
    report = {'scope':'exposed_issue_development_regression_not_independent_acceptance', 'created_at':datetime.now(UTC).isoformat(),
        'model':settings.model_id, 'embedding_model':settings.embedding_model_id,
        'generation_id':runtime.index.report['generation_id'], 'cases':[]}
    questions = [
        ('Q-UNIT','inventory_policies','本系统整桶、整套和整瓶的库存，分别应该用什么单位？是否要换算数量？',['INV-GLO-011']),
        ('Q-CONCEPT','inventory_policies','新增一种备件资料后，是否就自动产生库存？这是制度和流程问题，不查询实时库存。',None),
        ('Q-MATH','inventory_policies','假设A仓现有15件，B仓现有3件，A仓向B仓调拨6件，无其他库存流转。发出后和收货后各仓现有及在途是多少？',None),
        ('Q-MATH30','inventory_policies','假设，完整30日领用60件，交期4天，安全库存3件，可用1件，采购待收2件，调拨在途0件，请计算目标库存与补货缺口。',None),
        ('Q-DIAG','general_diagnostics','车辆空调出风量正常，但制冷不足，还没有检测记录。仅凭这些能否判断压缩机必须更换？',None),
    ]
    try:
        for cid,kb,question,ids in questions:
            started=time.monotonic()
            try:
                answer=await run_query(runtime,KnowledgeQuery(question=question,knowledge_base=kb,knowledge_scope='draft_preview',document_ids=ids),ask=True)
                report['cases'].append({'id':cid,'question':question,'answer':answer,'elapsed_seconds':round(time.monotonic()-started,3)})
            except KnowledgeError as error:
                report['cases'].append({'id':cid,'question':question,'error_code':error.code,'detail':error.detail})
            print(json.dumps({'case':cid,'status':report['cases'][-1].get('answer',{}).get('answer_status',report['cases'][-1].get('error_code'))},ensure_ascii=False),flush=True)
        with fixture(settings) as (engine,f):
            question=f"先核对收费维修领用条件：拟从评测B仓领用评测蓄电池9件，仓库ID {f['target_id']}、备件ID {f['part_id']}，用途paid_repair。库存不足时说明缺口并提供可选处理方式，不办理其他业务，不创建提案。"
            started=time.monotonic()
            try:
                answer=await run_agent(runtime,engine,f['user_id'],uuid4().hex,uuid4().hex,ChatMessage(message=question,client_request_id=uuid4().hex,allow_web=False),[],mode='multi')
                report['cases'].append({'id':'Q-SHORTAGE','question':question,'answer':answer,'elapsed_seconds':round(time.monotonic()-started,3),
                    'checks':{'has_shortage':any(i['code']=='insufficient_stock' for i in answer.get('business_issues',[])),
                        'asks_employee_choice':bool(answer.get('follow_up_questions')), 'no_proposals':not answer.get('proposals'),'multi_agent':answer['agent_mode']=='multi'}})
            except (KnowledgeError,HTTPException) as error:
                report['cases'].append({'id':'Q-SHORTAGE','detail':error.detail})
            print(json.dumps({'case':'Q-SHORTAGE','checks':report['cases'][-1].get('checks')},ensure_ascii=False),flush=True)
    finally:
        runtime.close()
        target=ROOT/'evals/runs'/('quality-closeout-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(target)


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser();parser.add_argument('--index-root',type=Path,default=ROOT)
    args=parser.parse_args();asyncio.run(main(args.index_root))
