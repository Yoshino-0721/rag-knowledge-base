"""追问改写测试：把带指代的追问补全成可独立检索的问题（不调用真实 API）。"""

from __future__ import annotations

import pytest

from src.config import settings
from src.rewrite import _clean, rewrite_question

HISTORY = [
    {"role": "user", "content": "集线器和交换机有什么区别？"},
    {"role": "assistant", "content": "集线器工作在物理层，交换机工作在数据链路层 [1]。"},
]


def test_without_history_returns_question_unchanged():
    """首轮提问没有上下文可补全，应省掉一次模型调用。"""
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        return "不该被调用"

    result = rewrite_question("什么是 RAG？", None, chat_fn=fake_chat)

    assert result == "什么是 RAG？"
    assert calls == []


def test_blank_question_short_circuits():
    assert rewrite_question("   ", HISTORY, chat_fn=lambda m: "改写") == ""


def test_disabled_rewrite_keeps_original(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_enabled", False)
    calls = []

    result = rewrite_question(
        "它的优势是什么？", HISTORY, chat_fn=lambda m: calls.append(m) or "改写过"
    )

    assert result == "它的优势是什么？"
    assert calls == [], "改写关闭时不应调用模型"


def test_rewrite_returns_model_output():
    result = rewrite_question("它的优势是什么？", HISTORY, chat_fn=lambda m: "交换机的优势是什么？")

    assert result == "交换机的优势是什么？"


def test_rewrite_prompt_carries_history_with_roles():
    captured = {}

    def fake_chat(messages):
        captured["messages"] = messages
        return "交换机的优势是什么？"

    rewrite_question("它的优势是什么？", HISTORY, chat_fn=fake_chat)

    assert captured["messages"][0]["role"] == "system"
    body = captured["messages"][1]["content"]
    assert "用户：集线器和交换机有什么区别？" in body
    assert "助手：集线器工作在物理层" in body
    assert "【最新提问】" in body and "它的优势是什么？" in body


def test_history_junk_is_filtered_before_prompting():
    captured = {}

    def fake_chat(messages):
        captured["body"] = messages[1]["content"]
        return "x"

    rewrite_question(
        "追问",
        [
            {"role": "system", "content": "不该出现"},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "有效问题"},
            "不是字典",
        ],
        chat_fn=fake_chat,
    )

    assert "有效问题" in captured["body"]
    assert "不该出现" not in captured["body"]


def test_empty_history_after_cleaning_makes_no_call():
    calls = []

    result = rewrite_question(
        "追问", [{"role": "user", "content": "  "}], chat_fn=lambda m: calls.append(m) or "x"
    )

    assert result == "追问"
    assert calls == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("交换机的优势是什么？", "交换机的优势是什么？"),
        ("改写后的问题：交换机的优势是什么？", "交换机的优势是什么？"),
        ("改写：交换机的优势是什么？", "交换机的优势是什么？"),
        ("问题：交换机的优势是什么？", "交换机的优势是什么？"),
        ("「交换机的优势是什么？」", "交换机的优势是什么？"),
        ('"交换机的优势是什么？"', "交换机的优势是什么？"),
        ("交换机的优势是什么？\n\n（这里补全了指代）", "交换机的优势是什么？"),
        ("   ", ""),
        ("", ""),
    ],
)
def test_clean_normalises_model_output(raw, expected):
    assert _clean(raw) == expected


def test_empty_model_reply_falls_back_to_question():
    assert rewrite_question("它的优势是什么？", HISTORY, chat_fn=lambda m: "   ") == "它的优势是什么？"


def test_model_failure_falls_back_to_question():
    """改写只是优化项，失败就退回原问题，不能让整条问答链路挂掉。"""

    def boom(messages):
        raise ValueError("网络抖动")

    assert rewrite_question("它的优势是什么？", HISTORY, chat_fn=boom) == "它的优势是什么？"


def test_config_error_propagates():
    """未配 API Key 是环境问题，不该被静默吞掉。"""

    def boom(messages):
        raise RuntimeError("未配置 API Key")

    with pytest.raises(RuntimeError):
        rewrite_question("它的优势是什么？", HISTORY, chat_fn=boom)
