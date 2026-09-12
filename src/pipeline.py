"""问答流水线：追问改写 → 召回精排 → 带引用生成。

把编排逻辑从 HTTP 层抽出来，一是让 server.py 保持轻薄，
二是整条链路可以在不启动服务的情况下被完整测试。
"""

from __future__ import annotations

from typing import Callable, Sequence

from src.generate import Answer, generate, normalize_history
from src.retrieve import search_ranked
from src.rewrite import rewrite_question

NO_HIT_ANSWER = "知识库中没有检索到相关内容。请先上传或放入文档并重建索引，再提问。"


def answer_question(
    question: str,
    history: Sequence[dict] | None = None,
    top_k: int | None = None,
    *,
    rewrite_fn: Callable[[str, Sequence[dict]], str] | None = None,
    chat_fn: Callable[[list[dict]], str] | None = None,
    embed_fn: Callable[[str], list[float]] | None = None,
    rerank_fn=None,
    collection=None,
) -> Answer:
    """跑完整条问答链路，返回带引用的回答。

    各环节都可注入替身，方便单测；默认全部走真实实现。
    """
    question = (question or "").strip()
    if not question:
        return Answer(text=NO_HIT_ANSWER, query="", rewritten=False)

    history = normalize_history(history)

    # 1. 追问改写：让「它的优势是什么」变成一句能独立检索的问题
    if rewrite_fn is not None:
        standalone = rewrite_fn(question, history) or question
    else:
        standalone = rewrite_question(question, history, chat_fn=chat_fn) or question
    rewritten = standalone != question

    # 2. 宽召回 + 精排
    hits = search_ranked(
        standalone,
        top_k=top_k,
        collection=collection,
        embed_fn=embed_fn,
        rerank_fn=rerank_fn,
    )

    # 3. 没命中就直说，不硬凑答案 —— 知识库答不了的问题不该由模型自由发挥
    if not hits:
        return Answer(text=NO_HIT_ANSWER, query=standalone, rewritten=rewritten)

    # 4. 生成：用用户原话提问（读起来更自然），但结论只许来自本轮召回片段
    answer = generate(question, hits, history=history, chat_fn=chat_fn)
    answer.query = standalone
    answer.rewritten = rewritten
    return answer
