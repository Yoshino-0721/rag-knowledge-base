"""工具层测试：注册表、天气格式化、参数解析与错误收敛。

全部使用替身 HTTP 客户端，不发起真实网络请求。
"""

from __future__ import annotations

import json

import pytest

from src import tools
from src.config import settings
from src.tools import (
    MAX_FORECAST_DAYS,
    WMO_CODES,
    ToolError,
    available_tools,
    describe_call,
    describe_weather_code,
    format_weather,
    get_tool,
    get_weather,
    parse_arguments,
    run_tool,
    tool_specs,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """按调用顺序返回预设响应，并记录每次请求的 url / params。"""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        if not self._payloads:
            raise AssertionError(f"多了一次请求：{url}")
        return FakeResponse(self._payloads.pop(0))

    def close(self):
        self.closed = True


GEO_PAYLOAD = {
    "results": [
        {
            "name": "北京",
            "admin1": "北京市",
            "country": "中国",
            "latitude": 39.9075,
            "longitude": 116.39723,
        }
    ]
}

FORECAST_PAYLOAD = {
    "current": {
        "temperature_2m": 30.5,
        "apparent_temperature": 27.9,
        "relative_humidity_2m": 21,
        "weather_code": 0,
        "wind_speed_10m": 10.5,
    },
    "daily": {
        "time": ["2026-09-11", "2026-09-12"],
        "weather_code": [0, 2],
        "temperature_2m_max": [31.3, 28.3],
        "temperature_2m_min": [16.9, 17.5],
        "precipitation_probability_max": [0, 40],
    },
}


# ---------------- 天气代码 ----------------


def test_known_weather_code_is_translated():
    assert describe_weather_code(0) == "晴"
    assert describe_weather_code(95) == "雷阵雨"


def test_unknown_weather_code_is_reported_not_guessed():
    """没见过的代码要如实带出来，不能猜一个看起来合理的天气。"""
    assert "未知天气" in describe_weather_code(1234)
    assert "1234" in describe_weather_code(1234)
    assert describe_weather_code(None).startswith("未知天气")


def test_all_declared_codes_have_chinese_text():
    assert all(text and text.strip() for text in WMO_CODES.values())


# ---------------- 结果格式化 ----------------


def test_format_weather_includes_current_and_forecast():
    text = format_weather("北京 · 北京市 · 中国", FORECAST_PAYLOAD, days=2)

    assert "北京" in text
    assert "晴" in text and "30.5°C" in text
    assert "体感 27.9°C" in text and "湿度 21%" in text
    assert "2026-09-11" in text and "16.9 ~ 31.3°C" in text
    assert "2026-09-12" in text and "多云" in text
    assert "降水概率 40%" in text
    # 模型需要知道这是外部数据，才能在被问来源时如实回答
    assert "Open-Meteo" in text


def test_format_weather_only_keeps_requested_days():
    text = format_weather("北京", FORECAST_PAYLOAD, days=1)

    assert "2026-09-11" in text
    assert "2026-09-12" not in text


def test_format_weather_survives_missing_fields():
    """外部接口少给字段时应当降级展示，而不是整条查询失败。"""
    text = format_weather("某地", {"current": {"temperature_2m": 20.0}}, days=1)

    assert "20.0°C" in text
    assert "【预报】" not in text


def test_format_weather_handles_empty_payload():
    text = format_weather("某地", {}, days=1)

    assert "某地" in text
    assert "【当前】" not in text


# ---------------- 真实调用（替身 HTTP） ----------------


def test_get_weather_calls_geocode_then_forecast():
    client = FakeClient(GEO_PAYLOAD, FORECAST_PAYLOAD)

    text = get_weather("北京", days=2, client=client)

    assert len(client.calls) == 2
    assert client.calls[0]["url"] == tools.GEOCODE_URL
    assert client.calls[0]["params"]["name"] == "北京"
    assert client.calls[1]["url"] == tools.FORECAST_URL
    assert client.calls[1]["params"]["latitude"] == 39.9075
    assert client.calls[1]["params"]["longitude"] == 116.39723
    assert client.calls[1]["params"]["forecast_days"] == 2
    assert "北京 · 北京市 · 中国" in text


def test_get_weather_does_not_close_injected_client():
    """客户端由调用方持有时不该被我们关掉，否则并发下会互相踩。"""
    client = FakeClient(GEO_PAYLOAD, FORECAST_PAYLOAD)

    get_weather("北京", client=client)

    assert client.closed is False


def test_get_weather_clamps_absurd_days():
    client = FakeClient(GEO_PAYLOAD, FORECAST_PAYLOAD)

    get_weather("北京", days=999, client=client)

    assert client.calls[1]["params"]["forecast_days"] == MAX_FORECAST_DAYS


def test_get_weather_rejects_blank_city():
    with pytest.raises(ToolError, match="city"):
        get_weather("   ")


def test_get_weather_reports_unknown_city():
    client = FakeClient({"results": []})

    with pytest.raises(ToolError) as excinfo:
        get_weather("不存在的地方", client=client)

    assert "没有找到城市" in str(excinfo.value)
    assert len(client.calls) == 1, "城市都没定位到，不该再去请求天气"


# ---------------- 注册表 ----------------


def test_registry_exposes_openai_style_specs():
    specs = tool_specs()

    assert specs and all(spec["type"] == "function" for spec in specs)
    spec = next(s for s in specs if s["function"]["name"] == "get_weather")
    assert spec["function"]["parameters"]["required"] == ["city"]
    assert "city" in spec["function"]["parameters"]["properties"]


def test_available_tools_follows_agent_switch(monkeypatch):
    """关掉 Agent 就得一个工具都不给，否则模型会去调一个不该存在的东西。"""
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert available_tools() == []

    monkeypatch.setattr(settings, "agent_enabled", True)
    assert available_tools() != []


def test_describe_call_uses_chinese_label_and_argument():
    assert describe_call("get_weather", {"city": "北京"}) == "查天气（北京）"
    # 未知工具或没有可读参数时退化成名字本身，不能报错
    assert describe_call("unknown_tool", {}) == "unknown_tool"


# ---------------- 参数解析与执行 ----------------


def test_parse_arguments_accepts_json_string_and_dict():
    assert parse_arguments('{"city": "北京"}') == {"city": "北京"}
    assert parse_arguments({"city": "北京"}) == {"city": "北京"}
    assert parse_arguments(None) == {}
    assert parse_arguments("") == {}


def test_parse_arguments_rejects_broken_json():
    with pytest.raises(ToolError, match="合法 JSON"):
        parse_arguments('{"city": ')


def test_parse_arguments_rejects_non_object_json():
    with pytest.raises(ToolError, match="JSON 对象"):
        parse_arguments("[1, 2, 3]")


def test_run_tool_dispatches_to_real_runner(monkeypatch):
    monkeypatch.setitem(
        tools.TOOLS,
        "echo",
        tools.Tool(
            name="echo",
            label="回声",
            description="测试用",
            parameters={"type": "object", "properties": {}},
            runner=lambda **kwargs: json.dumps(kwargs, ensure_ascii=False),
        ),
    )

    assert run_tool("echo", {"a": 1}) == '{"a": 1}'


def test_run_tool_rejects_unknown_tool():
    with pytest.raises(ToolError, match="没有名为"):
        run_tool("nope", {})


def test_run_tool_wraps_bad_arguments_as_tool_error():
    """参数不对要说人话，并且这个信息会回填给模型让它重试。"""
    with pytest.raises(ToolError, match="参数不对"):
        run_tool("get_weather", {})


def test_get_tool_returns_none_for_unknown_name():
    assert get_tool("nope") is None
    assert get_tool("get_weather") is not None
