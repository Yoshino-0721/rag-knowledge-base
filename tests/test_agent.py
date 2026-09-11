"""工具调用循环测试。

循环的每一步都用替身：这里要验证的是「流程对不对」——
是否按协议回填了工具结果、是否守住了步数上限、失败是否被安全吸收。
"""

from __future__ import annotations

import pytest

from src import agent, tools
from src.agent import (
    STEP_LIMIT_ANSWER,
    AgentResult,
    ToolCall,
    build_tool_reply,
    execute_tool,
    normalize_tool_call,
    run_agent,
)

MESSAGES = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "【参考资料】\n（无参考资料）\n\n【问题】\n北京今天天气怎么样？"},
]

TOOL_SPECS = [{"type": "function", "function": {"name": "get_weather"}}]


def scripted(*replies):
    """按顺序返回预设回复，并记录每次请求看到的消息与工具列表。"""
    queue = list(replies)
    seen: list[dict] = []

    def chat_turn_fn(messages, tool_specs=None):
        seen.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": list(tool_specs) if tool_specs else [],
            }
        )
        if not queue:
            raise AssertionError(f"多要了一次回复：{seen[-1]['tools']}")
        return queue.pop(0)

    chat_turn_fn.seen = seen
    return chat_turn_fn


def tool_call_reply(name="get_weather", arguments='{"city": "北京"}', call_id="call_1"):
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
    }


def ok_runner(name, raw):
    record = ToolCall(name=name, label="查天气", result="【当前】晴，30.5°C")
    return record


# ---------------- 基础流程 ----------------


def test_no_tool_call_returns_directly():
    chat_turn_fn = scripted({"content": "这是直接回答。", "tool_calls": []})

    result = run_agent(MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn)

    assert result.text == "这是直接回答。"
    assert result.tool_calls == []
    assert result.steps == 1
    assert len(chat_turn_fn.seen) == 1
    assert chat_turn_fn.seen[0]["tools"] == TOOL_SPECS


def test_tool_call_is_executed_and_result_is_fed_back():
    chat_turn_fn = scripted(
        tool_call_reply(),
        {"content": "北京现在晴，30.5°C。", "tool_calls": []},
    )

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=ok_runner
    )

    assert result.text == "北京现在晴，30.5°C。"
    assert [call.name for call in result.tool_calls] == ["get_weather"]
    assert result.tool_calls[0].result == "【当前】晴，30.5°C"
    assert result.tool_calls[0].error == ""
    assert result.steps == 2

    # 第二轮请求必须带上「模型想调工具」+「工具返回了什么」这两条消息
    second = chat_turn_fn.seen[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assistant = second[-2]
    assert assistant["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
    }
    tool_message = second[-1]
    assert tool_message["tool_call_id"] == "call_1", "tool 消息的 id 必须对得上"
    assert "30.5°C" in tool_message["content"]


def test_multiple_tool_calls_in_one_round_are_all_executed():
    chat_turn_fn = scripted(
        {
            "content": "",
            "tool_calls": [
                {"id": "a", "name": "get_weather", "arguments": '{"city": "北京"}'},
                {"id": "b", "name": "get_weather", "arguments": '{"city": "上海"}'},
            ],
        },
        {"content": "两地天气如下。", "tool_calls": []},
    )

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=ok_runner
    )

    assert len(result.tool_calls) == 2
    roles = [m["role"] for m in chat_turn_fn.seen[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]


def test_empty_content_is_handled():
    chat_turn_fn = scripted({"content": None, "tool_calls": []})

    result = run_agent(MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn)

    assert result.text == ""


# ---------------- 步数上限 ----------------


def test_steps_are_capped_and_final_call_drops_tools():
    """模型来回调工具时必须收得住，否则会一直烧额度。"""
    chat_turn_fn = scripted(
        tool_call_reply(call_id="1"),
        tool_call_reply(call_id="2"),
        {"content": "综合来看……", "tool_calls": []},
    )

    result = run_agent(
        MESSAGES,
        TOOL_SPECS,
        chat_turn_fn=chat_turn_fn,
        runner=ok_runner,
        max_steps=2,
    )

    assert result.steps == 3
    assert len(result.tool_calls) == 2
    assert result.text == "综合来看……"
    # 收尾那次请求要把工具摘掉，逼模型直接作答
    assert chat_turn_fn.seen[-1]["tools"] == []


def test_step_limit_falls_back_to_a_readable_message():
    chat_turn_fn = scripted(tool_call_reply(call_id="1"), {"content": ""})

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=ok_runner, max_steps=1
    )

    assert result.text == STEP_LIMIT_ANSWER


def test_max_steps_from_settings(monkeypatch):
    monkeypatch.setattr(agent.settings, "agent_max_steps", 1)
    chat_turn_fn = scripted(tool_call_reply(call_id="1"), {"content": "收尾"})

    result = run_agent(MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=ok_runner)

    assert len(result.tool_calls) == 1
    assert result.text == "收尾"


def test_zero_or_negative_steps_still_runs_once(monkeypatch):
    """配置写成 0 是误操作，但至少要保证还能给出一次回答。"""
    chat_turn_fn = scripted({"content": "兜底回答", "tool_calls": []})

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=ok_runner, max_steps=0
    )

    assert result.text == "兜底回答"


# ---------------- 错误处理 ----------------


def test_unknown_tool_is_reported_to_model():
    chat_turn_fn = scripted(tool_call_reply(name="发邮件"), {"content": "换个方式回答。"})

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=execute_tool
    )

    assert "没有名为「发邮件」的工具" in result.tool_calls[0].error
    fed_back = chat_turn_fn.seen[1]["messages"][-1]["content"]
    assert "失败" in fed_back and "发邮件" in fed_back


def test_broken_json_arguments_are_reported_without_executing():
    chat_turn_fn = scripted(
        tool_call_reply(arguments='{"city": '), {"content": "参数错了，我直接说。"}
    )

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=execute_tool
    )

    assert "合法 JSON" in result.tool_calls[0].error
    assert result.tool_calls[0].result == ""


def test_tool_failure_is_fed_back_and_loop_continues():
    def failing_runner(name, raw):
        return ToolCall(name=name, label="查天气", error="网络超时")

    chat_turn_fn = scripted(tool_call_reply(), {"content": "暂时查不到天气。"})

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=failing_runner
    )

    assert result.text == "暂时查不到天气。"
    assert result.tool_calls[0].error == "网络超时"
    fed_back = chat_turn_fn.seen[1]["messages"][-1]["content"]
    assert "网络超时" in fed_back
    assert "重试" in fed_back, "要提示模型可以换参数重试"


def test_runtime_error_from_tool_is_not_swallowed(monkeypatch):
    """未配 API Key 属于环境问题，必须让它冒出来（接口层会转成 400）。"""

    def boom(**kwargs):
        raise RuntimeError("未配置 API Key")

    monkeypatch.setitem(
        tools.TOOLS,
        "boom",
        tools.Tool(
            name="boom",
            label="炸",
            description="测试用",
            parameters={"type": "object", "properties": {}},
            runner=boom,
        ),
    )

    with pytest.raises(RuntimeError, match="API Key"):
        execute_tool("boom", {})


# ---------------- 协议细节 ----------------


def test_normalize_tool_call_fills_missing_id():
    """没有 id 会导致下一轮 tool 消息对不上号，接口会直接报错。"""
    normalized = normalize_tool_call({"name": "get_weather"})

    assert normalized["id"]
    assert normalized["arguments"] == "{}"


def test_normalize_tool_call_keeps_raw_arguments_string():
    """参数要原样回填，解析成 dict 再序列化容易和模型原本的输出产生偏差。"""
    raw = '{"city": "北京",  "days": 2}'
    assert normalize_tool_call({"name": "get_weather", "arguments": raw})["arguments"] == raw


def test_original_messages_are_not_mutated():
    chat_turn_fn = scripted({"content": "回答", "tool_calls": []})
    messages = [dict(m) for m in MESSAGES]

    run_agent(messages, TOOL_SPECS, chat_turn_fn=chat_turn_fn)

    assert messages == MESSAGES, "循环内部应该操作副本，不能改调用方的列表"


def test_long_tool_result_is_truncated_in_prompt(monkeypatch):
    monkeypatch.setattr(agent, "RESULT_CHARS_IN_PROMPT", 10)
    chat_turn_fn = scripted(tool_call_reply(), {"content": "好的"})

    def long_runner(name, raw):
        return ToolCall(name=name, label="查天气", result="x" * 500)

    result = run_agent(
        MESSAGES, TOOL_SPECS, chat_turn_fn=chat_turn_fn, runner=long_runner
    )

    assert len(result.tool_calls[0].result) == 500, "记录里保留完整结果"
    fed_back = chat_turn_fn.seen[1]["messages"][-1]["content"]
    assert fed_back.startswith("x" * 10), "进 Prompt 的部分按上限截断"
    assert fed_back != "x" * 10, "截断后还要带上那句提醒"


def test_tool_reply_carries_a_multi_intent_reminder():
    """工具结果是最靠近生成位置的一条消息，多诉求的提醒必须挂在这里才管用。

    只在系统提示里写一句是不够的：实测模型会顺着工具结果只答那一半，
    把问题里属于知识库的部分丢掉。
    """
    reply = build_tool_reply(
        "get_weather", ToolCall(name="get_weather", result="【当前】晴")
    )

    assert "【当前】晴" in reply
    assert "每个诉求" in reply
    assert "引用编号" in reply


def test_failed_tool_reply_has_no_reminder():
    """工具都失败了，提醒模型「逐条答到、标注引用」没有意义，别添乱。"""
    reply = build_tool_reply("get_weather", ToolCall(name="get_weather", error="超时"))

    assert "每个诉求" not in reply


def test_build_tool_reply_marks_failure():
    reply = build_tool_reply("get_weather", ToolCall(name="get_weather", error="超时"))

    assert "失败" in reply and "超时" in reply


def test_agent_result_defaults():
    result = AgentResult(text="回答")

    assert result.tool_calls == []
    assert result.steps == 0


def test_tool_call_as_dict_shape():
    payload = ToolCall(name="get_weather", label="查天气", arguments={"city": "北京"}).as_dict()

    assert set(payload) == {"name", "label", "arguments", "result", "error"}
