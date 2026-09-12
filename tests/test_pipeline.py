"""问答流水线测试：改写 → 召回精排 → 生成的编排（各环节均用替身）。"""

from __future__ import annotations

import src.pipeline as pipeline
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

    assert set(payload) == {"answer", "citations", "query", "rewritten"}


def test_citations_are_dropped_when_answer_never_references_them(monkeypatch):
    """检索照常做，但模型没用到参考资料时不能挂引用卡片 ——
    否则读者会以为结论来自那几份文档。"""
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "无关的问题",
        rewrite_fn=lambda q, h: q,
        chat_fn=lambda messages: "这个问题知识库里没有相关内容。",
    )

    assert answer.citations == []
    assert answer.text != NO_HIT_ANSWER, "命中了就走生成，只是模型没用材料"


def test_citations_are_kept_when_answer_cites_them(monkeypatch):
    monkeypatch.setattr(pipeline, "search_ranked", lambda *args, **kwargs: HITS)

    answer = answer_question(
        "问题", rewrite_fn=lambda q, h: q, chat_fn=lambda messages: "结论 [1]。"
    )

    assert len(answer.citations) == 1
