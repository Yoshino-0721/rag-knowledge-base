"""Agent 可调用的外部工具（函数调用）。

工具集中登记在一张注册表里，每个工具自带两份东西：

- **说明书**（名称 / 中文标签 / 用途 / 参数 JSON Schema）—— 交给模型，由它决定何时调用；
- **执行函数** —— 真正去取数据，并把结果整理成模型能直接读的纯文本。

两者分开的好处是模型永远看不到原始接口响应，也就不会拿错字段。
所有执行失败统一收敛成 ``ToolError``，由上层决定是喂回模型还是抛出。

当前内置：

- ``get_weather``：查实时天气与短期预报，走 Open-Meteo（免费、无需密钥）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 15.0
MAX_FORECAST_DAYS = 7

# WMO 天气现象代码 → 中文描述（Open-Meteo 用的是这套编码）
WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "轻微冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}


class ToolError(RuntimeError):
    """工具无法完成调用（参数不合法、城市查不到、外部接口异常等）。

    这类错误的信息是写给模型看的 —— 它会作为工具结果回填，
    让模型有机会换个参数重试，而不是把整轮问答打断。
    """


def describe_weather_code(code: Any) -> str:
    """把 WMO 天气代码翻成中文；未知代码原样带出，不猜。"""
    try:
        return WMO_CODES.get(int(code), f"未知天气（代码 {code}）")
    except (TypeError, ValueError):
        return "未知天气"


def _number(value: Any, suffix: str = "", digits: int = 1) -> str | None:
    """把数值格式化成带单位的字符串；拿不到值就返回 None，由调用方跳过。"""
    if value is None:
        return None
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return None


def _join(parts: list[str | None]) -> str:
    return "，".join(part for part in parts if part)


def format_weather(place: str, payload: dict, days: int) -> str:
    """把 Open-Meteo 的响应整理成给模型读的纯文本。

    纯函数，方便单独测试；字段缺失时逐个跳过，不因为少一个字段就整体失败。
    """
    current = payload.get("current") or {}
    lines = [f"【地点】{place}"]

    if current:
        temperature = _number(current.get("temperature_2m"), "°C")
        feels_like = _number(current.get("apparent_temperature"), "°C")
        humidity = _number(current.get("relative_humidity_2m"), "%", 0)
        wind = _number(current.get("wind_speed_10m"), " km/h")
        now = _join(
            [
                describe_weather_code(current.get("weather_code")),
                temperature,
                f"体感 {feels_like}" if feels_like else None,
                f"湿度 {humidity}" if humidity else None,
                f"风速 {wind}" if wind else None,
            ]
        )
        if now:
            lines.append(f"【当前】{now}")

    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    rains = daily.get("precipitation_probability_max") or []

    forecast: list[str] = []
    for index, date in enumerate(dates[:days]):
        high = _number(highs[index] if index < len(highs) else None)
        low = _number(lows[index] if index < len(lows) else None)
        span = f"{low} ~ {high}°C" if low and high else None
        rain = _number(rains[index] if index < len(rains) else None, "", 0)
        forecast.append(
            "- "
            + _join(
                [
                    str(date),
                    describe_weather_code(codes[index]) if index < len(codes) else None,
                    span,
                    f"降水概率 {rain}%" if rain else None,
                ]
            )
        )
    if forecast:
        lines.append("【预报】\n" + "\n".join(forecast))

    lines.append("（数据来源：Open-Meteo 实时观测，非知识库内容）")
    return "\n".join(lines)


def get_weather(
    city: str,
    days: int = 1,
    client: httpx.Client | None = None,
) -> str:
    """查某个城市的实时天气与未来若干天预报。

    先用地理编码接口把城市名换成经纬度，再取天气 —— 这样「北京」「Shanghai」
    之类的写法都能识别，不需要模型知道坐标。
    """
    city = (city or "").strip()
    if not city:
        raise ToolError("缺少参数 city，请给出城市名称（如「北京」）")

    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(days, MAX_FORECAST_DAYS))

    owned = client is None
    http = client or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        try:
            geo = http.get(
                GEOCODE_URL,
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            ).json()
        except httpx.HTTPError as exc:
            raise ToolError(f"查询城市失败：{exc}") from exc

        results = geo.get("results") or []
        if not results:
            raise ToolError(
                f"没有找到城市「{city}」，请换一个更常见的写法（如「北京」「New York」）"
            )

        place = results[0]
        label = " · ".join(
            str(item)
            for item in (place.get("name"), place.get("admin1"), place.get("country"))
            if item
        )

        try:
            payload = http.get(
                FORECAST_URL,
                params={
                    "latitude": place.get("latitude"),
                    "longitude": place.get("longitude"),
                    "current": (
                        "temperature_2m,apparent_temperature,"
                        "relative_humidity_2m,weather_code,wind_speed_10m"
                    ),
                    "daily": (
                        "weather_code,temperature_2m_max,"
                        "temperature_2m_min,precipitation_probability_max"
                    ),
                    "timezone": "auto",
                    "forecast_days": days,
                },
            ).json()
        except httpx.HTTPError as exc:
            raise ToolError(f"获取天气数据失败：{exc}") from exc

        return format_weather(label or city, payload, days)
    finally:
        if owned:
            http.close()


@dataclass(frozen=True)
class Tool:
    """一个可供模型调用的工具。"""

    name: str
    label: str
    description: str
    parameters: dict
    runner: Callable[..., str]

    def spec(self) -> dict:
        """OpenAI 兼容的函数调用说明书。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOLS: dict[str, Tool] = {
    "get_weather": Tool(
        name="get_weather",
        label="查天气",
        description=(
            "查询指定城市的实时天气与未来几天预报。当用户询问天气、气温、下雨等"
            "知识库之外的实时信息时使用。城市名支持中文或英文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，中英文均可，例如「北京」「上海」「Tokyo」",
                },
                "days": {
                    "type": "integer",
                    "description": f"预报天数，1 表示只看今天，最多 {MAX_FORECAST_DAYS} 天",
                    "minimum": 1,
                    "maximum": MAX_FORECAST_DAYS,
                },
            },
            "required": ["city"],
        },
        runner=get_weather,
    ),
}


def get_tool(name: str) -> Tool | None:
    return TOOLS.get(name)


def tool_specs() -> list[dict]:
    """全部工具的说明书列表，交给模型决定是否调用。"""
    return [tool.spec() for tool in TOOLS.values()]


def available_tools() -> list[dict]:
    """当前实际可用的工具；Agent 关闭时一个都不给，行为退化成纯知识库问答。"""
    from src.config import settings  # 延迟导入，避免与 config 形成循环

    return tool_specs() if settings.agent_enabled else []


def describe_call(name: str, arguments: dict | None = None) -> str:
    """给界面用的一句话描述，例如「查天气（北京）」。"""
    tool = get_tool(name)
    label = tool.label if tool else name
    args = arguments or {}
    detail = args.get("city") or args.get("query")
    return f"{label}（{detail}）" if detail else label


def parse_arguments(raw: Any) -> dict:
    """解析模型给出的参数。

    模型偶尔会返回半个 JSON 或数组，这里统一收敛成 ``ToolError``，
    把「参数不对」这件事变成一句能读懂的话回填给模型。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        raise ToolError("参数格式不正确，请给出一个 JSON 对象")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"参数不是合法 JSON：{exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise ToolError("参数必须是一个 JSON 对象，例如 {\"city\": \"北京\"}")
    return parsed


def run_tool(name: str, arguments: dict | str | None = None) -> str:
    """执行一个工具，返回给模型看的文本结果。"""
    tool = get_tool(name)
    if tool is None:
        known = "、".join(TOOLS) or "（无）"
        raise ToolError(f"没有名为「{name}」的工具，可用工具：{known}")

    kwargs = parse_arguments(arguments)
    try:
        return str(tool.runner(**kwargs))
    except ToolError:
        raise
    except TypeError as exc:
        raise ToolError(f"调用 {tool.label} 的参数不对：{exc}") from exc
    except RuntimeError:
        # 未配 API Key 之类的环境问题要让人看到，不能变成一句「工具失败」被吞掉
        raise
    except Exception as exc:  # 外部接口/网络等意外，回填给模型让它换招
        raise ToolError(f"调用 {tool.label} 失败：{exc}") from exc
