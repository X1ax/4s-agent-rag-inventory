"""Project review of the final paired development run; no automatic semantic accuracy claim."""
import json
import re
from decimal import Decimal

from backend.app.knowledge import ROOT, digest

BASELINE = 'm4-agent-comparison-20261024T140005Z.json'


def main():
    path = ROOT/'evals/runs'/BASELINE
    raw = path.read_bytes(); run = json.loads(raw)
    first = run['cases'][0]['answer']['tool_trace'][0]['result']['data']
    part = first['parts'][0]
    warehouses = {r['name']: r['id'] for r in first['warehouses']}
    source, target = warehouses['评测A仓'], warehouses['评测B仓']
    supplier = part['suppliers'][0]['id']
    result = {'run': BASELINE, 'sha256': digest(raw), 'reviewer': 'Codex project AI; not a human or external expert',
        'scope': 'Every task main conclusion compared with saved tool results and relevant source excerpts; not a frozen semantic grading model.',
        'parameter_rule': 'Exact part/warehouse/document/supplier IDs and proposal quantity/price/date/action; catalog search wording is not scored as ID/amount correctness.',
        'cases': [], 'summary': {}}
    for row in run['cases']:
        answer = row['answer']; pool = {c['citation_id']: c for c in answer['citations']}
        bound = all(k in pool for field in ['claims', 'fault_types', 'checks', 'part_decisions'] for c in answer[field] for k in c['citation_ids'])
        checks = []
        for trace in answer['tool_trace']:
            name, args = trace['tool'], trace.get('arguments', {})
            if name == 'stock':
                ok = args.get('part_id') == part['id'] and args.get('warehouse_id') in {None, source, target}
            elif name == 'documents':
                ok = args.get('document_id') == int(re.search(r'#(\d+)', row['task']).group(1))
            elif name == 'prepare_proposal':
                payload = args['payload']; line = payload['lines'][0]
                expected_kind = 'purchase' if row['case_id'] == 'AG-04' else 'transfer'
                ok = (args['action'] == 'create' and payload['kind'] == expected_kind and payload['warehouse_id'] == source
                    and payload['business_date'] == run['created_at'][:10]
                    and len(payload['lines']) == 1 and line['part_id'] == part['id']
                    and line['quantity'] == (2 if expected_kind == 'purchase' else 3)
                    and (line.get('supplier_id') == supplier and Decimal(str(line.get('unit_price'))) == Decimal('400.00') if expected_kind == 'purchase'
                        else payload.get('target_warehouse_id') == target))
            else:
                continue
            checks.append({'tool': name, 'matched': ok})
        notes = []
        if row['case_id'] == 'AG-01':
            notes.append('数量与口径有实际快照支持；回答将UTC获取时刻14:00写成无时区中文时间，需改善显示与时区表达。')
        if row['case_id'] == 'AG-03':
            notes.append('单据数值与实际记录一致，但另用ID文本调用仅按名称/SKU/别名检索的catalog，属于低效搜索，不计为目录搜索正确率通过。')
        if row['case_id'] == 'AG-07':
            notes.append('故障与库存两条链均完成；未检测时conditional与undetermined区分仍有基线缺口，不能将任务完成率当诊断质量通过率。')
        if row['case_id'] == 'AG-08' and row['mode'] == 'multi':
            notes.append('目标压缩机保持undetermined，但额外冷凝器为conditional，状态区分仍需改进。')
        if row['case_id'] == 'AG-09' and row['mode'] == 'single':
            notes.append('正确拒绝1000件提案；追问用可用减安全库存给最多11件，未调用目标库存计算，不能作为已验证可调量。')
        if row['case_id'] == 'AG-09' and row['mode'] == 'multi':
            notes.append('正确拒绝1000件提案；追问可用14件部分调拨只是可用上界，尚未验证目标库存/可调余量，不能当自动调拨建议。')
        result['cases'].append({'case_id': row['case_id'], 'mode': row['mode'], 'tool_parameter_checks': checks,
            'citation_binding_pass': bound, 'main_facts': 'core_numbers_and_evidence_checked',
            'quality_notes': notes, 'full_semantic_support_pass': None})
    for mode in ['single', 'multi']:
        rows = [r for r in result['cases'] if r['mode'] == mode]
        checks = [c for r in rows for c in r['tool_parameter_checks']]
        result['summary'][mode] = {'exact_target_parameter_calls': len(checks), 'exact_target_parameter_matches': sum(c['matched'] for c in checks),
            'citation_binding_tasks': len(rows), 'citation_binding_passes': sum(r['citation_binding_pass'] for r in rows),
            'full_semantic_support_rate': None, 'notes': '引用身份/存在性与核心事实核对分开；不以绑定通过声称全部语义、诊断状态和追问建议通过。'}
    output = path.with_name('m4-agent-project-review-20261024.json')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result['summary'], ensure_ascii=False)); print(output.relative_to(ROOT))


if __name__ == '__main__':
    main()
