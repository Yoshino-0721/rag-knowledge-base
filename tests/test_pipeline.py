"""问答流水线测试：改写 → 召回精排 → 生成的编排（各环节均用替身）。"""

from __future__ import annotations

import src.pipeline as pipeline
from src.agent import ToolCall
from src.config import settings
from src.pipeline import NO_HIT_ANSWER, answer_question
from src.retrieve import Hit

HITS = [Hit(text="集线器工作在物理层。", source="FX.pdf", location="第 1 页", score=0.62)]

HISTORY = [
    {"role": "user", "content": "集线器和交换机有什么区别？"},
    {"role": "assistant", "content": "集线器工作在物理层 [1]。"},
]


def test_blank_question_returns_guidance():
    answer = answer_question("   ")

    assert answer.text == NO_HIT_ANSWER
    assert answer.citations == []
    assert answer.rewritten is False


def test_no_hit_returns_guidance_and_keeps_query(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: [])

    answer = answer_question("库里没有的问题", rewrite_fn=lambda q, h: q)

    assert answer.text == NO_HIT_ANSWER
    assert answer.citations == []
    assert answer.query == "库里没有的问题"
    assert answer.rewritten is False


def test_rewritten_question_is_used_for_retrieval(monkeypatch):
    captured = {}

    def fake_search(question, **kwargs):
        captured["question"] = question
        return HITS

    monkeypatch.setattr(pipeline, "search_ranked", fake_search)

    answer = answer_question(
        "它的优势是什么？",
        history=HISTORY,
        rewrite_fn=lambda q, h: "交换机的优势是什么？",
        chat_fn=lambda messages: "交换机转发效率更高 [1]。",
    )

    assert captured["question"] == "交换机的优势是什么？", "检索要用补全后的问题"
    assert answer.rewritten is True
    assert answer.query == "交换机的优势是什么？"


def test_generation_uses_the_original_wording(monkeypatch):
    """检索用改写后的问题，但要让模型看到用户的原话与历史，回答才自然。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)
    captured = {}

    def fake_chat(messages):
        captured["messages"] = messages
        return "回答 [1]。"

    answer_question(
        "它的优势是什么？",
        history=HISTORY,
        rewrite_fn=lambda q, h: "交换机的优势是什么？",
        chat_fn=fake_chat,
    )

    messages = captured["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "它的优势是什么？" in messages[-1]["content"]
    assert "集线器工作在物理层。" in messages[-1]["content"]


def test_answer_carries_citations(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "问题", rewrite_fn=lambda q, h: q, chat_fn=lambda messages: "回答 [1]。"
    )

    assert answer.citations[0]["index"] == 1
    assert answer.citations[0]["source"] == "FX.pdf"
    assert answer.citations[0]["location"] == "第 1 页"


def test_rewrite_disabled_marks_not_rewritten(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_enabled", False)
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "它的优势是什么？", history=HISTORY, chat_fn=lambda messages: "回答"
    )

    assert answer.rewritten is False
    assert answer.query == "它的优势是什么？"


def test_as_dict_shape(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    payload = answer_question(
        "问题", rewrite_fn=lambda q, h: q, chat_fn=lambda messages: "回答"
    ).as_dict()

    assert set(payload) == {"answer", "citations", "query", "rewritten", "tool_calls"}


# ---------- 工具调用 ----------

TOOL_SPECS = [{"type": "function", "function": {"name": "get_weather"}}]


def _weather_turn_fn(calls, final="北京今天晴。"):
    """第一轮要求调工具，第二轮给出最终答案。"""

    def chat_turn_fn(messages, tool_specs=None):
        calls.append(tool_specs)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"id": "1", "name": "get_weather", "arguments": '{"city": "北京"}'}
                ],
            }
        return {"content": final, "tool_calls": []}

    return chat_turn_fn


def test_no_hit_with_tools_defers_to_the_model(monkeypatch):
    """挂了工具就不能在「没召回」处收口，否则天气类问题永远走不到工具那一步。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: [])
    calls = []
    answer = answer_question(
        "北京今天天气怎么样？",
        rewrite_fn=lambda q, h: q,
        tools=TOOL_SPECS,
        chat_turn_fn=_weather_turn_fn(calls),
        runner=lambda name, raw: ToolCall(name=name, label="查天气", result="晴，30.5°C"),
    )

    assert answer.text == "北京今天晴。"
    assert answer.text != NO_HIT_ANSWER
    assert [call["label"] for call in answer.tool_calls] == ["查天气"]
    assert calls[0] == TOOL_SPECS


def test_no_hit_without_tools_still_returns_guidance(monkeypatch):
    """没工具时行为不变：检索为空就直接给引导语，不做无意义的一次模型调用。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: [])

    answer = answer_question("库里没有的问题", rewrite_fn=lambda q, h: q, tools=[])

    assert answer.text == NO_HIT_ANSWER
    assert answer.tool_calls == []


def test_tools_follow_settings_when_not_given(monkeypatch):
    """不传 tools 时按 AGENT_ENABLED 自动决定。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: [])
    monkeypatch.setattr(settings, "agent_enabled", True)
    calls = []
    answer = answer_question(
        "北京今天天气怎么样？",
        rewrite_fn=lambda q, h: q,
        chat_turn_fn=_weather_turn_fn(calls),
        runner=lambda name, raw: ToolCall(name=name, label="查天气", result="晴"),
    )

    assert answer.text == "北京今天晴。"
    assert calls[0], "开关打开时说明书应自动带上"


def test_explicit_empty_tools_override_enabled_setting(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)
    monkeypatch.setattr(settings, "agent_enabled", True)

    answer = answer_question(
        "问题",
        rewrite_fn=lambda q, h: q,
        chat_fn=lambda messages: "回答",
        tools=[],
    )

    assert answer.text == "回答"
    assert answer.tool_calls == []


def test_hit_and_tool_can_coexist(monkeypatch):
    """知识库命中与工具调用不是二选一：参考资料在，工具结果也在。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)
    calls = []

    answer = answer_question(
        "北京今天天气怎么样？另外集线器工作在物理层吗？",
        rewrite_fn=lambda q, h: q,
        tools=TOOL_SPECS,
        chat_turn_fn=_weather_turn_fn(calls, final="北京今天晴。集线器工作在物理层 [1]。"),
        runner=lambda name, raw: ToolCall(name=name, label="查天气", result="晴"),
    )

    assert answer.text == "北京今天晴。集线器工作在物理层 [1]。"
    assert [call["source"] for call in answer.citations] == ["FX.pdf"]
    assert len(answer.tool_calls) == 1


def test_citations_are_dropped_when_answer_never_references_them(monkeypatch):
    """问天气时也会照常检索，但模型没用到参考资料就不能挂引用卡片 ——
    否则读者会以为天气数据来自这几份文档。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "北京今天天气怎么样？",
        rewrite_fn=lambda q, h: q,
        tools=TOOL_SPECS,
        chat_turn_fn=_weather_turn_fn([], final="北京今天晴，30.5°C。"),
        runner=lambda name, raw: ToolCall(name=name, label="查天气", result="晴"),
    )

    assert answer.citations == []
    assert len(answer.tool_calls) == 1


def test_citations_are_kept_when_answer_cites_them(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "问题", rewrite_fn=lambda q, h: q, chat_fn=lambda messages: "结论 [1]。"
    )

    assert len(answer.citations) == 1


def test_query_and_rewritten_are_kept_on_tool_answers(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: [])

    answer = answer_question(
        "它的天气怎么样？",
        history=HISTORY,
        rewrite_fn=lambda q, h: "北京的天气怎么样？",
        tools=TOOL_SPECS,
        chat_turn_fn=_weather_turn_fn([]),
        runner=lambda name, raw: ToolCall(name=name, label="查天气", result="晴"),
    )

    assert answer.rewritten is True
    assert answer.query == "北京的天气怎么样？"
