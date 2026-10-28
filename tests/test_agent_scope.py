import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agent import run_agent
from backend.app.knowledge_api import empty_answer


@pytest.mark.parametrize('mode', ['single', 'multi'])
@pytest.mark.parametrize('stock_succeeded', [False, True])
def test_document_only_limits_are_scoped_when_real_stock_tool_succeeds(monkeypatch, mode, stock_succeeded):
    document_limit = '本服务只读取文档，未查询实时库存或办理业务单据。'
    technical_limit = '当前未提供检测记录，不能判断蓄电池需要更换。'

    async def dispatch(*args, **kwargs):
        return json.dumps({'route': 'combined', 'task': '故障与库存',
            'diagnostic_task': '仅故障排查', 'business_task': '仅查询库存'}), {}

    async def specialist(ctx, question, role, **kwargs):
        result = empty_answer('answered', '业务查询来自本轮工具快照，执行前仍须重新校验。')
        result['usage'] = {}
        if role in {'single', 'diagnostics'}:
            ctx.knowledge_answers.append({'limitations': [document_limit, technical_limit]})
            result['limitations'].extend([document_limit, technical_limit])
        if role in {'single', 'business'}:
            ctx.trace.append({'tool': 'stock', 'status': 'ok' if stock_succeeded else 'error'})
            result['claims'] = [{'text': '主仓当前现有6件。', 'citation_ids': ['stock-evidence']}] if stock_succeeded else []
        return result

    monkeypatch.setattr('backend.app.agent.chat_json', dispatch)
    monkeypatch.setattr('backend.app.agent.worker', specialist)
    runtime = SimpleNamespace(root=Path.cwd(), settings=SimpleNamespace())
    message = SimpleNamespace(message='排查故障并查询库存', allow_web=False, knowledge_scope='draft_preview')
    answer = asyncio.run(run_agent(runtime, None, 1, 'thread', 'turn', message, [], mode=mode))
    if stock_succeeded:
        assert document_limit not in answer['limitations']
        assert any('仅限文档检索' in note and document_limit in note for note in answer['limitations'])
        assert any('本轮已调用库存工具' in note for note in answer['limitations'])
        assert answer['claims'] == [{'text': '主仓当前现有6件。', 'citation_ids': ['stock-evidence']}]
    else:
        assert document_limit in answer['limitations']
        assert not any('本轮已调用库存工具' in note for note in answer['limitations'])
    assert any(technical_limit in note for note in answer['limitations'])
