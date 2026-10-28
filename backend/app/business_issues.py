"""Business validation facts and available next steps; never an execution command."""
from fastapi import HTTPException


class BusinessIssue(HTTPException):
    def __init__(self, code, detail, *, facts=None, options=(), status_code=409):
        super().__init__(status_code, detail)
        self.issue = {'code': code, 'summary': detail, 'facts': facts or {},
            'options': [{'label': label, 'prompt': prompt} for label, prompt in options],
            'notice': '选择处理方式后仍需补齐必要条件并提交审核；当前未执行业务。'}


def shortage(lines, balances, source_id, parts, action):
    available = {r['part_id']: int(r['available']) for r in balances if r['warehouse_id'] == source_id}
    missing = [{'part_id': r['part_id'], 'sku': parts[r['part_id']].sku, 'name': parts[r['part_id']].name,
        'requested': r['quantity'], 'available': available.get(r['part_id'], 0),
        'gap': r['quantity']-available.get(r['part_id'], 0)} for r in lines if r['quantity'] > available.get(r['part_id'], 0)]
    if not missing:
        return
    summary = '；'.join(f"{r['name']}需求{r['requested']}件，可用{r['available']}件，缺口{r['gap']}件" for r in missing)
    needs = '、'.join(f"{r['name']}（{r['sku']}）{r['gap']}件" for r in missing)
    raise BusinessIssue('insufficient_stock', summary, facts={'warehouse_id': source_id, 'items': missing, 'action': action}, options=[
        ('核查跨仓调拨', f'请为仓库ID {source_id}核查{needs}的其他仓库存和可调条件，先给方案，不创建或发出单据。'),
        ('准备缺口采购', f'请为仓库ID {source_id}准备{needs}的采购草稿提案；先查询合格供应商，供应商和成交价未确定时向我确认，不自动审核或入库。'),
        ('调整本次需求', '我想调整刚才的业务需求，请先列出原备件、需求量与可用量，等我提供新数量后重新核对，不自动缩减或部分执行。')])
