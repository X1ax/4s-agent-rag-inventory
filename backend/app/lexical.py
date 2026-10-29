"""Small-corpus character TF-IDF and deterministic rank fusion, without model downloads."""
import math
import re
from collections import Counter

UNIT_CODE = re.compile(r"(?<![A-Za-z0-9_.-])(?:INV-[A-Z]{3}-\d{3}-C\d{3}|DX-[A-Z]{3,4}-\d{2})(?![A-Za-z0-9_.-])", re.I)
DOCUMENT_CODE = re.compile(r"(?<![A-Za-z0-9_.-])(?:INV|DX)-[A-Z]{3}-\d{3}(?![A-Za-z0-9_.-])", re.I)
TOKEN = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+(?:[_.-][A-Za-z0-9]+)*")
# API field labels bridge code identifiers to the Chinese policy vocabulary, not to permissions.
FIELD_TERMS = {'operator_id': '操作人 身份 服务端', 'request_id': '请求标识 幂等',
    'thread_id': '会话编号 身份', 'warehouse_id': '仓库', 'part_id': '备件', 'supplier_id': '供应商'}


def field_query_terms(question):
    return list(dict.fromkeys(FIELD_TERMS[word.lower()] for word in TOKEN.findall(question)
        if word.lower() in FIELD_TERMS))


def tokens(text):
    result = []
    for match in TOKEN.findall(text):
        if '\u4e00' <= match[0] <= '\u9fff':
            result.extend(match[i:i+2] for i in range(max(1, len(match)-1)))
        else:
            result.append(match.lower())
    return Counter(result)


def lexical_candidates(question, units, limit=20):
    documents = {u["chunk_id"]: tokens(u["embedding_text"]) for u in units}
    query = tokens(question)
    frequency = Counter(term for row in documents.values() for term in row)
    idf = {term: 1 + math.log((1 + len(documents))/(1 + count)) for term, count in frequency.items()}

    def vector(counts):
        values = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items() if term in idf}
        norm = math.sqrt(sum(value*value for value in values.values()))
        return {term: value/norm for term, value in values.items()} if norm else {}

    query_vector = vector(query)
    scored = [(key, sum(weight*query_vector.get(term, 0) for term, weight in vector(counts).items()))
        for key, counts in documents.items()]
    return sorted(((key, score) for key, score in scored if score > 0), key=lambda item: (-item[1], item[0]))[:limit]


def fuse(dense, lexical, exact):
    scores, ranks = {}, {}
    for branch, values in (("dense", dense), ("lexical", lexical)):
        for rank, (chunk_id, score) in enumerate(values, 1):
            scores[chunk_id] = scores.get(chunk_id, 0) + 1/(60 + rank)
            ranks.setdefault(chunk_id, {})[branch + "_rank"] = rank
    ordered = list(dict.fromkeys(exact + sorted(scores, key=lambda key: (-scores[key], key))))
    return ordered, {key: {**ranks.get(key, {}), "rrf_score": scores.get(key, 0), "exact_match": key in exact} for key in ordered}
