import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.assistant_store import AssistantStore
from backend.app.config import Settings
from backend.app.knowledge import KnowledgeError
from backend.app.main import create_app
from backend.app.security import current_user
from backend.app.web_search import public_url, tavily_search, validate_public_query
from test_knowledge_answers import app_system, mocked_ai


@pytest.fixture
def chat_ai(monkeypatch, mocked_ai):
    calls = []
    async def chat(settings, messages, **kwargs):
        body = json.loads(messages[1]['content'])
        calls.append(body)
        if 'title_context' in body:
            return json.dumps({'title':'库存业务咨询'}), {'total_tokens':3}
        if 'current_message' in body:
            question = body['current_message']
            kind = 'conversation' if question == '你好' else 'web_search' if '联网' in question else 'knowledge'
            return json.dumps({'kind': kind, 'knowledge_base': 'inventory_policies', 'standalone_question': '调拨未收货时的库存口径',
                'search_query': '汽车蓄电池 检测 官方资料' if kind == 'web_search' else '', 'reply': '你好，可以聊聊你要处理的库存业务。'}), {'total_tokens': 5}
        evidence = body['evidence'][0]
        return json.dumps({'answer_status': 'answered', 'claims': [{'text': '目标仓未收货时不能使用该笔在途数量。', 'citation_ids': [evidence['citation_id']]}]}), {'total_tokens': 6}
    monkeypatch.setattr('backend.app.assistant_api.chat_json', chat)
    monkeypatch.setattr('backend.app.knowledge_api.chat_json', chat)
    return calls


def send(client, tid, question, key=None, **extra):
    return client.post(f'/api/assistant/threads/{tid}/messages', json={'message': question, 'client_request_id': key or uuid4().hex, **extra})


def test_conversation_followup_is_persisted_and_reloaded(app_system, chat_ai):
    app, client, root, _ = app_system
    thread = client.post('/api/assistant/threads').json()
    tid = thread['id']
    assert send(client, tid, '你好').json()['answer']['message'].startswith('你好')
    assert send(client, tid, '调拨发出但未收货怎么办').json()['answer']['claims']
    assert send(client, tid, '那目标仓呢？').status_code == 200
    intent = next(c for c in reversed(chat_ai) if c.get('current_message') == '那目标仓呢？')
    assert any('调拨发出' in m['content'] and m['role'] == 'user' for m in intent['history'])
    history = client.get(f'/api/assistant/threads/{tid}').json()
    assert len(history['turns']) == 3 and all(t['status'] == 'completed' for t in history['turns'])
    restored = AssistantStore(root).history(42, tid)
    assert restored['turns'] == history['turns']
    assert any(t['id'] == tid for t in client.get('/api/assistant/threads').json()['items'])


def test_first_success_summarizes_title_once_and_keeps_it(app_system, chat_ai):
    _,client,root,_=app_system
    tid=client.post('/api/assistant/threads').json()['id']
    before=len([c for c in chat_ai if 'title_context' in c])
    question='请解释采购入库前需要核对哪些资料与实收信息'
    assert send(client,tid,question).status_code==200
    assert client.get(f'/api/assistant/threads/{tid}').json()['title']=='库存业务咨询'
    assert send(client,tid,'那调拨又应该怎么处理').status_code==200
    assert len([c for c in chat_ai if 'title_context' in c])==before+1
    assert AssistantStore(root).history(42,tid)['title']=='库存业务咨询'


def test_manual_title_including_default_word_is_never_replaced(app_system,chat_ai):
    _,client,_,_=app_system
    tid=client.post('/api/assistant/threads').json()['id']
    client.patch(f'/api/assistant/threads/{tid}',json={'title':'新会话'})
    before=len([c for c in chat_ai if 'title_context' in c])
    assert send(client,tid,'你好').status_code==200
    assert client.get(f'/api/assistant/threads/{tid}').json()['title']=='新会话'
    assert len([c for c in chat_ai if 'title_context' in c])==before


def test_title_failure_does_not_lose_successful_reply(app_system,chat_ai,monkeypatch):
    from backend.app import assistant_api
    original=assistant_api.chat_json
    async def failed_title(settings,messages,**kwargs):
        if 'title_context' in json.loads(messages[1]['content']):raise KnowledgeError('chat_timeout','模拟标题生成失败')
        return await original(settings,messages,**kwargs)
    monkeypatch.setattr(assistant_api,'chat_json',failed_title)
    client=app_system[1];tid=client.post('/api/assistant/threads').json()['id']
    assert send(client,tid,'你好').json()['answer']['message'].startswith('你好')
    history=client.get(f'/api/assistant/threads/{tid}').json()
    assert history['turns'][0]['status']=='completed' and history['title']=='你好'


def test_manual_rename_during_title_request_wins(app_system,chat_ai,monkeypatch):
    from backend.app import assistant_api
    original=assistant_api.chat_json;app,client,_,_=app_system
    tid=client.post('/api/assistant/threads').json()['id']
    async def rename_during_title(settings,messages,**kwargs):
        if 'title_context' in json.loads(messages[1]['content']):app.state.assistant.rename(42,tid,'我的业务记录')
        return await original(settings,messages,**kwargs)
    monkeypatch.setattr(assistant_api,'chat_json',rename_during_title)
    assert send(client,tid,'你好').status_code==200
    assert client.get(f'/api/assistant/threads/{tid}').json()['title']=='我的业务记录'


def test_legacy_thread_schema_preserves_existing_titles(tmp_path):
    import sqlite3
    (tmp_path/'data').mkdir()
    with sqlite3.connect(tmp_path/'data/assistant.sqlite3') as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
        db.execute("INSERT INTO threads VALUES ('old',42,'原有手动标题','2026-10-24','2026-10-24')")
    store=AssistantStore(tmp_path);store.start()
    assert store.history(42,'old')['title']=='原有手动标题' and not store.needs_title(42,'old')
    assert store.needs_title(42,store.create(42)['id'])


def test_repeated_request_reuses_answer_and_modified_parameters_conflict(app_system, chat_ai):
    _, client, _, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']; key = uuid4().hex
    assert send(client, tid, '你好', key).status_code == 200
    calls = len(chat_ai)
    again = send(client, tid, '你好', key)
    assert again.json()['reused'] and len(chat_ai) == calls
    changed = send(client, tid, '不同内容', key)
    assert changed.status_code == 409 and changed.json()['error_code'] == 'turn_conflict'


def test_other_user_cannot_read_or_send_to_thread(app_system, chat_ai):
    app, client, _, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=43, role='manager')
    try:
        assert client.get(f'/api/assistant/threads/{tid}').status_code == 404
        assert client.delete(f'/api/assistant/threads/{tid}').status_code == 404
        assert client.patch(f'/api/assistant/threads/{tid}', json={'title': '其他人不能修改'}).status_code == 404
        assert send(client, tid, '你好').status_code == 404
        assert tid not in [t['id'] for t in client.get('/api/assistant/threads').json()['items']]
    finally:
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=42, role='manager')


def test_pending_turn_is_not_duplicated_and_restart_marks_interrupted(tmp_path):
    store = AssistantStore(tmp_path); store.start()
    tid = store.create(1)['id']; key = uuid4().hex
    turn, _ = store.begin(1, tid, key, '问题', 'draft_preview')
    with pytest.raises(KnowledgeError, match='正在处理'):
        store.begin(1, tid, key, '问题', 'draft_preview')
    with pytest.raises(KnowledgeError):
        store.begin(1, tid, uuid4().hex, '另一问题', 'draft_preview')
    reopened = AssistantStore(tmp_path); reopened.start(recover=True)
    rows = reopened.history(1, tid)['turns']
    assert rows[0]['status'] == 'failed' and rows[0]['error_code'] == 'interrupted'
    retry, _ = reopened.begin(1, tid, key, '问题', 'draft_preview')
    assert retry == turn


def test_web_disabled_does_not_contact_tavily(app_system, chat_ai, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Tavily must not be contacted')
    monkeypatch.setattr('backend.app.assistant_api.tavily_search', forbidden)
    client = app_system[1]; tid = client.post('/api/assistant/threads').json()['id']
    result = send(client, tid, '联网查资料', allow_web=False).json()['answer']
    assert result['answer_status'] == 'needs_clarification' and '关闭' in str(result['limitations'])


def test_tavily_fixed_endpoint_key_snippets_and_url_validation():
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == 'https://api.tavily.com/search'
        assert request.headers['Authorization'] == 'Bearer search-only'
        payload = json.loads(request.content)
        assert payload['include_raw_content'] is False and payload['include_answer'] is False
        assert payload['max_results'] == 5 and 'api_key' not in payload
        return httpx.Response(200, json={'results': [{'title': '公开资料', 'url': 'https://example.org/reference', 'content': '公开摘录'},
            {'title': '非法', 'url': 'javascript:alert(1)', 'content': '恶意网页'}, {'title': '本机', 'url': 'http://127.0.0.1/internal', 'content': '本机'}], 'usage': {'credits': 1}})
    settings = Settings(_env_file=None, tavily_api_key='search-only', api_key='chat-only')
    pool, usage = asyncio.run(tavily_search(settings, '汽车蓄电池 检测 官方资料', transport=httpx.MockTransport(respond)))
    assert len(calls) == 1 and len(pool) == 1 and usage == {'search_requests': 1, 'credits': 1}
    source = next(iter(pool.values()))
    assert source['source_type'] == 'web_search' and source['excerpt'] == '公开摘录'


@pytest.mark.parametrize('query', ['联系13800138000', 'user@example.org', 'API_KEY=secret', 'VIN: L1234567890123456', 'tvly-this-is-a-secret'])
def test_private_search_terms_are_not_sent(query):
    with pytest.raises(KnowledgeError):
        validate_public_query(query)


def test_timeout_failure_saved_and_manual_retry_reuses_same_turn(app_system, chat_ai, monkeypatch):
    client = app_system[1]; tid = client.post('/api/assistant/threads').json()['id']; key = uuid4().hex
    async def timeout(*args, **kwargs):
        raise KnowledgeError('chat_timeout', '测试超时')
    with monkeypatch.context() as patch:
        patch.setattr('backend.app.assistant_api.chat_json', timeout)
        result = send(client, tid, '你好', key)
        assert result.status_code == 504
    history = client.get(f'/api/assistant/threads/{tid}').json()['turns']
    assert len(history) == 1 and history[0]['status'] == 'failed'
    assert send(client, tid, '你好', key).status_code == 200
    assert len(client.get(f'/api/assistant/threads/{tid}').json()['turns']) == 1


def test_unavailable_chat_storage_does_not_block_business(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('unwritable private location')
    monkeypatch.setattr(AssistantStore, 'start', unavailable)
    app = create_app(Settings(_env_file=None), knowledge_root=tmp_path)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/assistant/threads').status_code == 503


def test_web_answer_binds_only_search_citations_and_preserves_source_kind(app_system, chat_ai, monkeypatch):
    pool = {'web-source': {'citation_id': 'web-source', 'chunk_id': 'web-source', 'source_type': 'web_search',
        'title': '测试官方网页', 'url': 'https://example.org/guide', 'excerpt': '测试公开摘录', 'unit_id': None,
        'unit_ids': [], 'native_format': 'web', 'document_id': 'WEB-1', 'document_version': '2026-10-24', 'section_path': []}}
    async def search(*args, **kwargs):
        return pool, {'search_requests': 1, 'credits': 1}
    monkeypatch.setattr('backend.app.assistant_api.tavily_search', search)
    client = app_system[1]; tid = client.post('/api/assistant/threads').json()['id']
    response = send(client, tid, '联网查资料')
    assert response.status_code == 200
    answer = response.json()['answer']
    assert answer['citations'][0]['source_type'] == 'web_search'
    assert answer['search_sources'] == list(pool.values()) and answer['usage']['search']['credits'] == 1


def test_non_knowledge_intent_can_omit_unused_domain(app_system, mocked_ai, monkeypatch):
    async def greeting(*args, **kwargs):
        return json.dumps({'kind': 'conversation', 'knowledge_base': '', 'standalone_question': '', 'search_query': '', 'reply': '你好，有什么需要一起处理？'}), {}
    monkeypatch.setattr('backend.app.assistant_api.chat_json', greeting)
    client = app_system[1]; tid = client.post('/api/assistant/threads').json()['id']
    assert send(client, tid, '你好').json()['answer']['message'].startswith('你好')
    assert not public_url('http://127.1/internal')


def test_delete_own_conversation_removes_turns_and_list_entry(app_system, chat_ai):
    app, client, _, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']
    assert send(client, tid, '你好').status_code == 200
    assert client.delete(f'/api/assistant/threads/{tid}').json() == {'deleted': True}
    assert client.get(f'/api/assistant/threads/{tid}').status_code == 404
    assert tid not in [t['id'] for t in client.get('/api/assistant/threads').json()['items']]
    with app.state.assistant.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM turns WHERE thread_id=?', (tid,)).fetchone()[0] == 0


def test_pending_conversation_cannot_be_deleted(app_system):
    app, client, _, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']
    turn, _ = app.state.assistant.begin(42, tid, uuid4().hex, '处理中', 'draft_preview')
    response = client.delete(f'/api/assistant/threads/{tid}')
    assert response.status_code == 409 and response.json()['error_code'] == 'turn_in_progress'
    app.state.assistant.finish(turn, error=KnowledgeError('interrupted', '测试停止'))
    assert client.delete(f'/api/assistant/threads/{tid}').status_code == 200


def test_rename_thread_changes_list_and_survives_reopen(app_system):
    _, client, root, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']
    assert client.patch(f'/api/assistant/threads/{tid}', json={'title': ' 调拨流程咨询 '}).json()['title'] == '调拨流程咨询'
    assert AssistantStore(root).history(42, tid)['title'] == '调拨流程咨询'
    assert next(t for t in client.get('/api/assistant/threads').json()['items'] if t['id'] == tid)['title'] == '调拨流程咨询'


@pytest.mark.parametrize('title', ['   ', 'a'*81])
def test_bad_thread_titles_do_not_change_existing_name(app_system, title):
    _, client, _, _ = app_system
    tid = client.post('/api/assistant/threads').json()['id']
    assert client.patch(f'/api/assistant/threads/{tid}', json={'title': title}).status_code == 422
    assert client.get(f'/api/assistant/threads/{tid}').json()['title'] == '新会话'


@pytest.mark.parametrize('route_kind', ['conversation', 'web_search'])
def test_fact_summary_never_returns_uncited_conversation_from_history(app_system, chat_ai, monkeypatch, route_kind):
    pool = {'web-source': {'citation_id': 'web-source', 'chunk_id': 'web-source', 'source_type': 'web_search',
        'title': '测试网页', 'url': 'https://example.org/guide', 'excerpt': '应由维修人员检查', 'unit_id': None,
        'unit_ids': [], 'native_format': 'web', 'document_id': 'WEB-1', 'document_version': '2026-10-24', 'section_path': []}}
    async def search(*args, **kwargs):
        return pool, {'search_requests': 1, 'credits': 1}
    monkeypatch.setattr('backend.app.assistant_api.tavily_search', search)
    client = app_system[1]; tid = client.post('/api/assistant/threads').json()['id']
    assert send(client, tid, '联网查资料').status_code == 200
    async def summary(settings, messages, **kwargs):
        data = json.loads(messages[1]['content'])
        if 'current_message' in data:
            return json.dumps({'kind': route_kind, 'search_query': 'battery testing guide', 'reply': '未引用的历史复述'}), {}
        key = data['evidence'][0]['citation_id']
        return json.dumps({'answer_status': 'answered', 'claims': [{'text': '按本轮提供的网页证据总结', 'citation_ids': [key]}]}), {}
    monkeypatch.setattr('backend.app.assistant_api.chat_json', summary)
    with monkeypatch.context() as patch:
        async def forbidden(*args, **kwargs):
            raise AssertionError('Snapshot follow-up should not search again')
        patch.setattr('backend.app.assistant_api.tavily_search', forbidden)
        answer = send(client, tid, '概括刚才的资料').json()['answer']
        assert answer['citations'] and answer['reused_source_snapshot']
        assert '未引用的历史复述' not in str(answer)
    answer = send(client, tid, '重新搜索最新资料').json()['answer']
    assert answer['citations'] and answer['usage']['search']['search_requests'] == 1
    assert not answer.get('reused_source_snapshot')


def test_web_maintenance_checks_do_not_masquerade_as_inventory_policy():
    from backend.app.knowledge_answers import bind_answer
    pool = {'web': {'citation_id': 'web', 'excerpt': '由维修人员核对电池类型', 'source_type': 'web_search'}}
    output = json.dumps({'answer_status': 'answered', 'checks': [{'text': '由维修人员核对电池类型', 'citation_ids': ['web']}]})
    answer = bind_answer(output, pool, {}, '维修人员核查事项', 'public_web')
    assert answer['checks'][0]['citation_ids'] == ['web'] and answer['citations'][0]['source_type'] == 'web_search'
    wrong_domain = bind_answer(output, pool, {}, '库存制度', 'inventory_policies')
    assert wrong_domain['answer_status'] == 'outside_scope'
    assert not wrong_domain['checks'] and not wrong_domain['citations']
