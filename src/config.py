"""集中配置：优先读取环境变量，其次读取项目根目录的 .env 文件。

``settings`` 是全局单例，其它模块直接持有其引用；通过 ``apply_settings()``
可以就地更新字段并写回 .env，改动对所有模块立即可见，无需重启进程。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

load_dotenv(ENV_PATH)

# 占位值不应被当成真实配置
_PLACEHOLDERS = {"your-api-key-here", "your-api-key", "changeme", "none"}

# 字段名 ↔ 环境变量名
FIELD_TO_ENV = {
    "api_key": "ZHIPUAI_API_KEY",
    "base_url": "ZHIPU_BASE_URL",
    "chat_model": "CHAT_MODEL",
    "embed_model": "EMBED_MODEL",
    "embed_dim": "EMBED_DIM",
    "top_k": "TOP_K",
    "history_turns": "HISTORY_TURNS",
    "rewrite_enabled": "QUERY_REWRITE",
    "rerank_enabled": "RERANK_ENABLED",
    "rerank_model": "RERANK_MODEL",
    "rerank_candidates": "RERANK_CANDIDATES",
}

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _get_bool(name: str, default: str = "false") -> bool:
    return _get(name, default).lower() in _TRUE_VALUES


@dataclass
class Settings:
    """运行期配置。字段全部来自环境变量，缺省时使用合理默认值。"""

    api_key: str
    base_url: str
    chat_model: str
    embed_model: str
    embed_dim: int
    data_dir: Path
    storage_dir: Path
    collection_name: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    app_password: str = ""
    # 多轮对话
    history_turns: int = 6
    rewrite_enabled: bool = True
    # 精排
    rerank_enabled: bool = True
    rerank_model: str = "rerank"
    rerank_candidates: int = 20

    @property
    def access_protected(self) -> bool:
        """是否启用了访问口令。公网部署时建议开启，避免陌生人消耗你的 API Key。"""
        return bool(self.app_password)

    @property
    def rerank_url(self) -> str:
        """精排接口地址。

        智谱的 rerank 与对话 / 向量接口不同，**不是 OpenAI 兼容协议**，
        需要自己在 base_url 后面拼 ``/rerank``。
        """
        return f"{self.base_url.rstrip('/')}/rerank"

    @property
    def manifest_path(self) -> Path:
        """增量索引的清单文件，记录每个文件的内容哈希。"""
        return self.storage_dir / "manifest.json"

    @property
    def api_key_hint(self) -> str:
        """脱敏后的 Key，用于在界面上展示「是否已配置」。"""
        if not self.api_key:
            return ""
        if len(self.api_key) <= 10:
            return "****"
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "未配置 API Key。请点击界面右上角「设置」填入 API Key，"
                "或复制 .env.example 为 .env 后写入 ZHIPUAI_API_KEY"
                "（申请地址：https://bigmodel.cn/usercenter/apikeys）。"
            )
        return self.api_key


def load_settings() -> Settings:
    api_key = _get("ZHIPUAI_API_KEY") or _get("ZAI_API_KEY")
    if api_key.lower() in _PLACEHOLDERS:
        api_key = ""

    return Settings(
        api_key=api_key,
        base_url=_get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        chat_model=_get("CHAT_MODEL", "glm-5.2"),
        embed_model=_get("EMBED_MODEL", "embedding-3"),
        embed_dim=int(_get("EMBED_DIM", "1024")),
        data_dir=Path(_get("DATA_DIR", str(PROJECT_ROOT / "data"))),
        storage_dir=Path(_get("STORAGE_DIR", str(PROJECT_ROOT / "storage"))),
        collection_name=_get("COLLECTION", "knowledge_base"),
        chunk_size=int(_get("CHUNK_SIZE", "800")),
        chunk_overlap=int(_get("CHUNK_OVERLAP", "120")),
        top_k=int(_get("TOP_K", "5")),
        app_password=_get("APP_PASSWORD"),
        history_turns=int(_get("HISTORY_TURNS", "6")),
        rewrite_enabled=_get_bool("QUERY_REWRITE", "true"),
        rerank_enabled=_get_bool("RERANK_ENABLED", "true"),
        rerank_model=_get("RERANK_MODEL", "rerank"),
        rerank_candidates=int(_get("RERANK_CANDIDATES", "20")),
    )


def save_env(updates: dict[str, str]) -> Path:
    """把配置写回 .env，保留已有注释与其它键。

    .env 不存在时以 .env.example 为模板，这样注释说明不会丢。
    """
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    elif ENV_EXAMPLE_PATH.exists():
        lines = ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    pending = dict(updates)
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                result.append(f"{key}={pending.pop(key)}")
                continue
        result.append(line)

    for key, value in pending.items():
        result.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
    return ENV_PATH


def apply_settings(**changes) -> Settings:
    """就地更新全局配置并写回 .env；``None`` 表示该字段不变。"""
    updates: dict[str, str] = {}

    for field, value in changes.items():
        env_name = FIELD_TO_ENV.get(field)
        if env_name is None or value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        setattr(settings, field, value)
        # 布尔值写进 .env 时统一成 true / false，便于手工阅读
        updates[env_name] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )

    if updates:
        save_env(updates)
    return settings


settings = load_settings()
