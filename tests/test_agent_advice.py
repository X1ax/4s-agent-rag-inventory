import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.agent import worker
from backend.app.agent_tools import ToolContext
from test_agent import actor
from test_m1 import resources, system, opening_body
from test_m2 import call


@pytest.mark.parametrize('field', ['follow_up_questions', 'message'])
@pytest.mark.parametrize('text', ['是否改为按可用量（3件）调拨？', '是否先调拨3件，其余缺口再采购？'])
@pytest.mark.parametrize('other_part_calculated', [False, True])
def test_available_stock_is_not_recommended_as_transfer_surplus_in_uncited_text(system, monkeypatch, field, text, other_part_calculated):
    warehouse, parts = resources(system, 1)
    call(system, '/api/inventory/openings', opening_body(warehouse, parts))
    runtime = type('Runtime', (), {'root': Path(__file__).resolve().parents[1], 'settings': system[4]})()
    context = ToolContext(runtime, system[2], actor(system), uuid4().hex, uuid4().hex, '缺货怎么办', allow_web=False)
    calls = []

    async def scripted(settings, messages, tools):
        calls.append(messages)
        if len(calls) == 1:
            name, args = 'stock', {'part_id': parts[0]['id'], 'warehouse_id': warehouse['id']}
        elif len(calls) == 2:
            name, args = 'finish', {field: [text] if field == 'follow_up_questions' else text}
        else:
            assert '目标库存' in messages[-1]['content']
            name, args = 'finish', {'follow_up_questions': ['是否需要查询其他仓库或准备采购？']}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(len(calls)), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}, {'total_tokens': 1}

    monkeypatch.setattr('backend.app.agent.chat_tools', scripted)
    prior = {'citations': [{'citation_id': 'other', 'document_id': 'TOOL-replenishment',
        'excerpt': json.dumps({'calculation_status': 'calculated', 'part_id': -1})}]} if other_part_calculated else None
    answer = asyncio.run(worker(context, '缺货怎么办', 'business', prior=prior))
    assert len(calls) == 3
    assert answer['follow_up_questions'] == ['是否需要查询其他仓库或准备采购？']
    assert not context.proposals
