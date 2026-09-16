"""Chroma 本地向量库封装：持久化到 storage/ 目录，重启不丢数据。"""

from __future__ import annotations

import chromadb

from src.config import settings


def existing_collection(name: str, client: chromadb.ClientAPI | None = None):
    """取**已存在**的集合；不存在返回 ``None``（**不创建**）。

    与 ``get_collection`` 的差别是语义而不是实现细节：``get_collection`` 是
    get_or_create，**任何一次调用都会在磁盘上留下一个集合**。所以"只看不改"的动作
    （启动对账、巡检脚本）必须走这个 —— 否则会为每个从未上传过文档的账号
    凭空建出空集合（真实踩到过：对账把 `user_3_docs` 建了出来）。
    """
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if client is None:
        client = chromadb.PersistentClient(path=str(settings.storage_dir))
    if name not in {collection.name for collection in client.list_collections()}:
        return None
    return client.get_collection(name=name)


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
