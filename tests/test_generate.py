"""回答组装测试：上下文编号、消息构造、历史裁剪与引用格式（不调用真实 API）。"""

from __future__ import annotations

import src.generate as generate_module
from src.agent import ToolCall
from src.config import settings
from src.generate import (
    build_citations,
    build_context,
    build_messages,
    generate,
    normalize_history,
    used_citations,
)
from src.retrieve import Hit

HITS = [
    Hit(text="RAG 是检索增强生成。", source="notes/rag.md", location="", score=0.92),
    Hit(text="向量库用于相似度检索。", source="docs/intro.pdf", location="第 3 页", score=0.81),
]


def test_build_context_numbers_each_hit():
    context = build_context(HITS)

    assert "[1] 来源：notes/rag.md" in context
    assert "[2] 来源：docs/intro.pdf · 第 3 页" in context
    assert "RAG 是检索增强生成。" in context


def test_build_context_without_hits_uses_placeholder():
    assert "无参考资料" in build_context([])


def test_build_messages_contains_system_question_and_context():
    messages = build_messages("什么是 RAG？", HITS)

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "什么是 RAG？" in messages[1]["content"]
    assert "RAG 是检索增强生成。" in messages[1]["content"]


def test_generate_returns_reply_and_citations():
    captured = {}

    def fake_chat(messages):
        captured["messages"] = messages
        return "RAG 是检索增强生成 [1]。"

    answer = generate("什么是 RAG？", HITS, chat_fn=fake_chat)

    assert answer.text.startswith("RAG 是检索增强生成")
    assert len(answer.citations) == 2
    assert answer.citations[0]["index"] == 1
    assert answer.citations[0]["source"] == "notes/rag.md"
    assert answer.citations[1]["location"] == "第 3 页"
    assert "什么是 RAG？" in captured["messages"][1]["content"]


def test_build_citations_matches_input_order():
    citations = build_citations(HITS)

    assert [c["index"] for c in citations] == [1, 2]
    assert citations[0]["score"] == 0.92


# ---------- 引用过滤 ----------


def test_used_citations_dropped_when_answer_has_no_marker():
    """答案里没有任何 [n] → 本轮没用到参考资料 → 不展示引用卡片。

    否则「问天气、下面却挂着五张文档卡片」会让人误以为数据来自这些文档。
    """
    citations = build_citations(HITS)

    assert used_citations("北京今天晴，30.5°C。", citations) == []
    assert used_citations("", citations) == []


def test_used_citations_kept_when_answer_cites():
    citations = build_citations(HITS)

    assert used_citations("结论一 [1]，结论二 [2]。", citations) == citations
    assert used_citations("只有一个角标 [12]。", citations) == citations


def test_used_citations_ignores_non_numeric_brackets():
    citations = build_citations(HITS)

    assert used_citations("见 [附录] 或 [a] 的说明。", citations) == []


def test_answer_as_dict_shape():
    payload = generate("问题", HITS, chat_fn=lambda _: "回答").as_dict()

    assert set(payload) == {"answer", "citations", "query", "rewritten", "tool_calls"}
    assert payload["answer"] == "回答"
    assert payload["tool_calls"] == []


# ---------- 工具调用 ----------

TOOL_SPECS = [{"type": "function", "function": {"name": "get_weather"}}]


def test_build_messages_without_tools_keeps_original_prompt():
    """没挂工具时不能提工具，否则模型会以为有工具可用而凭空捏造调用。"""
    messages = build_messages("什么是 RAG？", HITS)

    assert "工具" not in messages[0]["content"]


def test_build_messages_with_tools_adds_usage_rules():
    messages = build_messages("什么是 RAG？", HITS, tools=TOOL_SPECS)
    system = messages[0]["content"]

    assert "工具" in system
    assert "参考资料" in system, "原有的引用约束不能被工具说明顶掉"
    # 实测发现：只答其中一个诉求是这个场景下最常见的翻车方式
    assert "逐条" in system, "要提醒模型不要漏答多诉求问题"


def test_build_messages_without_hits_still_reports_no_context():
    messages = build_messages("北京天气", [], tools=TOOL_SPECS)

    assert "无参考资料" in messages[-1]["content"]


def test_generate_with_tools_runs_agent_loop():
    seen = []

    def fake_chat_turn(messages, tool_specs=None):
        seen.append(tool_specs)
        if len(seen) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"id": "1", "name": "get_weather", "arguments": '{"city": "北京"}'}
                ],
            }
        return {"content": "北京今天晴，30.5°C。", "tool_calls": []}

    answer = generate(
        "北京今天天气怎么样？",
        [],
        tools=TOOL_SPECS,
        chat_turn_fn=fake_chat_turn,
        runner=lambda name, raw: ToolCall(
            name=name, label="查天气", result="【当前】晴，30.5°C"
        ),
    )

    assert answer.text == "北京今天晴，30.5°C。"
    assert answer.citations == [], "纯工具回答没有知识库引用"
    assert [call["name"] for call in answer.tool_calls] == ["get_weather"]
    assert answer.tool_calls[0]["label"] == "查天气"
    assert seen[0] == TOOL_SPECS, "说明书要真的传给模型"


def test_generate_without_tools_never_builds_agent_loop():
    """关掉工具时要走原来的单次对话，别把 Agent 循环捎带上。"""
    called = []

    def fake_chat(messages):
        called.append(messages)
        return "回答"

    answer = generate("问题", HITS, chat_fn=fake_chat)

    assert answer.text == "回答"
    assert answer.tool_calls == []
    assert len(called) == 1


# ---------- 多轮对话历史 ----------


HISTORY = [
    {"role": "user", "content": "前一个问题"},
    {"role": "assistant", "content": "前一个回答 [1]。"},
]


def test_build_messages_inserts_history_between_system_and_question():
    messages = build_messages("追问", HITS, HISTORY)

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "前一个问题"
    assert messages[2]["content"] == "前一个回答 [1]。"
    assert "追问" in messages[-1]["content"]
    assert "参考资料" in messages[-1]["content"]


def test_generate_passes_history_into_messages():
    captured = {}

    def fake_chat(messages):
        captured["messages"] = messages
        return "回答"

    generate("追问", HITS, HISTORY, chat_fn=fake_chat)

    assert [m["role"] for m in captured["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


def test_normalize_history_handles_missing_input():
    assert normalize_history(None) == []
    assert normalize_history([]) == []


def test_normalize_history_filters_invalid_entries():
    cleaned = normalize_history(
        [
            {"role": "user", "content": "有效"},
            {"role": "system", "content": "系统消息不该进历史"},
            {"role": "assistant", "content": "   "},
            {"role": "USER", "content": "角色大小写应被归一"},
            "不是字典",
            {"content": "缺 role"},
        ]
    )

    assert cleaned == [
        {"role": "user", "content": "有效"},
        {"role": "user", "content": "角色大小写应被归一"},
    ]


def test_normalize_history_keeps_only_recent_turns(monkeypatch):
    monkeypatch.setattr(settings, "history_turns", 2)
    history = [
        {"role": "user", "content": "第1问"},
        {"role": "assistant", "content": "第1答"},
        {"role": "user", "content": "第2问"},
        {"role": "assistant", "content": "第2答"},
        {"role": "user", "content": "第3问"},
        {"role": "assistant", "content": "第3答"},
    ]

    cleaned = normalize_history(history)

    assert [m["content"] for m in cleaned] == ["第2问", "第2答", "第3问", "第3答"]


def test_normalize_history_zero_turns_disables_memory(monkeypatch):
    """0 表示「不记忆上下文」，别写成 cleaned[-0:] 反而把全部历史留下。"""
    monkeypatch.setattr(settings, "history_turns", 0)

    assert normalize_history([{"role": "user", "content": "内容"}]) == []


def test_normalize_history_respects_char_budget(monkeypatch):
    monkeypatch.setattr(settings, "history_turns", 10)
    monkeypatch.setattr(generate_module, "MAX_HISTORY_CHARS", 10)

    cleaned = normalize_history(
        [
            {"role": "user", "content": "一" * 8},
            {"role": "assistant", "content": "二" * 8},
        ]
    )

    assert cleaned == [{"role": "assistant", "content": "二" * 8}], "只保留能塞进预算的最近消息"
