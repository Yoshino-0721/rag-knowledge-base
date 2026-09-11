"""共享测试夹具。"""

from __future__ import annotations

import pytest

from src.config import settings

_TRACKED_FIELDS = (
    "api_key",
    "base_url",
    "chat_model",
    "embed_model",
    "embed_dim",
    "top_k",
    "app_password",
    "history_turns",
    "rewrite_enabled",
    "rerank_enabled",
    "rerank_model",
    "rerank_candidates",
    "agent_enabled",
    "agent_max_steps",
)

# 每个用例开始前强制回到这组基线值，避免受开发者本机 .env 影响。
# 注意 agent_enabled 默认置 False：工具调用会真打外部接口，
# 用例必须显式打开（或直接注入工具说明书）才能走 Agent 路径。
_BASELINE = {
    "app_password": "",
    "history_turns": 6,
    "rewrite_enabled": True,
    "rerank_enabled": True,
    "rerank_model": "rerank",
    "rerank_candidates": 20,
    "agent_enabled": False,
    "agent_max_steps": 4,
}


@pytest.fixture(autouse=True)
def restore_global_settings():
    """settings 是全局单例，用例改动后必须还原，避免互相污染。"""
    snapshot = {name: getattr(settings, name) for name in _TRACKED_FIELDS}
    for name, value in _BASELINE.items():
        setattr(settings, name, value)
    yield
    for name, value in snapshot.items():
        setattr(settings, name, value)
