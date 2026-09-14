"""配置模块测试：.env 读写、占位值处理与 Key 脱敏。"""

from __future__ import annotations

import pytest

from src import config
from src.config import Settings, load_settings


def _make_settings(api_key: str) -> Settings:
    return Settings(
        api_key=api_key,
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        chat_model="glm-5.2",
        embed_model="embedding-3",
        embed_dim=1024,
        data_dir=config.PROJECT_ROOT / "data",
        storage_dir=config.PROJECT_ROOT / "storage",
        collection_name="knowledge_base",
        chunk_size=800,
        chunk_overlap=120,
        top_k=5,
    )


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """把 .env 指向临时文件，避免污染真实项目配置。"""
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    example_path.write_text(
        "# 智谱 AI 开放平台 API Key\nZHIPUAI_API_KEY=your-api-key-here\nTOP_K=5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "ENV_EXAMPLE_PATH", example_path)
    return env_path


def test_save_env_seeds_from_example_and_keeps_comments(temp_env):
    config.save_env({"ZHIPUAI_API_KEY": "k-123"})

    text = temp_env.read_text(encoding="utf-8")
    assert "ZHIPUAI_API_KEY=k-123" in text
    assert "# 智谱 AI 开放平台 API Key" in text, "注释应被保留"
    assert "TOP_K=5" in text, "未改动的键应保持原样"


def test_save_env_replaces_instead_of_duplicating(temp_env):
    config.save_env({"TOP_K": "9"})
    config.save_env({"TOP_K": "12"})

    lines = [
        line
        for line in temp_env.read_text(encoding="utf-8").splitlines()
        if line.startswith("TOP_K=")
    ]
    assert lines == ["TOP_K=12"]


def test_save_env_appends_new_key(temp_env):
    config.save_env({"EMBED_DIM": "512"})

    assert "EMBED_DIM=512" in temp_env.read_text(encoding="utf-8")


def test_apply_settings_updates_global_and_env(temp_env):
    config.apply_settings(api_key="k-abc", top_k=9)

    assert config.settings.api_key == "k-abc"
    assert config.settings.top_k == 9

    text = temp_env.read_text(encoding="utf-8")
    assert "ZHIPUAI_API_KEY=k-abc" in text
    assert "TOP_K=9" in text


def test_apply_settings_ignores_none_and_blank(temp_env):
    config.apply_settings(api_key=None)

    original_model = config.settings.chat_model
    config.apply_settings(chat_model="   ")

    assert config.settings.chat_model == original_model


def test_placeholder_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("ZHIPUAI_API_KEY", "your-api-key-here")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    assert load_settings().api_key == ""


def test_api_key_hint_masks_middle():
    assert _make_settings("abcdefghijklmnop").api_key_hint == "abcd****mnop"


def test_api_key_hint_handles_short_key():
    assert _make_settings("shortkey").api_key_hint == "****"


def test_api_key_hint_empty_when_unset():
    assert _make_settings("").api_key_hint == ""


def test_require_api_key_raises_with_actionable_message():
    with pytest.raises(RuntimeError, match="未配置 API Key"):
        _make_settings("").require_api_key()


def test_require_api_key_returns_key_when_set():
    assert _make_settings("k-1").require_api_key() == "k-1"


def test_rerank_url_joins_base_url_without_double_slash():
    """rerank 不是 OpenAI 兼容协议，路径要自己拼，注意别拼出 //rerank。"""
    assert _make_settings("k").rerank_url == "https://open.bigmodel.cn/api/paas/v4/rerank"


def test_rerank_url_handles_base_url_without_trailing_slash():
    settings = _make_settings("k")
    settings.base_url = "https://open.bigmodel.cn/api/paas/v4"

    assert settings.rerank_url == "https://open.bigmodel.cn/api/paas/v4/rerank"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("随便写的", False),
        # 空值等同于「没配」，应回退到默认值 true（与其它配置项语义一致）
        ("", True),
    ],
)
def test_load_settings_parses_boolean_env(monkeypatch, raw, expected):
    monkeypatch.setenv("RERANK_ENABLED", raw)

    assert load_settings().rerank_enabled is expected


def test_load_settings_multi_turn_defaults(monkeypatch):
    for name in (
        "HISTORY_TURNS",
        "QUERY_REWRITE",
        "RERANK_ENABLED",
        "RERANK_MODEL",
        "RERANK_CANDIDATES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.history_turns == 6
    assert settings.rewrite_enabled is True
    assert settings.rerank_enabled is True
    assert settings.rerank_model == "rerank"
    assert settings.rerank_candidates == 20


def test_load_settings_reads_multi_turn_overrides(monkeypatch):
    monkeypatch.setenv("HISTORY_TURNS", "3")
    monkeypatch.setenv("QUERY_REWRITE", "false")
    monkeypatch.setenv("RERANK_ENABLED", "false")
    monkeypatch.setenv("RERANK_CANDIDATES", "50")

    settings = load_settings()

    assert settings.history_turns == 3
    assert settings.rewrite_enabled is False
    assert settings.rerank_enabled is False
    assert settings.rerank_candidates == 50


def test_apply_settings_writes_booleans_in_lowercase(temp_env):
    """布尔值写进 .env 要便于手读，不能落成 Python 风格的 True / False。"""
    config.apply_settings(rerank_enabled=False)

    assert config.settings.rerank_enabled is False
    assert "RERANK_ENABLED=false" in temp_env.read_text(encoding="utf-8")


def test_apply_settings_updates_turn_and_rerank_fields(temp_env):
    config.apply_settings(history_turns=0, rerank_candidates=8)

    assert config.settings.history_turns == 0
    assert config.settings.rerank_candidates == 8

    text = temp_env.read_text(encoding="utf-8")
    assert "HISTORY_TURNS=0" in text
    assert "RERANK_CANDIDATES=8" in text
