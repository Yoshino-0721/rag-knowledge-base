"""组装 Prompt、调用 GLM 并生成带引用的回答。"""

from __future__ import annotations

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

    def as_dict(self) -> dict:
        return {
            "answer": self.text,
            "citations": self.citations,
            "query": self.query,
            "rewritten": self.rewritten,
        }


def build_citations(hits: Sequence[Hit]) -> list[dict]:
    """把召回结果转成前端可直接渲染的引用条目。"""
    return [
        {"index": index, **asdict(hit)}
        for index, hit in enumerate(hits, start=1)
    ]


def generate(
    question: str,
    hits: Sequence[Hit],
    history: Sequence[dict] | None = None,
    chat_fn: Callable[[list[dict]], str] | None = None,
) -> Answer:
    """基于召回片段生成回答，并附带引用。

    ``question`` 用用户的原话（回答更自然），``history`` 提供上下文。
    """
    chat_fn = chat_fn or chat
    reply = chat_fn(build_messages(question, hits, history))
    return Answer(text=reply, citations=build_citations(hits))
