"""精排（Rerank）测试。

分两层：
1. ``llm.rerank_documents`` —— 请求怎么拼（URL / 模型 / 鉴权头）、响应怎么解析；
2. ``retrieve`` —— 候选池怎么放大、结果怎么写回、失败怎么降级。

全程用替身，不产生任何真实网络请求。
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.llm import rerank_documents
from src.retrieve import (
    RERANK_MAX_CANDIDATES,
    Hit,
    rerank_hits,
    search_ranked,
)

HITS = [
    Hit(text="A", source="a.md", location="", score=0.90),
    Hit(text="B", source="b.md", location="", score=0.70),
    Hit(text="C", source="c.md", location="第 2 页", score=0.50),
]


# ---------- 请求构造与响应解析 ----------


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """替身 httpx.Client，记录调用参数。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self.payload)


def test_rerank_url_is_built_on_top_of_base_url():
    """rerank 不是 OpenAI 兼容协议，需要自己在 base_url 后拼路径，别拼出双斜杠。"""
    assert settings.rerank_url.endswith("/rerank")
    assert "//rerank" not in settings.rerank_url


def test_rerank_documents_builds_request_and_sorts_by_score(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "sk-test-123")
    client = FakeClient(
        {
            "results": [
                {"index": 0, "relevance_score": 0.21},
                {"index": 2, "relevance_score": 0.93},
            ]
        }
    )

    scored = rerank_documents("问题", ["A", "B", "C"], top_n=2, client=client)

    assert scored == [(2, 0.93), (0, 0.21)], "结果应按相关性得分降序"

    call = client.calls[0]
    assert call["url"] == settings.rerank_url
    assert call["json"]["model"] == settings.rerank_model
    assert call["json"]["query"] == "问题"
    assert call["json"]["documents"] == ["A", "B", "C"]
    assert call["json"]["top_n"] == 2


def test_rerank_requests_raw_scores(monkeypatch):
    """默认的归一化分在候选都很相关时会一起饱和到 1.0，必须取原始分才看得见高下。"""
    monkeypatch.setattr(settings, "api_key", "k")
    client = FakeClient({"results": []})

    rerank_documents("问题", ["A", "B"], client=client)

    assert client.calls[0]["json"]["return_raw_scores"] is True
    assert "top_n" not in client.calls[0]["json"], "未指定 top_n 时不应带上该参数"


def test_rerank_reuses_the_same_api_key_as_other_calls(monkeypatch):
    """精排复用 .env 里的 ZHIPUAI_API_KEY，不额外要求第二套凭据。"""
    monkeypatch.setattr(settings, "api_key", "sk-reused-key")
    client = FakeClient({"results": []})

    rerank_documents("问题", ["A"], client=client)

    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-reused-key"


def test_rerank_documents_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    client = FakeClient({"results": []})

    with pytest.raises(RuntimeError):
        rerank_documents("问题", ["A"], client=client)

    assert client.calls == [], "没配 Key 时不该发出请求"


def test_rerank_documents_skips_empty_inputs():
    client = FakeClient({"results": []})

    assert rerank_documents("   ", ["A"], client=client) == []
    assert rerank_documents("问题", [], client=client) == []
    assert client.calls == []


def test_rerank_documents_tolerates_missing_results(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "k")

    assert rerank_documents("问题", ["A"], client=FakeClient({})) == []


# ---------- 候选重排与降级 ----------


def test_rerank_hits_reorders_and_attaches_score():
    def fake_rerank(query, documents, top_n):
        assert documents == ["A", "B", "C"]
        return [(2, 0.95), (0, 0.40)]

    ranked = rerank_hits("问题", HITS, top_k=2, rerank_fn=fake_rerank)

    assert [hit.text for hit in ranked] == ["C", "A"]
    assert [hit.rerank_score for hit in ranked] == [0.95, 0.4]
    assert ranked[0].score == 0.5, "向量分要保留，作为对照"
    assert ranked[0].source == "c.md", "其它字段应跟着一起搬过来"
    assert ranked[0].location == "第 2 页"


def test_rerank_failure_falls_back_to_vector_order():
    def boom(query, documents, top_n):
        raise ValueError("接口超时")

    ranked = rerank_hits("问题", HITS, top_k=2, rerank_fn=boom)

    assert [hit.text for hit in ranked] == ["A", "B"]
    assert all(hit.rerank_score is None for hit in ranked)


def test_rerank_config_error_is_not_swallowed():
    """未配 API Key 属于环境问题，不能静默降级，否则很难排查。"""

    def boom(query, documents, top_n):
        raise RuntimeError("未配置 API Key")

    with pytest.raises(RuntimeError):
        rerank_hits("问题", HITS, top_k=2, rerank_fn=boom)


def test_rerank_empty_result_falls_back_to_vector_order():
    ranked = rerank_hits("问题", HITS, top_k=2, rerank_fn=lambda q, d, n: [])

    assert [hit.text for hit in ranked] == ["A", "B"]


def test_rerank_ignores_out_of_range_and_duplicate_index():
    def fake_rerank(query, documents, top_n):
        return [(9, 0.90), (1, 0.80), (1, 0.70), (-1, 0.60)]

    ranked = rerank_hits("问题", HITS, top_k=3, rerank_fn=fake_rerank)

    assert [hit.text for hit in ranked] == ["B"], "越界下标与重复下标都应被忽略"
    assert ranked[0].rerank_score == 0.8


def test_rerank_empty_hits_returns_empty():
    assert rerank_hits("问题", [], rerank_fn=lambda q, d, n: [(0, 1.0)]) == []


# ---------- 完整召回链路 ----------


class StubCollection:
    """按 n_results 生成等量结果的替身集合。"""

    def __init__(self, count):
        self._count = count
        self.queries: list[int] = []

    def count(self):
        return self._count

    def query(self, query_embeddings, n_results):
        self.queries.append(n_results)
        return {
            "documents": [[f"片段{i}" for i in range(n_results)]],
            "metadatas": [[{"source": "a.md", "location": ""}] * n_results],
            "distances": [[0.1 + i * 0.01 for i in range(n_results)]],
        }


def test_search_ranked_widens_candidates_then_truncates(monkeypatch):
    monkeypatch.setattr(settings, "top_k", 2)
    monkeypatch.setattr(settings, "rerank_candidates", 6)
    collection = StubCollection(count=10)
    seen = {}

    def fake_rerank(query, documents, top_n):
        seen["docs"] = len(documents)
        seen["top_n"] = top_n
        return [(1, 0.9), (0, 0.8)]

    hits = search_ranked(
        "问题", collection=collection, embed_fn=lambda q: [0.1], rerank_fn=fake_rerank
    )

    assert collection.queries == [6], "应先按候选池大小宽召回"
    assert seen == {"docs": 6, "top_n": 2}
    assert len(hits) == 2


def test_search_ranked_skips_rerank_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(settings, "top_k", 2)
    collection = StubCollection(count=10)

    hits = search_ranked(
        "问题",
        collection=collection,
        embed_fn=lambda q: [0.1],
        rerank_fn=lambda *args: pytest.fail("关闭精排时不该调用精排"),
    )

    assert collection.queries == [2]
    assert all(hit.rerank_score is None for hit in hits)


def test_search_ranked_skips_rerank_when_library_is_small(monkeypatch):
    """库里的条目本来就少于 Top-K，全都要了，精排只是白花一次调用。"""
    monkeypatch.setattr(settings, "top_k", 5)
    monkeypatch.setattr(settings, "rerank_candidates", 20)
    collection = StubCollection(count=3)
    calls = []

    hits = search_ranked(
        "问题",
        collection=collection,
        embed_fn=lambda q: [0.1],
        rerank_fn=lambda *args: calls.append(args) or [],
    )

    assert len(hits) == 3
    assert calls == []


def test_candidate_pool_capped_at_api_limit(monkeypatch):
    monkeypatch.setattr(settings, "top_k", 5)
    monkeypatch.setattr(settings, "rerank_candidates", 500)
    collection = StubCollection(count=300)

    search_ranked(
        "问题",
        collection=collection,
        embed_fn=lambda q: [0.1],
        rerank_fn=lambda q, d, n: [(0, 1.0)],
    )

    assert collection.queries == [RERANK_MAX_CANDIDATES], "候选数不能超过接口上限"
