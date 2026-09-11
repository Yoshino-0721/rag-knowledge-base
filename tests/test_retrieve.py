"""检索测试：排序、参数约束与空库行为（使用桩集合）。"""

from src.retrieve import search


class StubCollection:
    """返回预设结果的集合替身，并校验 n_results 取值。"""

    def __init__(self, documents, metadatas, distances):
        self._result = {
            "documents": [documents],
            "metadatas": [metadatas],
            "distances": [distances],
        }

    def count(self):
        return len(self._result["documents"][0])

    def query(self, query_embeddings, n_results):
        assert n_results <= self.count(), "n_results 不应超过库内条目数"
        return self._result


def make_stub(entries):
    """entries: [(text, source, location, distance), ...]"""
    documents = [e[0] for e in entries]
    metadatas = [{"source": e[1], "location": e[2]} for e in entries]
    distances = [e[3] for e in entries]
    return StubCollection(documents, metadatas, distances)


def test_blank_question_returns_nothing():
    collection = make_stub([("内容", "a.md", "", 0.1)])

    assert search("   ", collection=collection) == []


def test_empty_collection_skips_embedding():
    collection = make_stub([])
    called = []

    hits = search("问题", collection=collection, embed_fn=lambda q: called.append(q) or [])

    assert hits == []
    assert called == [], "空库时不应调用 embedding 接口"


def test_hits_sorted_by_similarity_desc():
    collection = make_stub(
        [
            ("A", "a.md", "", 0.40),
            ("B", "b.md", "", 0.10),
            ("C", "c.md", "", 0.25),
        ]
    )

    hits = search("问题", top_k=3, collection=collection, embed_fn=lambda q: [0.1])

    assert [hit.text for hit in hits] == ["B", "C", "A"]
    assert [hit.score for hit in hits] == [0.9, 0.75, 0.6]


def test_top_k_capped_by_collection_size():
    collection = make_stub([("A", "a.md", "", 0.2), ("B", "b.md", "", 0.3)])

    hits = search("问题", top_k=10, collection=collection, embed_fn=lambda q: [0.1])

    assert len(hits) == 2


def test_hit_carries_source_and_location():
    collection = make_stub([("内容", "docs/x.pdf", "第 2 页", 0.15)])

    hit = search("问题", collection=collection, embed_fn=lambda q: [0.1])[0]

    assert hit.source == "docs/x.pdf"
    assert hit.location == "第 2 页"


def test_missing_metadata_falls_back_to_placeholder():
    collection = StubCollection(["内容"], [{}], [0.5])

    hit = search("问题", collection=collection, embed_fn=lambda q: [0.1])[0]

    assert hit.source == "未知来源"
    assert hit.location == ""
