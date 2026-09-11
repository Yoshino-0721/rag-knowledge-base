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


def test_access_protected_flag_follows_password():
    settings = _make_settings("k")

    settings.app_password = "pw-123"
    assert settings.access_protected is True

    settings.app_password = ""
    assert settings.access_protected is False


def test_load_settings_reads_app_password(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw-456")

    assert load_settings().app_password == "pw-456"


def test_load_settings_defaults_app_password_to_empty(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    assert load_settings().app_password == ""
