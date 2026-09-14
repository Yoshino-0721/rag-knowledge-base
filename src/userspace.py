"""按用户隔离的存储路径。

每个用户有三样专属资源：
- 数据目录 ``data/users/<id>/``（上传的原始文档）
- 增量索引清单 ``storage/manifests/user_<id>.json``
- Chroma 向量集合 ``user_<id>_docs``

集合命名符合 Chroma 的 3-63 位字母数字下划线约束；不同用户的向量在
同一个 PersistentClient 下按集合隔离，互不可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserPaths:
    """一个用户的全部存储路径与集合名。"""

    data_dir: Path
    manifest_path: Path
    collection_name: str


def user_paths(settings_obj, user_id: int) -> UserPaths:
    """由调用方传入 settings（便于测试替换），生成该用户的隔离路径。"""
    return UserPaths(
        data_dir=Path(settings_obj.data_dir) / "users" / str(user_id),
        manifest_path=Path(settings_obj.storage_dir) / "manifests" / f"user_{user_id}.json",
        collection_name=f"user_{user_id}_docs",
    )
