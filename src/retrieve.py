"""Top-K 向量检索。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.config import settings
from src.llm import embed_query
from src.store import get_collection


@dataclass(frozen=True)
class Hit:
    """一条召回结果。score 为余弦相似度（越大越相关）。"""

    text: str
    source: str
    location: str
    score: float


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
