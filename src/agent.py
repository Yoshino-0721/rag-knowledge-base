"""工具调用循环（Agent）。

把「模型请求调工具 → 执行 → 把结果回填 → 再请求模型」这一轮循环封装起来。

**这不是一个全自主 Agent**：知识库检索仍由 ``pipeline`` 前置完成（那是本项目
引用溯源的根基，不能让模型自己决定要不要查），这里只负责补上「知识库之外的
实时信息 / 外部动作」那一环。所以它更像 RAG 之上的一个工具层。

三条设计约束：

1. **有步数上限**。模型可能反复调工具，上限用尽后强制让它收尾作答，避免死循环。
2. **工具出错不打断问答**。失败信息作为工具结果回填，模型有机会换参数重试；
   整轮问答不会因为一次网络抖动就崩掉。
3. **配置类错误照常抛出**。未配 API Key 属于环境问题，静默吞掉只会让人查半天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from src.config import settings
from src.llm import chat_turn
from src.tools import ToolError, describe_call, get_tool, parse_arguments, run_tool

# 工具结果拼进 Prompt 时的字符上限，防止一次返回把上下文撑爆
RESULT_CHARS_IN_PROMPT = 2000

# 回填工具结果时顺带一句提醒。只写在系统提示里不够 ——
# 工具结果是最靠近生成位置的一条消息，模型很容易只顺着它作答，
# 把问题里「属于知识库的那一半」忘掉（实测过）。
TOOL_REPLY_REMINDER = "（回答时请把用户问题里的每个诉求都答到：属于知识库的部分同样要给出结论并标注引用编号。）"

STEP_LIMIT_ANSWER = "（已达到工具调用步数上限，未能得出最终结论。请把问题拆得更具体一些再试。）"


@dataclass
class ToolCall:
    """一次工具调用的记录，供界面展示与排错。"""

    name: str
    label: str = ""
    arguments: dict = field(default_factory=dict)
    result: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class AgentResult:
    """循环的产出：最终回答 + 过程中所有工具调用。"""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    steps: int = 0


def normalize_tool_call(raw: dict) -> dict:
    """补齐 OpenAI 协议要求的字段，缺 id 时补一个占位值。

    ``tool`` 角色消息必须带 ``tool_call_id`` 且与前一条 assistant 消息里的
    id 对得上，否则接口会直接报错，所以这里宁可补一个假的也不能留空。
    """
    name = str(raw.get("name") or "").strip()
    return {
        "id": str(raw.get("id") or f"call_{name or 'unknown'}"),
        "name": name,
        "arguments": raw.get("arguments") if raw.get("arguments") is not None else "{}",
    }


def execute_tool(name: str, raw_arguments) -> ToolCall:
    """执行一次工具调用，任何失败都转成 ``ToolCall.error`` 而不上抛。"""
    tool = get_tool(name)
    record = ToolCall(name=name, label=tool.label if tool else name)

    try:
        record.arguments = parse_arguments(raw_arguments)
    except ToolError as exc:
        # 参数都解析不了，就没必要真的去执行了
        record.error = str(exc)
        return record

    try:
        record.result = run_tool(name, record.arguments)
    except ToolError as exc:
        record.error = str(exc)
    except RuntimeError:
        raise  # 未配 Key 之类的环境问题要让人看到
    return record


def build_tool_reply(name: str, record: ToolCall) -> str:
    """工具结果回填给模型的文本：失败时要明说，否则模型会当成拿到了数据。"""
    if record.error:
        return (
            f"调用 {describe_call(name, record.arguments)} 失败：{record.error}。"
            "你可以调整参数重试，或直接说明无法获取。"
        )
    return record.result[:RESULT_CHARS_IN_PROMPT] + "\n" + TOOL_REPLY_REMINDER


def run_agent(
    messages: Sequence[dict],
    tools: Sequence[dict],
    *,
    chat_turn_fn: Callable[[list[dict], list[dict] | None], dict] | None = None,
    max_steps: int | None = None,
    runner: Callable[[str, object], ToolCall] | None = None,
) -> AgentResult:
    """跑工具调用循环，返回最终回答与调用记录。

    各环节均可注入替身，方便单测；默认走真实实现。
    """
    chat_turn_fn = chat_turn_fn or chat_turn
    runner = runner or execute_tool
    steps = max_steps if max_steps is not None else settings.agent_max_steps
    steps = max(1, int(steps))

    convo: list[dict] = [dict(message) for message in messages]
    calls: list[ToolCall] = []
    text = ""
    rounds = 0

    for _ in range(steps):
        rounds += 1
        reply = chat_turn_fn(convo, list(tools)) or {}
        text = str(reply.get("content") or "").strip()
        pending = [normalize_tool_call(raw) for raw in (reply.get("tool_calls") or [])]

        # 模型没提要调工具，这轮就是最终回答
        if not pending:
            return AgentResult(text=text, tool_calls=calls, steps=rounds)

        # 把模型的这次决定写回对话，工具结果才能按 id 对号入座
        convo.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    for call in pending
                ],
            }
        )

        for call in pending:
            record = runner(call["name"], call["arguments"])
            calls.append(record)
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": build_tool_reply(call["name"], record),
                }
            )

    # 步数用尽仍未收敛：摘掉工具再问一次，强制它把已知信息组织成回答
    final = chat_turn_fn(convo, None) or {}
    return AgentResult(
        text=str(final.get("content") or "").strip() or text or STEP_LIMIT_ANSWER,
        tool_calls=calls,
        steps=rounds + 1,
    )
