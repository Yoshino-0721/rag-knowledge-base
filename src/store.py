"""Chroma 本地向量库封装：持久化到 storage/ 目录，重启不丢数据。"""

from __future__ import annotations

import chromadb

from src.config import settings


def get_collection(name: str | None = None, client: chromadb.ClientAPI | None = None):
    """获取（不存在则创建）向量集合。

    ``name`` 为空时用全局配置的集合名（兼容旧的单用户数据）；
    多用户模式下传入 ``user_<id>_docs`` 实现按用户隔离。
    """
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if client is None:
        client = chromadb.PersistentClient(path=str(settings.storage_dir))
    return client.get_or_create_collection(
        name=name or settings.collection_name,
        metadata={"hnsw:space": "cosine"},
    )
