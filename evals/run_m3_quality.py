"""Evaluate frozen quality inputs; expected answers never enter model prompts."""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from backend.app.config import ROOT, Settings
from backend.app.knowledge import KnowledgeError, digest
from backend.app.knowledge_api import KnowledgeQuery, KnowledgeRuntime, run_query


async def main(index_root, suite_path=ROOT/'evals/m3-quality-independent.json'):
    suite=json.loads(suite_path.read_text(encoding='utf-8'))
    settings=Settings();runtime=KnowledgeRuntime(settings,index_root);runtime.start()
    suite_sha=digest(suite_path.read_bytes())
    used=suite.get('scope','').startswith('exposed') or any(json.loads(p.read_text(encoding='utf-8')).get('suite_sha256')==suite_sha for p in (ROOT/'evals/runs').glob('m3-quality-*.json'))
    output=ROOT/'evals/runs'/(('m3-quality-retest-' if used else 'm3-quality-independent-')+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    report={'scope':'exposed_quality_regression_not_independent' if used else suite['scope'],'suite_sha256':suite_sha,'model':settings.model_id,
        'embedding':settings.embedding_model_id,'code_hashes':{p:digest((ROOT/p).read_bytes()) for p in ['backend/app/knowledge_answers.py','backend/app/knowledge_math.py','backend/app/knowledge_api.py']},'cases':[]}
    try:
        if runtime.index is None:raise runtime.error
        report['generation_id']=runtime.index.report['generation_id'];report['source_fingerprint']=runtime.index.report['fingerprint']
        for case in suite['cases']:
            row={'id':case['id'],'question':case['question'],'rubric':case};report['cases'].append(row)
            try:
                answer=await run_query(runtime,KnowledgeQuery(question=case['question'],knowledge_base=case['kb'],knowledge_scope='draft_preview'),ask=True)
                row['answer']=answer
                text='\n'.join([c['text'] for c in answer['claims']]+answer['limitations']+answer['follow_up_questions'])
                states={p['part_type_key']:p['status']for p in answer['part_decisions']}
                checks={'status':answer['answer_status'] in case['statuses'],'no_write_tools':True,
                    'points':all(any(term in text for term in variants)for variants in case.get('points',[])),
                    'parts':all(states.get(k)==v for k,v in case.get('parts',{}).items()),
                    'catalog_units':all(p['catalog']['unit']=='件' for p in answer['part_decisions'])}
                if case.get('no_calculation'):checks['no_calculation']=not answer.get('calculation')
                if case.get('forbid_recommended'):checks['no_unproved_recommendation']='recommended' not in states.values()
                if case.get('calculation'):
                    actual=answer.get('calculation') or {};expected=case['calculation']
                    checks['calculation']=actual.get('kind')==expected['kind'] and actual.get('result')==expected['result']
                row['automated_checks']=checks;row['automated_pass']=all(checks.values())
            except KnowledgeError as error:row.update(error={'code':error.code,'detail':error.detail},automated_pass=False)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(row['id'],row['automated_pass'],row.get('automated_checks',row.get('error')),flush=True)
    finally:
        runtime.close();report['summary']={'attempted':len(report['cases']),'automated_pass':sum(r['automated_pass']for r in report['cases']),
            'semantic_review':'requires_project_ai_source_review_not_model_self_score'}
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(output,flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('index_root',type=Path);parser.add_argument('--suite',type=Path,default=ROOT/'evals/m3-quality-independent.json')
    args=parser.parse_args();asyncio.run(main(args.index_root,args.suite))
