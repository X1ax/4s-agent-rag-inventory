"""Fixed-endpoint Tavily search. Snippets are untrusted evidence, never instructions."""
import ipaddress
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from backend.app.ai import post_ai
from backend.app.knowledge import KnowledgeError, digest


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        return False
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.port not in {None, 80, 443} or url.hostname.lower() in {"localhost", "localhost.localdomain"}
                or url.hostname.lower().endswith((".local", ".internal", ".localhost"))):
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            return "." in url.hostname and not re.fullmatch(r"[\d.]+", url.hostname)
    except ValueError:
        return False


def validate_public_query(query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise KnowledgeError("search_query_invalid", "联网搜索词无效，请缩短并只提供公开信息")
    if re.search(r"sk-[\w-]{8,}|tvly-[\w-]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|\b[A-HJ-NPR-Z0-9]{17}\b|(?:API_KEY|密码|密钥|身份证|手机号|VIN)\s*[:：=]\s*\S+", query, re.I):
        raise KnowledgeError("search_query_private", "搜索词可能包含凭据或个人标识，请移除后再联网查询")
    return query.strip()


async def tavily_search(settings, query, *, transport=None):
    query = validate_public_query(query)
    body = await post_ai("https://api.tavily.com/search", settings.tavily_api_key,
        {"query": query, "search_depth": "basic", "max_results": 5, "include_answer": False,
         "include_raw_content": False, "include_images": False, "include_usage": True},
        timeout=10, kind="search", transport=transport)
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise KnowledgeError("search_response", "Tavily返回格式无效，本次未生成联网结论")
    pool = {}
    date = datetime.now(UTC).isoformat()
    for row in body["results"][:5]:
        if (not isinstance(row, dict) or not public_url(row.get("url")) or not isinstance(row.get("content"), str)
                or not row["content"].strip() or not isinstance(row.get("title"), str)):
            continue
        excerpt = row["content"][:2400]
        key = digest(["tavily", row["url"], excerpt])
        pool[key] = {"citation_id": key, "chunk_id": key, "source_type": "web_search", "title": row["title"][:300],
            "url": row["url"], "excerpt": excerpt, "accessed_at": date, "native_format": "web", "source_state": "external",
            "document_id": "WEB-" + str(len(pool)+1), "document_version": date[:10], "unit_id": None, "unit_ids": [], "section_path": []}
    usage = body.get("usage", {})
    credits = usage.get("credits") if isinstance(usage, dict) else None
    return pool, {"search_requests": 1, "credits": credits if type(credits) in {int, float} and credits >= 0 else None}
