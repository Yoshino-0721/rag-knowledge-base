"""检索：向量召回 + 精排重排序。

两级结构：
1. ``search``      纯向量召回，按余弦相似度排序；
2. ``search_ranked`` 先用 ``search`` 宽召回一批候选，再交给精排模型二次排序，
   最终截取 Top-K。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from src.config import settings
from src.llm import embed_query, rerank_documents
from src.store import get_collection

# 智谱精排接口单次最多接受 128 条候选
RERANK_MAX_CANDIDATES = 128

RerankFn = Callable[[str, Sequence[str], int | None], list[tuple[int, float]]]


@dataclass(frozen=True)
class Hit:
    """一条召回结果。

    ``score`` 是向量余弦相似度（越大越相关）；
    ``rerank_score`` 是精排模型给出的相关性得分，没走精排时为 ``None``。
    """

    text: str
    source: str
    location: str
    score: float
    rerank_score: float | None = None


def search(
    question: str,
    top_k: int | None = None,
    collection=None,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> list[Hit]:
    """按语义相似度召回最相关的若干片段。"""
    question = (question or "").strip()
    if not question:
        return []

    collection = collection if collection is not None else get_collection()
    total = collection.count()
    if total == 0:
        return []

    embed_fn = embed_fn or embed_query
    vector = embed_fn(question)

    k = max(1, min(settings.top_k if top_k is None else top_k, total))
    result = collection.query(query_embeddings=[vector], n_results=k)

    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    hits: list[Hit] = []
    for text, metadata, distance in zip(documents, metadatas, distances):
        metadata = metadata or {}
        hits.append(
            Hit(
                text=text,
                source=str(metadata.get("source", "未知来源")),
                location=str(metadata.get("location", "")),
                score=round(1.0 - float(distance), 4),
            )
        )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits


def _take(hits: Sequence[Hit], limit: int) -> list[Hit]:
    return list(hits[:limit])


def rerank_hits(
    question: str,
    hits: Sequence[Hit],
    top_k: int | None = None,
    rerank_fn: RerankFn | None = None,
) -> list[Hit]:
    """用精排模型给候选片段重新打分并排序。

    精排是锦上添花而非必需：接口出错（网络抖动、额度耗尽、模型不可用）时
    退回向量序，保证问答整体仍然可用。但**配置类错误会照常抛出** ——
    比如没配 API Key，那是环境问题，静默降级只会让人查半天。
    """
    candidates = list(hits)
    if not candidates:
        return []

    limit = len(candidates) if top_k is None else max(1, min(top_k, len(candidates)))
    rerank_fn = rerank_fn or rerank_documents

    try:
        scored = rerank_fn(question, [hit.text for hit in candidates], limit)
    except RuntimeError:
        raise
    except Exception:
        return _take(candidates, limit)

    ranked: list[Hit] = []
    seen: set[int] = set()
    for index, score in scored:
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            continue
        if index in seen:
            continue
        seen.add(index)
        ranked.append(replace(candidates[index], rerank_score=round(float(score), 4)))

    # 一条都没解析出来也退回向量序，避免因接口返回异常而丢掉全部结果
    return ranked[:limit] if ranked else _take(candidates, limit)


def search_ranked(
    question: str,
    top_k: int | None = None,
    collection=None,
    embed_fn: Callable[[str], list[float]] | None = None,
    rerank_fn: RerankFn | None = None,
) -> list[Hit]:
    """完整召回链路：先宽召回，再用精排截取最终 Top-K。

    关掉精排时等价于直接调用 ``search``。
    """
    k = settings.top_k if top_k is None else top_k

    if not settings.rerank_enabled:
        return search(question, top_k=k, collection=collection, embed_fn=embed_fn)

    # 候选池要比最终条数宽，精排才有腾挪空间
    candidates = max(k, settings.rerank_candidates, 1)
    candidates = min(candidates, RERANK_MAX_CANDIDATES)
    hits = search(question, top_k=candidates, collection=collection, embed_fn=embed_fn)

    # 库里就这么几条，全都要了，精排只是白花一次调用
    if len(hits) <= k:
        return hits

    return rerank_hits(question, hits, top_k=k, rerank_fn=rerank_fn)
