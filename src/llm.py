"""智谱 GLM 调用封装。

智谱提供 OpenAI 兼容接口，因此直接复用官方 openai SDK，
只需把 base_url 指向 https://open.bigmodel.cn/api/paas/v4/ 。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable, Sequence

import httpx
from openai import OpenAI

from src.config import settings

EMBED_BATCH_SIZE = 32
# 精排是同步接口，候选多时耗时会明显长于对话，给足超时
RERANK_TIMEOUT = 60.0


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


def chat_turn(
    messages: list[dict],
    tools: Sequence[dict] | None = None,
    client: OpenAI | None = None,
    temperature: float = 0.2,
) -> dict:
    """调用一次对话模型，返回**归一化**的助手消息。

    和 ``chat()`` 的区别是这里保留了「模型想调工具」这件事。返回值形如::

        {"content": "...", "tool_calls": [{"id": ..., "name": ..., "arguments": "原始JSON字符串"}]}

    ``arguments`` 刻意保持字符串原样 —— 它要原封不动地回填进下一轮请求，
    解析成 dict 再序列化回去容易和模型原本的输出产生偏差。
    """
    client = client or get_client()
    request: dict = {
        "model": settings.chat_model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        request["tools"] = list(tools)

    message = client.chat.completions.create(**request).choices[0].message
    return {
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments or "{}",
            }
            for call in (message.tool_calls or [])
        ],
    }


def _post_rerank(payload: dict, client: httpx.Client | None = None) -> dict:
    """向智谱精排接口发一次请求。

    rerank **不是 OpenAI 兼容协议**（路径为 ``{base_url}/rerank``），
    所以这里不能用 openai SDK，直接发 HTTP；API Key 复用 ``ZHIPUAI_API_KEY``。
    """
    headers = {"Authorization": f"Bearer {settings.require_api_key()}"}

    if client is not None:
        response = client.post(settings.rerank_url, json=payload, headers=headers)
    else:
        with httpx.Client(timeout=RERANK_TIMEOUT) as owned:
            response = owned.post(settings.rerank_url, json=payload, headers=headers)

    response.raise_for_status()
    return response.json() or {}


def rerank_documents(
    query: str,
    documents: Sequence[str],
    top_n: int | None = None,
    client: httpx.Client | None = None,
) -> list[tuple[int, float]]:
    """用精排模型给候选文本打分，返回 ``[(候选原下标, 相关性得分), ...]``（得分降序）。

    下标是候选列表里的原始位置，调用方据此把分数映射回自己的数据结构。
    """
    items = list(documents)
    if not (query or "").strip() or not items:
        return []

    payload: dict = {
        "model": settings.rerank_model,
        "query": query,
        "documents": items,
        # 必须取原始分：接口默认返回的是 sigmoid 归一化后的分数，
        # 在候选彼此都相关时（个人知识库里的片段常常如此）会一起饱和到 1.0，
        # 既看不出高下，排序也失去了展示意义。
        "return_raw_scores": True,
    }
    if top_n:
        payload["top_n"] = top_n

    results = _post_rerank(payload, client=client).get("results") or []
    scored = [
        (int(item.get("index", -1)), float(item.get("relevance_score", 0.0)))
        for item in results
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


# 便于类型标注与测试注入
ChatFn = Callable[[list[dict]], str]
ChatTurnFn = Callable[[list[dict], list[dict] | None], dict]
RerankFn = Callable[[str, Sequence[str], int | None], list[tuple[int, float]]]
