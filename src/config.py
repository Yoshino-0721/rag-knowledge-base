"""集中配置：优先读取环境变量，其次读取项目根目录的 .env 文件。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True)
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

    @property
    def manifest_path(self) -> Path:
        """增量索引的清单文件，记录每个文件的内容哈希。"""
        return self.storage_dir / "manifest.json"

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "未配置 API Key。请复制 .env.example 为 .env 并填入 "
                "ZHIPUAI_API_KEY（申请地址：https://bigmodel.cn/usercenter/apikeys），"
                "或直接设置同名环境变量。"
            )
        return self.api_key


def load_settings() -> Settings:
    return Settings(
        api_key=_get("ZHIPUAI_API_KEY") or _get("ZAI_API_KEY"),
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
    )


settings = load_settings()
