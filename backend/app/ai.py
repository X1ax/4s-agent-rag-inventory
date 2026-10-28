"""Explicit BGE-M3 HTTP requests; no implicit embedding or local model download."""
import math
import asyncio
import time
from urllib.parse import urlsplit

import httpx

from backend.app.knowledge import KnowledgeError

EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMENSION = 1024


def embedding_contract(settings):
    base = settings.embedding_base_url.strip().rstrip("/")
    url = urlsplit(base)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise KnowledgeError("embedding_configuration", "Embedding地址需为不含凭据或查询参数的HTTPS地址")
    if settings.embedding_model_id != EMBEDDING_MODEL:
        raise KnowledgeError("embedding_model_mismatch", "本阶段仅允许已选定的BAAI/bge-m3，修改模型后须重新验证并建索引")
    return {"endpoint": base + "/embeddings", "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIMENSION, "distance": "cosine"}


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise KnowledgeError("embedding_response", "Embedding返回数量不符合请求")
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSION
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            raise KnowledgeError("embedding_response", "Embedding向量维度或数值无效")
    return vectors


class EmbeddingClient:
    def __init__(self, settings, *, transport=None):
        self.contract = embedding_contract(settings)
        self.key = settings.embedding_api_key
        self.transport = transport
        self.requests = self.texts = self.retries = 0
        self.total_tokens = 0
        self.usage_available = True

    def embed(self, texts, *, build=False):
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 16
                or any(not isinstance(text, str) or not text.strip() or len(text) > 4000 for text in texts)):
            raise KnowledgeError("embedding_input", "Embedding需为1至16段非空文本，每段不超过4000字符")
        if not self.key.get_secret_value():
            raise KnowledgeError("embedding_not_configured", "尚未配置EMBEDDING_API_KEY")
        with httpx.Client(timeout=30 if build else 10, transport=self.transport, follow_redirects=False) as client:
            for attempt in range(2 if build else 1):
                transient = False
                self.requests += 1
                try:
                    response = client.post(self.contract["endpoint"],
                        headers={"Authorization": "Bearer " + self.key.get_secret_value()},
                        json={"model": EMBEDDING_MODEL, "input": texts, "encoding_format": "float"})
                except httpx.TimeoutException:
                    error = KnowledgeError("embedding_timeout", "Embedding请求超时，请稍后显式重试")
                    transient = True
                except httpx.RequestError:
                    error = KnowledgeError("embedding_unavailable", "无法连接Embedding服务")
                    transient = True
                else:
                    if response.status_code == 200:
                        try:
                            body = response.json()
                            rows = body["data"]
                            if (body.get("model", EMBEDDING_MODEL) != EMBEDDING_MODEL
                                    or not isinstance(rows, list) or len(rows) != len(texts)
                                    or any(not isinstance(row, dict) or type(row.get("index")) is not int for row in rows)
                                    or sorted(row["index"] for row in rows) != list(range(len(texts)))):
                                raise ValueError
                            vectors = validate_vectors([row["embedding"] for row in sorted(rows, key=lambda row: row["index"])], len(texts))
                        except (ValueError, KeyError, TypeError):
                            raise KnowledgeError("embedding_response", "Embedding响应结构、模型或向量校验失败") from None
                        usage = body.get("usage")
                        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
                        if type(tokens) is int and tokens >= 0:
                            self.total_tokens += tokens
                        else:
                            self.usage_available = False
                        self.texts += len(texts)
                        return vectors
                    transient = response.status_code in {429, 502, 503, 504}
                    error = KnowledgeError("embedding_http_error", f"Embedding服务返回HTTP {response.status_code}，请检查配置或服务状态")
                if not transient or not build or attempt == 1 or self.retries >= 1:
                    raise error from None
                self.retries += 1
                self.usage_available = False  # A failed attempt may still have consumed upstream tokens.
                time.sleep(1)

    def usage(self):
        return {"embedding_requests": self.requests, "embedded_texts": self.texts,
            "embedding_retries": self.retries, "total_tokens": self.total_tokens if self.usage_available else None}


async def post_ai(endpoint, key, payload, *, timeout, kind, transport=None):
    """One cancellable HTTP attempt. Never propagate upstream bodies or URLs."""
    if not key.get_secret_value():
        raise KnowledgeError(kind + "_not_configured", "尚未配置" + {"chat": "对话模型", "embedding": "Embedding", "search": "Tavily"}[kind] + "密钥")
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
                response = await client.post(endpoint, headers={"Authorization": "Bearer " + key.get_secret_value()}, json=payload)
        if response.status_code != 200:
            raise KnowledgeError(kind + "_http_error", f"模型服务返回HTTP {response.status_code}，请检查配置或稍后重试")
        return response.json()
    except (TimeoutError, httpx.TimeoutException):
        raise KnowledgeError(kind + "_timeout", "模型请求超时，请显式重试") from None
    except httpx.RequestError:
        raise KnowledgeError(kind + "_unavailable", "无法连接模型服务") from None
    except ValueError:
        raise KnowledgeError(kind + "_response", "模型服务返回了无效JSON") from None


async def query_embedding(settings, question, *, transport=None):
    contract = embedding_contract(settings)
    body = await post_ai(contract["endpoint"], settings.embedding_api_key,
        {"model": contract["model"], "input": [question], "encoding_format": "float"},
        timeout=10, kind="embedding", transport=transport)
    try:
        if body.get("model", contract["model"]) != contract["model"] or len(body["data"]) != 1 or type(body["data"][0]["index"]) is not int or body["data"][0]["index"] != 0:
            raise ValueError
        vector = validate_vectors([body["data"][0]["embedding"]], 1)[0]
        return vector, token_usage(body)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise KnowledgeError("embedding_response", "Embedding响应结构校验失败") from None


def token_usage(body):
    usage = body.get("usage", {})
    return {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}


async def chat_json(settings, messages, *, transport=None, max_tokens=3000):
    base = settings.base_url.strip().rstrip("/")
    url = urlsplit(base)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
            or not settings.model_id.strip()):
        raise KnowledgeError("chat_configuration", "请配置有效的HTTPS对话地址和模型名称")
    payload = {"model": settings.model_id, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if url.hostname == "api.deepseek.com":
        # Official API: bounded, non-thinking JSON output for this read-only task.
        payload.update(thinking={"type": "disabled"}, response_format={"type": "json_object"})
    body = await post_ai(base + "/chat/completions", settings.api_key, payload,
        timeout=30, kind="chat", transport=transport)
    try:
        choice = body["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") != "stop" or message.get("tool_calls") or not isinstance(message["content"], str):
            raise ValueError
        return message["content"], token_usage(body)
    except (ValueError, KeyError, TypeError, IndexError):
        raise KnowledgeError("chat_response", "模型未返回完整文本回答") from None


async def chat_tools(settings, messages, tools, *, transport=None):
    """Native function calling; execution remains in the server whitelist."""
    base = settings.base_url.strip().rstrip('/')
    url = urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or not settings.model_id:
        raise KnowledgeError('chat_configuration', '请配置有效的HTTPS模型地址')
    payload = {'model': settings.model_id, 'messages': messages, 'tools': tools, 'tool_choice': 'auto',
        'temperature': 0, 'max_tokens': 2500, 'parallel_tool_calls': False}
    if url.hostname == 'api.deepseek.com':
        payload['thinking'] = {'type': 'disabled'}
    body = await post_ai(base+'/chat/completions', settings.api_key, payload, timeout=30, kind='chat', transport=transport)
    try:
        choice = body['choices'][0]
        message = choice['message']
        calls = message.get('tool_calls', [])
        if choice['finish_reason'] not in {'stop', 'tool_calls'} or not isinstance(calls, list) or len(calls) > 4:
            raise ValueError
        allowed = {t['function']['name'] for t in tools}
        for call in calls:
            if (call['type'] != 'function' or call['function']['name'] not in allowed
                    or not isinstance(call['id'], str) or not isinstance(call['function']['arguments'], str)
                    or len(call['function']['arguments']) > 15000):
                raise ValueError
        return {'role': 'assistant', 'content': message.get('content') or '', **({'tool_calls': calls} if calls else {})}, token_usage(body)
    except (ValueError, KeyError, TypeError, IndexError):
        raise KnowledgeError('chat_response', '模型工具调用结构或工具白名单校验失败') from None
