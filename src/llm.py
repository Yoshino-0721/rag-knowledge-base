"""智谱 GLM 调用封装。

智谱提供 OpenAI 兼容接口，因此直接复用官方 openai SDK，
只需把 base_url 指向 https://open.bigmodel.cn/api/paas/v4/ 。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable, Sequence

from openai import OpenAI

from src.config import settings

EMBED_BATCH_SIZE = 32


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """构造（并缓存）智谱客户端。缺少 API Key 时会抛出可读的错误。"""
    return OpenAI(api_key=settings.require_api_key(), base_url=settings.base_url)


def reset_client() -> None:
    """清掉缓存的客户端。界面里改了 API Key 后调用，让新配置立即生效。"""
    get_client.cache_clear()


def embed_texts(
    texts: Sequence[str],
    client: OpenAI | None = None,
) -> list[list[float]]:
    """批量把文本转成向量，内部按 EMBED_BATCH_SIZE 分批。"""
    items = [t for t in texts if t and t.strip()]
    if not items:
        return []

    client = client or get_client()
    extra = {"dimensions": settings.embed_dim} if settings.embed_dim else {}
    vectors: list[list[float]] = []

    for start in range(0, len(items), EMBED_BATCH_SIZE):
        batch = list(items[start : start + EMBED_BATCH_SIZE])
        response = client.embeddings.create(
            model=settings.embed_model, input=batch, **extra
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors.extend(list(item.embedding) for item in ordered)

    return vectors


def embed_query(text: str, client: OpenAI | None = None) -> list[float]:
    """把单条查询转成向量。"""
    vectors = embed_texts([text], client=client)
    return vectors[0] if vectors else []


def chat(
    messages: list[dict],
    client: OpenAI | None = None,
    temperature: float = 0.2,
) -> str:
    """调用对话模型，返回纯文本回答。"""
    client = client or get_client()
    response = client.chat.completions.create(
        model=settings.chat_model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


# 便于类型标注与测试注入
ChatFn = Callable[[list[dict]], str]
