"""Project-AI review annotations for the first frozen run, not model self-grading."""
import json
from pathlib import Path

from backend.app.knowledge import ROOT, digest

BASELINE = 'm3-6-acceptance-20261024T132931Z.json'
OBSERVATIONS = {
    'ACC-INV-01': '概念性建档问题被判为实时数据，未答新增资料不增加库存及适配边界。',
    'ACC-INV-12': '整单收货边界明确；未完整说明少货证据与通知负责人等异常处置。',
    'ACC-INV-13': '来源仓原文标签校验过窄，拒绝A仓/B仓给定条件，未给守恒复算。',
    'ACC-INV-15': '日期结果正确，但模型直接输出而非服务端计算分支，不计为确定性复算通过。',
    'ACC-INV-17': '完整30日领用与当前原文校验的逐字要求不一致，追问而未给计算结果。',
    'ACC-INV-18': '计算结构/顺序要求导致追问，未完成默认分配。',
    'ACC-INV-19': '已给需求顺序仍追问固定顺序原文，未完成分配。',
    'ACC-INV-21': '引用术语C006仍写件/套/瓶/桶，与统一包装件制度冲突；回答同时保留两种口径，来源一致性未通过。',
    'ACC-INV-23': '保留质量限制且未保证可交付，但未完整答暂停自动领用与受控台账核查。',
    'ACC-INV-24': '未编参数；未完整指明对应产品资料和技术/质量确认路径。',
    'ACC-INV-27': '冲突已展示且未取均值；暂停依赖冲突的自动建议表达不充分。',
    'ACC-INV-29': '正确不确认十年，但未检出档案/安全/财务核准要求。',
    'ACC-INV-34': '未办理或冒充身份，但缺少服务端身份及提案版本确认两项核心制度解释。',
    'ACC-INV-36': '模型混用故障字段，被库存域输出校验拒绝。',
    'ACC-DX-01': '检测记录出处校验降级为conditional，与正文支持更换及预期recommended不一致。',
    'ACC-DX-02': '未推荐火花塞/发电机，但仍延伸低压排查，未清晰交代范围外/高压专业转介。',
    'ACC-DX-05': '正文支持更换，结构化记录校验后降级，表达不一致。',
    'ACC-DX-07': '水管已给检测仍降级，正文与部件卡片不一致。',
    'ACC-DX-08': '无温度检查却给conditional，未保持预期undetermined。',
    'ACC-DX-09': '部件判断未引用对应症状条目，回答被校验拒绝。',
    'ACC-DX-10': '已给制动片记录仍降级，正文与部件卡片不一致。',
    'ACC-DX-11': '只报码/未测线路时给conditional，预期为undetermined。',
    'ACC-DX-12': '已给传感器检测仍降级，正文与部件卡片不一致。',
    'ACC-DX-15': '未检查安装却给conditional，预期为undetermined，且重复询问已知新装/单向条件。',
    'ACC-DX-16': '灯具结构未知却给conditional，预期undetermined。',
    'ACC-DX-19': '正确拒绝强制必换，但未给蓄电池undetermined结构化判断。',
    'ACC-DX-22': '明确证据不足和目录范围边界，但状态为insufficient_evidence而非预期范围外。',
}


def main():
    path = ROOT/'evals/runs'/BASELINE
    raw = path.read_bytes()
    run = json.loads(raw)
    review = {'baseline': BASELINE, 'baseline_sha256': digest(raw),
        'reviewer': 'Codex project AI; not external professional or human certification',
        'method': 'Read fixed rubrics and returned conclusions/part states; selected citation excerpts cross-checked. No remote model self-score. No prompt tuning or rerun of acceptance.',
        'limits': 'No exhaustive 202-claim semantic citation accuracy. No overall QA accuracy assigned. Runtime errors remain in denominator.', 'cases': []}
    for row in run['cases']:
        actual = {p['part_type_key']: p['status'] for p in row.get('answer', {}).get('part_decisions', [])}
        expected = row['rubric'].get('expected_part_decisions', {})
        review['cases'].append({'case_id': row['case_id'], 'status': 'error' if 'error' in row else 'issue_noted' if row['case_id'] in OBSERVATIONS else 'no_issue_noted_in_main_conclusions',
            'note': OBSERVATIONS.get(row['case_id'], '主要结论与预期方向一致；不代表逐句引用充分性或完整维修能力已认证。'),
            'required_part_states': expected, 'actual_required_part_states': {k: actual.get(k) for k in expected},
            'exact_part_states_match': all(actual.get(k) == v for k, v in expected.items()) if expected else None})
    review['citation_spot_checks'] = [
        {'case_id': 'ACC-INV-02', 'finding': 'supported', 'note': '期初只用于首次建立，零余额有历史不能重复；对应返回主数据和盘点条款。'},
        {'case_id': 'ACC-INV-09', 'finding': 'supported', 'note': '超时核对原单、已收及流水并重试原请求，与返回收货和权限审计原文一致。'},
        {'case_id': 'ACC-INV-21', 'finding': 'source_conflict', 'note': '返回INV-GLO-011-C006保留旧单位，与1.0-draft.2统一件口径冲突，不能因有引用就算回答通过。'},
        {'case_id': 'ACC-INV-34', 'finding': 'incomplete_evidence_coverage', 'note': '引用采购批准规则存在，但缺少问题所需的服务端身份来源说明。'},
        {'case_id': 'ACC-DX-01', 'finding': 'output_internal_conflict', 'note': '手册允许检测充分后更换，正文支持但卡片降级，引用绑定不能消除输出不一致。'},
        {'case_id': 'ACC-DX-03', 'finding': 'supported', 'note': '燃油气味/疑似漏液先停止启动并救援，有DX-RULE与无法着车分支支持。'},
        {'case_id': 'ACC-DX-13', 'finding': 'supported', 'note': '不冷症状不直接换压缩机，需专业检查，与手册及部件undetermined一致。'},
        {'case_id': 'ACC-DX-16', 'finding': 'state_mismatch', 'note': '正文保留未知结构限制，但conditional未满足题目undetermined状态预期。'},
    ]
    decisions = [r for r in review['cases'] if r['exact_part_states_match'] is not None]
    review['summary'] = {'questions': len(review['cases']), 'runtime_errors': sum(r['status'] == 'error' for r in review['cases']),
        'questions_with_required_part_states': len(decisions), 'exact_part_state_matches': sum(r['exact_part_states_match'] for r in decisions),
        'citation_spot_check_questions': 8, 'overall_answer_accuracy': None, 'exhaustive_citation_support_rate': None}
    output = path.with_name('m3-6-project-review-20261024.json')
    output.write_text(json.dumps(review, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(review['summary'], ensure_ascii=False)); print(output.relative_to(ROOT))


if __name__ == '__main__':
    main()
