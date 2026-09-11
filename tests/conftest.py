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
)


@pytest.fixture(autouse=True)
def restore_global_settings():
    """settings 是全局单例，用例改动后必须还原，避免互相污染。"""
    snapshot = {name: getattr(settings, name) for name in _TRACKED_FIELDS}
    yield
    for name, value in snapshot.items():
        setattr(settings, name, value)
