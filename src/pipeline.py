"""问答流水线：追问改写 → 召回精排 → 带引用生成（必要时调用工具）。

把编排逻辑从 HTTP 层抽出来，一是让 server.py 保持轻薄，
二是整条链路可以在不启动服务的情况下被完整测试。
"""

from __future__ import annotations

from typing import Callable, Sequence

from src.agent import ToolCall
from src.generate import Answer, generate, normalize_history
from src.retrieve import search_ranked
from src.rewrite import rewrite_question
from src.tools import available_tools

NO_HIT_ANSWER = "知识库中没有检索到相关内容。请先上传或放入文档并重建索引，再提问。"


def answer_question(
    question: str,
    history: Sequence[dict] | None = None,
    top_k: int | None = None,
    *,
    rewrite_fn: Callable[[str, Sequence[dict]], str] | None = None,
    chat_fn: Callable[[list[dict]], str] | None = None,
    chat_turn_fn: Callable[[list[dict], list[dict] | None], dict] | None = None,
    embed_fn: Callable[[str], list[float]] | None = None,
    rerank_fn=None,
    collection=None,
    tools: Sequence[dict] | None = None,
    runner: Callable[[str, object], ToolCall] | None = None,
    max_steps: int | None = None,
) -> Answer:
    """跑完整条问答链路，返回带引用的回答。

    各环节都可注入替身，方便单测；默认全部走真实实现。

    ``tools`` 传 ``None`` 表示按 ``AGENT_ENABLED`` 配置自动决定，
    传空列表则显式关闭工具能力。
    """
    question = (question or "").strip()
    if not question:
        return Answer(text=NO_HIT_ANSWER, query="", rewritten=False)

    history = normalize_history(history)
    tools = list(tools) if tools is not None else available_tools()

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

    # 3. 没命中就直说，不硬凑答案。
    #    但挂了工具时不能在这里收口 —— 否则「北京今天天气怎么样」这种
    #    知识库里本来就不该有的问题，永远走不到工具那一步。
    if not hits and not tools:
        return Answer(text=NO_HIT_ANSWER, query=standalone, rewritten=rewritten)

    # 4. 生成：用用户原话提问（读起来更自然），但结论只许来自本轮召回片段。
    #    召回为空但有工具时，参考资料栏会明确写「（无参考资料）」，由模型决定改调工具。
    answer = generate(
        question,
        hits,
        history=history,
        chat_fn=chat_fn,
        tools=tools,
        chat_turn_fn=chat_turn_fn,
        runner=runner,
        max_steps=max_steps,
    )
    answer.query = standalone
    answer.rewritten = rewritten
    return answer
