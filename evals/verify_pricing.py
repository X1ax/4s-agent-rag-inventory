"""Opt-in live-model regression via local server; no proposal decisions or stock writes."""
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from backend.app.config import ROOT, Settings


CASES = [
    ('actual_profit', '仅查询模拟主仓机油滤清器在2026-09-01至2026-09-15的收费结算金额、实际出库成本、收费毛利和质保成本，说明统计口径，不办理任何单据。'),
    ('missing_issue_purpose', '请帮我办理从模拟主仓领用1件机油滤清器，先核对必要条件；我尚未说明维修用途。'),
    ('pricing_policy', '仅咨询本地制度：收费维修和质保维修怎样确定结算价和出库成本？供货价由谁维护？请引用资料，不查询实物库存，也不办理业务。'),
]


def main():
    settings = Settings()
    env = dotenv_values(ROOT/'.env')
    report = {'purpose': 'synthetic development regression; not independent evaluation',
        'created_at': datetime.now(UTC).isoformat(), 'model': settings.model_id, 'cases': []}
    with httpx.Client(base_url='http://127.0.0.1:8000/api', timeout=145) as client:
        login = client.post('/auth/login', json={'username': 'demo_admin', 'password': env['DEMO_ADMIN_PASSWORD']})
        login.raise_for_status()
        client.headers['Authorization'] = 'Bearer '+login.json()['access_token']
        for case_id, question in CASES:
            thread = client.post('/assistant/threads')
            thread.raise_for_status()
            thread_id = thread.json()['id']
            client.patch('/assistant/threads/'+thread_id, json={'title': '定价验收·'+case_id})
            response = client.post('/assistant/threads/'+thread_id+'/messages', json={
                'message': question, 'client_request_id': uuid4().hex, 'allow_web': False})
            row = {'case_id': case_id, 'thread_id': thread_id, 'question': question, 'http_status': response.status_code,
                'response': response.json()}
            answer = row['response'].get('answer', {})
            tools = [entry['tool'] for entry in answer.get('tool_trace', [])]
            checks = {'no_proposal': not answer.get('proposals'), 'answered': response.status_code==200,
                'has_evidence': bool(answer.get('citations'))}
            if case_id == 'actual_profit':
                checks['actual_profit_tool'] = 'profits' in tools
                checks['explicit_dates_used'] = any(t['tool']=='profits' and t.get('status')=='ok'
                    and t.get('arguments',{}).get('date_from')=='2026-09-01'
                    and t.get('arguments',{}).get('date_to')=='2026-09-15' for t in answer.get('tool_trace',[]))
                checks['no_tool_parameter_errors'] = not any(t.get('status')=='error' for t in answer.get('tool_trace',[]))
            if case_id == 'missing_issue_purpose':
                questions = ' '.join(answer.get('follow_up_questions', []))+answer.get('message', '')
                checks['asks_issue_purpose'] = '收费' in questions and '质保' in questions
            if case_id == 'pricing_policy':
                checks['updated_document_cited'] = any(c.get('document_id') in {'INV-MDM-002','INV-ISS-005','INV-ANA-009'}
                    and c.get('document_version') in {'1.0-draft.2','1.0-draft.3','1.0-draft.4'} for c in answer.get('citations', []))
            row['checks'] = checks
            report['cases'].append(row)
            print(json.dumps({'case': case_id, 'checks': checks, 'tools': tools}, ensure_ascii=False), flush=True)
    target = ROOT/'evals/runs'/('pricing-development-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(str(target))


if __name__ == '__main__':
    main()
