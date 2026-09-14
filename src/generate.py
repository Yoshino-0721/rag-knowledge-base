"""组装 Prompt、调用 GLM 并生成带引用的回答。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from src.config import settings
from src.llm import chat
from src.retrieve import Hit

SYSTEM_PROMPT = """你是一个严谨的个人知识库助手。请严格依据【参考资料】回答用户问题：

1. 只使用参考资料中的信息，不要依赖你的先验知识，更不要编造。
2. 如果参考资料不足以回答，请明确说明「知识库中没有相关内容」，并指出还缺什么。
3. 每个结论后面用 [编号] 标注来源，编号对应参考资料的序号。
4. 使用与提问相同的语言作答，条理清晰，直接给结论。
5. 可以结合【对话历史】理解用户的追问，但结论仍必须来自本轮【参考资料】。"""

NO_CONTEXT_HINT = "（无参考资料）"

# 历史拼进 Prompt 的字符预算，避免长对话把上下文撑爆
MAX_HISTORY_CHARS = 4000

# 一轮 = 一问一答
MESSAGES_PER_TURN = 2

# 正文里的引用角标，例如 [1] [12]
CITATION_PATTERN = re.compile(r"\[\d+\]")


def normalize_history(history: Sequence[dict] | None) -> list[dict]:
    """清洗并裁剪对话历史。

    只保留 role 为 user / assistant 且内容非空的消息；先按 ``history_turns``
    轮截取最近的若干条，再按字符预算从最近往回裁剪。
    """
    if not history:
        return []

    cleaned: list[dict] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip().lower()
        content = str(turn.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            cleaned.append({"role": role, "content": content})

    turns = settings.history_turns
    if turns <= 0:
        return []  # 注意别写成 cleaned[-turns * 2:]，0 会变成「取全部」
    cleaned = cleaned[-turns * MESSAGES_PER_TURN :]

    kept: list[dict] = []
    budget = MAX_HISTORY_CHARS
    for message in reversed(cleaned):
        cost = len(message["content"])
        if cost > budget:
            break
        budget -= cost
        kept.append(message)
    return list(reversed(kept))


def build_context(hits: Sequence[Hit]) -> str:
    """把召回片段拼成带编号的上下文，供模型引用。"""
    if not hits:
        return NO_CONTEXT_HINT

    blocks = []
    for index, hit in enumerate(hits, start=1):
        location = f" · {hit.location}" if hit.location else ""
        blocks.append(f"[{index}] 来源：{hit.source}{location}\n{hit.text}")
    return "\n\n".join(blocks)


def build_messages(
    question: str,
    hits: Sequence[Hit],
    history: Sequence[dict] | None = None,
) -> list[dict]:
    """构造发给对话模型的消息列表：system → 历史对话 → 本轮（参考资料 + 问题）。"""
    user_content = (
        f"【参考资料】\n{build_context(hits)}\n\n"
        f"【问题】\n{question.strip()}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *normalize_history(history),
        {"role": "user", "content": user_content},
    ]


@dataclass
class Answer:
    """回答正文 + 结构化引用列表 + 本次实际用于检索的问题。"""

    text: str
    citations: list[dict] = field(default_factory=list)
    query: str = ""
    rewritten: bool = False
    # 精排降级信息：None = 成功或未启用；分类词 = 已降级到向量序（见 retrieve.py）
    rerank_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "answer": self.text,
            "citations": self.citations,
            "query": self.query,
            "rerank_failed": self.rerank_error is not None,
            "rerank_error": self.rerank_error,
            "rewritten": self.rewritten,
        }


def build_citations(hits: Sequence[Hit]) -> list[dict]:
    """把召回结果转成前端可直接渲染的引用条目。"""
    return [
        {"index": index, **asdict(hit)}
        for index, hit in enumerate(hits, start=1)
    ]


def used_citations(text: str, citations: list[dict]) -> list[dict]:
    """答案里一个 ``[n]`` 都没有时，判定本轮没用到参考资料，不展示引用卡片。

    检索是照常做的，所以模型偶尔脱离材料作答时命中片段仍在。
    如果照样把卡片挂在答案下面，读者会以为结论来自这几份文档 ——
    引用本该是「能核对」的凭据，挂错地方就成了误导。

    只做这一层过滤，不按编号逐条裁剪：召回集合代表「模型本轮看到了什么」，
    保留全部反而更透明，编号也对得上正文里的角标。
    """
    return citations if CITATION_PATTERN.search(text or "") else []


def generate(
    question: str,
    hits: Sequence[Hit],
    history: Sequence[dict] | None = None,
    chat_fn: Callable[[list[dict]], str] | None = None,
) -> Answer:
    """基于召回片段生成回答，并附带引用。

    ``question`` 用用户的原话（回答更自然），``history`` 提供上下文。
    """
    messages = build_messages(question, hits, history)
    chat_fn = chat_fn or chat
    reply = chat_fn(messages)
    return Answer(text=reply, citations=used_citations(reply, build_citations(hits)))
