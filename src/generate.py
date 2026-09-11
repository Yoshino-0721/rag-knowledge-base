"""组装 Prompt、调用 GLM 并生成带引用的回答。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from src.agent import AgentResult, ToolCall, run_agent
from src.config import settings
from src.llm import chat
from src.retrieve import Hit

SYSTEM_PROMPT = """你是一个严谨的个人知识库助手。请严格依据【参考资料】回答用户问题：

1. 只使用参考资料中的信息，不要依赖你的先验知识，更不要编造。
2. 如果参考资料不足以回答，请明确说明「知识库中没有相关内容」，并指出还缺什么。
3. 每个结论后面用 [编号] 标注来源，编号对应参考资料的序号。
4. 使用与提问相同的语言作答，条理清晰，直接给结论。
5. 可以结合【对话历史】理解用户的追问，但结论仍必须来自本轮【参考资料】。"""

# 只有真的挂了工具才追加 —— 否则会让模型以为有工具可用，凭空捏造调用
TOOL_PROMPT_SUFFIX = """
6. 当问题需要知识库以外的实时信息（例如天气）时，调用提供的工具获取，不要凭记忆回答。
7. 工具返回的结果视为可靠数据，用它作答时请说明数据来源；工具调用失败就如实说明，不要用猜测顶替。
8. 如果工具也帮不上忙，再按第 2 条说明知识库中没有相关内容。
9. 问题里如果同时有多个诉求（一部分在参考资料里、一部分要靠工具），要逐条都答到，不要只答其中一个。"""

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
    tools: Sequence[dict] | None = None,
) -> list[dict]:
    """构造发给对话模型的消息列表：system → 历史对话 → 本轮（参考资料 + 问题）。

    ``tools`` 非空时会在系统提示里补一段工具使用约束 —— 有工具和没工具，
    对模型的要求是不一样的，不能共用同一段提示词。
    """
    system_prompt = SYSTEM_PROMPT + TOOL_PROMPT_SUFFIX if tools else SYSTEM_PROMPT
    user_content = (
        f"【参考资料】\n{build_context(hits)}\n\n"
        f"【问题】\n{question.strip()}"
    )
    return [
        {"role": "system", "content": system_prompt},
        *normalize_history(history),
        {"role": "user", "content": user_content},
    ]


@dataclass
class Answer:
    """回答正文 + 结构化引用列表 + 本次实际用于检索的问题 + 工具调用记录。"""

    text: str
    citations: list[dict] = field(default_factory=list)
    query: str = ""
    rewritten: bool = False
    tool_calls: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "answer": self.text,
            "citations": self.citations,
            "query": self.query,
            "rewritten": self.rewritten,
            "tool_calls": self.tool_calls,
        }


def build_citations(hits: Sequence[Hit]) -> list[dict]:
    """把召回结果转成前端可直接渲染的引用条目。"""
    return [
        {"index": index, **asdict(hit)}
        for index, hit in enumerate(hits, start=1)
    ]


def used_citations(text: str, citations: list[dict]) -> list[dict]:
    """答案里一个 ``[n]`` 都没有时，判定本轮没用到参考资料，不展示引用卡片。

    检索是照常做的，所以问天气这类问题时命中几个不相干的片段很常见。
    如果照样把卡片挂在答案下面，读者会以为天气数据来自这几份文档 ——
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
    *,
    tools: Sequence[dict] | None = None,
    chat_turn_fn: Callable[[list[dict], list[dict] | None], dict] | None = None,
    runner: Callable[[str, object], ToolCall] | None = None,
    max_steps: int | None = None,
) -> Answer:
    """基于召回片段生成回答，并附带引用。

    ``question`` 用用户的原话（回答更自然），``history`` 提供上下文。

    ``tools`` 非空时走 Agent 循环：模型可以在需要实时信息时调用工具，
    再把工具结果和参考资料一起组织成回答。为空则是一次普通的对话请求，
    行为与没有工具层时完全一致。
    """
    messages = build_messages(question, hits, history, tools=tools)

    if tools:
        result: AgentResult = run_agent(
            messages,
            tools,
            chat_turn_fn=chat_turn_fn,
            runner=runner,
            max_steps=max_steps,
        )
        return Answer(
            text=result.text,
            citations=used_citations(result.text, build_citations(hits)),
            tool_calls=[call.as_dict() for call in result.tool_calls],
        )

    chat_fn = chat_fn or chat
    reply = chat_fn(messages)
    return Answer(text=reply, citations=used_citations(reply, build_citations(hits)))
