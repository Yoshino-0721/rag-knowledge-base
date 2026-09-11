"""追问改写：把依赖上下文的追问，改写成可以独立检索的问题。

多轮对话里用户会说「它的优势是什么」「那缺点呢」—— 这类句子含指代，
直接拿去向量检索基本命中不到内容。这里先让模型结合历史把指代补全，
得到一句能独立成立的问题，再去查库。
"""

from __future__ import annotations

from typing import Callable, Sequence

from src.config import settings
from src.generate import normalize_history
from src.llm import chat

REWRITE_SYSTEM_PROMPT = """你是一个检索查询改写器。

用户会给出【对话历史】和【最新提问】。最新提问常常依赖上下文，含有
「它」「这个」「上面提到的」等指代，单独拿去检索会找不到相关内容。

请把【最新提问】改写成一个**不依赖上下文、可独立检索**的问题：
1. 补全指代对象，保留原意与关键术语，不要改变提问意图。
2. 只输出改写后的问题本身：不要解释、不要引号、不要任何前缀或编号。
3. 如果最新提问本身已经完整独立，原样输出即可。"""

# 模型偶尔会加上「改写后：」之类的前缀，这里做一次兜底清理
_STRIP_PREFIXES = (
    "改写后的问题：",
    "改写后的问题:",
    "改写后：",
    "改写后:",
    "改写：",
    "改写:",
    "问题：",
    "问题:",
)

_QUOTE_CHARS = "\"'“”‘’「」"


def _clean(raw: str) -> str:
    """把模型输出整理成一句干净的问题。"""
    text = (raw or "").strip()
    if not text:
        return ""

    text = text.splitlines()[0].strip()  # 只取第一行，丢掉后面的解释
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text.strip(_QUOTE_CHARS).strip()


def _history_text(history: Sequence[dict]) -> str:
    lines = []
    for message in history:
        speaker = "用户" if message["role"] == "user" else "助手"
        lines.append(f"{speaker}：{message['content']}")
    return "\n".join(lines)


def rewrite_question(
    question: str,
    history: Sequence[dict] | None = None,
    chat_fn: Callable[[list[dict]], str] | None = None,
) -> str:
    """结合历史把追问改写成可独立检索的问题。

    以下情况一律原样返回原问题，保证链路不中断：
    没有历史、改写开关关闭、模型返回空、改写过程出错。
    但配置类错误（``RuntimeError``，例如未配 API Key）会照常抛出 ——
    那是环境问题，不该被静默吞掉。
    """
    question = (question or "").strip()
    if not question or not settings.rewrite_enabled:
        return question

    history = normalize_history(history)
    if not history:
        return question  # 首轮提问没有上下文可补全，省掉一次模型调用

    chat_fn = chat_fn or chat
    user_content = (
        f"【对话历史】\n{_history_text(history)}\n\n【最新提问】\n{question}"
    )

    try:
        rewritten = _clean(
            chat_fn(
                [
                    {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ]
            )
        )
    except RuntimeError:
        raise
    except Exception:
        return question

    return rewritten or question
