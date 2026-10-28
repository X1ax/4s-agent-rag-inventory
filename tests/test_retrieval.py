"""Mock HTTP protocol checks and real local Chroma checks; not model-quality scores."""
import json
import pickle
import shutil
import subprocess
import sys

import httpx
import pytest

from backend.app.ai import EmbeddingClient, embedding_contract
from backend.app.config import Settings
from backend.app.knowledge import CORPORA, ROOT, KnowledgeError, digest, parse_corpora
from backend.app.retrieval import build_index, index_owner, open_index


@pytest.fixture
def settings():
    return Settings(_env_file=None, embedding_api_key="test-secret-only")


def response_for(texts):
    return {"model": "BAAI/bge-m3", "data": [{"index": i, "embedding": [float(i+1)] + [0.0]*1023}
        for i, _ in enumerate(texts)], "usage": {"total_tokens": 7}}


def test_http_request_uses_separate_key_and_orders_vectors_by_response_index(settings):
    def respond(request):
        assert str(request.url) == "https://api.siliconflow.cn/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-secret-only"
        body = json.loads(request.content)
        assert body == {"model": "BAAI/bge-m3", "input": ["条款甲", "条款乙"], "encoding_format": "float"}
        result = response_for(body["input"])
        result["data"].reverse()
        return httpx.Response(200, json=result)
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(respond))
    assert [vector[0] for vector in embedder.embed(["条款甲", "条款乙"])] == [1.0, 2.0]
    assert embedder.usage() == {"embedding_requests": 1, "embedded_texts": 2, "embedding_retries": 0, "total_tokens": 7}
    assert "test-secret-only" not in repr(settings)


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(data=[]),
    lambda b: b["data"][0].update(index=True),
    lambda b: b["data"][0].update(index=2),
    lambda b: b["data"][0].update(embedding=[1.0]*768),
    lambda b: b["data"][0].update(embedding=[0.0]*1024),
    lambda b: b["data"][0].update(embedding=[True]*1024),
    lambda b: b["data"][0].update(embedding=["1"]*1024),
    lambda b: b.update(model="another-model"),
    lambda b: b.update(data=[{}, {}]),
])
def test_bad_embedding_payload_never_retries_or_returns_a_vector(settings, mutation):
    body = response_for(["甲"])
    mutation(body)
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(KnowledgeError) as error:
        embedder.embed(["甲"], build=True)
    assert error.value.code == "embedding_response" and embedder.requests == 1


@pytest.mark.parametrize("body", [b"not-json", b'{"data":[{"index":0,"embedding":[NaN]}]}'])
def test_non_json_or_non_finite_embedding_is_rejected(settings, body):
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
    with pytest.raises(KnowledgeError):
        embedder.embed(["甲"])


@pytest.mark.parametrize("status,build,expected", [(401, True, 1), (403, True, 1), (404, True, 1), (429, True, 2), (503, True, 2), (503, False, 1)])
def test_http_failure_is_sanitized_and_only_build_has_one_transient_retry(settings, monkeypatch, status, build, expected):
    monkeypatch.setattr("backend.app.ai.time.sleep", lambda _: None)
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(status, text="sensitive-upstream-body")))
    with pytest.raises(KnowledgeError) as error:
        embedder.embed(["甲"], build=build)
    assert embedder.requests == expected
    assert "sensitive-upstream-body" not in str(error.value)


def test_timeout_and_missing_key_have_explicit_safe_errors(settings):
    def timeout(request):
        raise httpx.ReadTimeout("sensitive-upstream-url", request=request)
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(timeout))
    with pytest.raises(KnowledgeError) as error:
        embedder.embed(["甲"])
    assert error.value.code == "embedding_timeout" and "sensitive" not in str(error.value)
    embedder = EmbeddingClient(settings.model_copy(update={"embedding_api_key": settings.embedding_api_key.__class__("")}))
    with pytest.raises(KnowledgeError) as error:
        embedder.embed(["甲"])
    assert error.value.code == "embedding_not_configured" and embedder.requests == 0


def test_build_retry_budget_is_shared_across_batches_and_usage_is_unknown(settings, monkeypatch):
    monkeypatch.setattr("backend.app.ai.time.sleep", lambda _: None)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=response_for(["甲"])) if len(calls) == 2 else httpx.Response(503)
    embedder = EmbeddingClient(settings, transport=httpx.MockTransport(respond))
    embedder.embed(["甲"], build=True)
    with pytest.raises(KnowledgeError):
        embedder.embed(["乙"], build=True)
    assert embedder.requests == 3 and embedder.retries == 1
    assert embedder.usage()["total_tokens"] is None


@pytest.mark.parametrize("changes", [{"embedding_model_id": "Pro/BAAI/bge-m3"},
    {"embedding_base_url": "https://user:password@example.org/v1"},
    {"embedding_base_url": "https://example.org/v1?api_key=secret"}])
def test_contract_rejects_unselected_model_or_credential_bearing_url(settings, changes):
    with pytest.raises(KnowledgeError):
        embedding_contract(settings.model_copy(update=changes))


@pytest.fixture
def project(tmp_path):
    for relative, _ in CORPORA.values():
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return tmp_path


class FakeEmbedding:
    def __init__(self, settings, fail=False):
        self.contract = embedding_contract(settings)
        self.calls = 0
        self.fail = fail

    def embed(self, texts, *, build=False):
        self.calls += 1
        if self.fail:
            raise KnowledgeError("embedding_unavailable", "测试Embedding失败")
        result = []
        for text in texts:
            vector = [0.0]*1024
            vector[int(digest(text.encode("utf-8"))[:8], 16) % 1024] = 1.0
            result.append(vector)
        return result

    def usage(self):
        return {"embedding_requests": self.calls, "total_tokens": None}


def test_metadata_complete_but_unreadable_vector_segment_is_not_published(settings, project, monkeypatch):
    from chromadb.api.models.Collection import Collection
    from chromadb.errors import InternalError

    def missing_segment(self, *args, **kwargs):
        raise InternalError('Error creating hnsw segment reader: Nothing found on disk')

    monkeypatch.setattr(Collection, 'query', missing_segment)
    with pytest.raises(KnowledgeError) as caught:
        build_index(settings, project, embedder=FakeEmbedding(settings))
    assert caught.value.code == 'index_build_failed'
    assert not (project / 'data/chroma/rag/active.json').exists()


def test_published_vectors_persist_the_tail_batch_before_wal_replay(settings, project):
    built = build_index(settings, project, embedder=FakeEmbedding(settings))
    directory = project / 'data/chroma/rag/generations' / built['generation_id']
    expected = {json.loads(line)['chunk_id'] for line in (directory / 'units.jsonl').read_text(encoding='utf-8').splitlines()}
    # Only inspect files produced by this isolated fixture, never uploaded/untrusted pickle data.
    persisted = {chunk_id for path in (directory / 'chroma').glob('*/index_metadata.pickle')
        for chunk_id in pickle.loads(path.read_bytes())['id_to_label']}
    assert persisted == expected


def test_real_chroma_reopen_scopes_domain_filters_and_shared_context(settings, project):
    embedder = FakeEmbedding(settings)
    result = build_index(settings, project, embedder=embedder)
    assert result["unit_count"] == 242 and result["indexed_documents"] == 19
    assert result["unsupported_documents"] == 0 and embedder.calls == 16
    with open_index(settings, project, embedder=embedder) as index:
        calls = embedder.calls
        assert index.search("库存制度", "inventory_policies")["status"] == "no_eligible_documents"
        assert embedder.calls == calls
        for kb in CORPORA:
            found = index.search("查询完整条件", kb, scope="draft_preview", top_k=10)
            assert found["status"] == "retrieved" and len(found["hits"]) == 10
            assert all(u["knowledge_base"] == kb and u["source_state"] == "draft" for u in found["hits"])
            assert all(c["knowledge_base"] == kb for c in found["contexts"])
            assert all("page_start" not in u for u in found["hits"] if u["document_id"] not in {"INV-RCV-004", "INV-WHS-007"})
            if kb == "general_diagnostics":
                common = [c for c in found["contexts"] if c["document_id"] == "DX-GEN-001"]
                assert len(common) == 8
    # A separate process reopens the persisted store without any online embedding call.
    code = """
import sys
from backend.app.config import Settings
from backend.app.retrieval import open_index
with open_index(Settings(_env_file=None), sys.argv[1]) as index:
    assert len(index.units) == 242
    assert index.search('库存', 'inventory_policies')['status'] == 'no_eligible_documents'
print('reopened')
"""
    run = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(project)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "reopened"


def test_unchanged_build_skips_embedding_even_when_parse_date_changes(settings, project, monkeypatch):
    embedder = FakeEmbedding(settings)
    first = build_index(settings, project, embedder=embedder)
    calls = embedder.calls
    original_parse = parse_corpora
    def next_date(root):
        snapshot = original_parse(root)
        snapshot["qualification_date"] = "2026-10-24"
        snapshot["parse_id"] = "next-day-parse"
        return snapshot
    monkeypatch.setattr("backend.app.retrieval.parse_corpora", next_date)
    second = build_index(settings, project, embedder=embedder)
    assert second["reused"] and second["generation_id"] == first["generation_id"]
    assert second["embedding_requests"] == 0 and embedder.calls == calls


def test_failed_build_never_replaces_active_generation(settings, project):
    first = build_index(settings, project, embedder=FakeEmbedding(settings))
    pointer = project / "data/chroma/rag/active.json"
    previous = pointer.read_bytes()
    other = settings.model_copy(update={"embedding_base_url": "https://example.org/v1"})
    with pytest.raises(KnowledgeError, match="测试Embedding失败"):
        build_index(other, project, embedder=FakeEmbedding(other, fail=True))
    assert pointer.read_bytes() == previous
    with open_index(settings, project) as index:
        assert index.report["generation_id"] == first["generation_id"]
    assert len(list((project / "data/chroma/rag/generations").iterdir())) == 2


def test_same_dimension_different_endpoint_cannot_query_old_vectors(settings, project):
    build_index(settings, project, embedder=FakeEmbedding(settings))
    other = settings.model_copy(update={"embedding_base_url": "https://example.org/v1"})
    with pytest.raises(KnowledgeError) as error:
        with open_index(other, project):
            pass
    assert error.value.code == "index_model_mismatch"


def test_changed_sources_and_tampered_snapshots_fail_closed(settings, project):
    build_index(settings, project, embedder=FakeEmbedding(settings))
    source = project / "data/knowledge/inventory/policies/INV-ADJ-008.md"
    original = source.read_bytes()
    with open_index(settings, project, embedder=FakeEmbedding(settings)) as index:
        source.write_bytes(original + b"\nchanged")
        found = index.search("查询", "inventory_policies", scope="draft_preview")
        assert found["status"] == "retrieved"
        assert {"document_id": "INV-ADJ-008", "reason": "source_hash_mismatch"} in found["unavailable_documents"]
        assert all(u["document_id"] != "INV-ADJ-008" for u in found["hits"] + found["contexts"])
    source.write_bytes(original)
    active = json.loads((project / "data/chroma/rag/active.json").read_text())
    units = project / "data/chroma/rag/generations" / active["generation_id"] / "units.jsonl"
    units.write_bytes(units.read_bytes() + b"\n")
    with pytest.raises(KnowledgeError) as error:
        with open_index(settings, project):
            pass
    assert error.value.code == "index_corrupt"


def test_first_build_failure_does_not_publish_any_active_index(settings, project):
    with pytest.raises(KnowledgeError):
        build_index(settings, project, embedder=FakeEmbedding(settings, fail=True))
    assert not (project / "data/chroma/rag/active.json").exists()


def test_source_change_during_embedding_prevents_activation(settings, project):
    embedder = FakeEmbedding(settings)
    original_embed = embedder.embed
    def changing_embed(texts, *, build=False):
        source = project / "data/knowledge/inventory/policies/INV-ADJ-008.md"
        source.write_bytes(source.read_bytes()+b"\n")
        return original_embed(texts, build=build)
    embedder.embed = changing_embed
    with pytest.raises(KnowledgeError) as error:
        build_index(settings, project, embedder=embedder)
    assert error.value.code == "knowledge_changed"
    assert not (project / "data/chroma/rag/active.json").exists()


def test_withdrawn_sources_are_filtered_before_dense_search_and_context_expansion(settings, project):
    source = project / "data/knowledge/inventory/policies/INV-ADJ-008.md"
    source.write_text(source.read_text(encoding="utf-8").replace("approval_status: pending_approval", "approval_status: withdrawn"), encoding="utf-8")
    path = project / "data/knowledge/inventory/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    row = next(d for d in manifest["documents"] if d["document_id"] == "INV-ADJ-008")
    row.update(approval_status="withdrawn", sha256=digest(source.read_bytes()))
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        found = index.search("退库", "inventory_policies", scope="draft_preview", top_k=10)
        assert all(u["document_id"] != "INV-ADJ-008" for u in found["hits"])
        assert all(c["document_id"] != "INV-ADJ-008" for c in found["contexts"])


def test_source_change_during_query_discards_results(settings, project):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    original_embed = embedder.embed
    def changing_embed(texts, *, build=False):
        path = project / "data/knowledge/diagnostics/part-type-map.json"
        path.write_bytes(path.read_bytes()+b"\n")
        return original_embed(texts, build=build)
    embedder.embed = changing_embed
    with open_index(settings, project, embedder=embedder) as index:
        with pytest.raises(KnowledgeError) as error:
            index.search("发动机不转", "general_diagnostics", scope="draft_preview")
        assert error.value.code == "knowledge_changed"


def test_changed_content_cannot_reuse_an_existing_document_version(settings, project):
    build_index(settings, project, embedder=FakeEmbedding(settings))
    path = project / "data/knowledge/inventory/policies/INV-ADJ-008.md"
    path.write_bytes(path.read_bytes()+b"\n")
    manifest_path = project / "data/knowledge/inventory/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    next(d for d in manifest["documents"] if d["document_id"] == "INV-ADJ-008")["sha256"] = digest(path.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(KnowledgeError) as error:
        build_index(settings, project, embedder=FakeEmbedding(settings))
    assert error.value.code == "document_version_reused"


def test_query_validation_and_deleted_chroma_records_are_not_silently_accepted(settings, project):
    embedder = FakeEmbedding(settings)
    build_index(settings, project, embedder=embedder)
    with open_index(settings, project, embedder=embedder) as index:
        for question, kb, scope, k in [(" ", "inventory_policies", "draft_preview", 5),
                ("甲", "other", "draft_preview", 5), ("甲", "inventory_policies", "all", 5),
                ("甲", "inventory_policies", "draft_preview", True), ("甲", "inventory_policies", "draft_preview", 11)]:
            with pytest.raises(KnowledgeError) as error:
                index.search(question, kb, scope=scope, top_k=k)
            assert error.value.code == "invalid_query"
        collection = index.client.get_collection("inventory_policies", embedding_function=None)
        collection.delete(ids=[next(u["chunk_id"] for u in index.units.values() if u["knowledge_base"] == "inventory_policies")])
    with pytest.raises(KnowledgeError) as error:
        with open_index(settings, project):
            pass
    assert error.value.code == "index_corrupt"


def test_owner_lock_rejects_second_process_and_releases_on_exit(project):
    code = """
import sys
from backend.app.retrieval import index_owner
from backend.app.knowledge import KnowledgeError
try:
    with index_owner(sys.argv[1]):
        print('acquired')
except KnowledgeError as error:
    print(error.code)
"""
    with index_owner(project):
        run = subprocess.run([sys.executable, "-c", code, str(project)], cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert run.returncode == 0 and run.stdout.strip() == "index_in_use", run.stderr
    run = subprocess.run([sys.executable, "-c", code, str(project)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0 and run.stdout.strip() == "acquired", run.stderr
