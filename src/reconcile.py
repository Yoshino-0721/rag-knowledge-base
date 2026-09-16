"""启动期向量对账：清掉「清单里没有、向量库里还留着」的分块（幽灵文档）。

为什么要这一步
--------------
应用**自己**的删除路径是清向量的：`documents.delete_document()` 与 `ingest()` 重灌前
都会 `collection.delete(where={"source": ...})`。但只要发生过一次**绕过应用**的清库 ——
例如轮换凭证时删了 `storage/app.db`、`storage/manifests/`、`data/users/`，却漏了
`storage/chroma.sqlite3` —— 旧分块就会留在向量库里，变成"幽灵文档"：

    清单里没有、磁盘上没有，**检索却仍会命中**，于是引用来源里会出现
    用户以为已经删掉的文件（README 的效果图里那两个 `*-upload.pdf` 就是这么来的）。

对账规则（只做减法，且只碰「清单里没有」的 source）：

    集合里实际存在的 source  −  清单里的文件名  =  要清掉的幽灵

清理属于**附带动作**：任何异常都只记日志，绝不让启动失败（AGENTS.md §2.9 的口径）。
"""

from __future__ import annotations

import logging
from typing import Iterable

from src.config import settings
from src.ingest import load_manifest
from src.store import existing_collection
from src.userspace import UserPaths, user_paths

logger = logging.getLogger(__name__)


def collection_sources(collection) -> set[str]:
    """集合里出现过的全部 source（去重；空集合返回空集）。"""
    got = collection.get(include=["metadatas"]) or {}
    metadatas = got.get("metadatas") or ()
    return {str(meta["source"]) for meta in metadatas if meta and meta.get("source")}


def known_sources(manifest_path) -> set[str]:
    """清单（`storage/manifests/user_<id>.json`）里记录的文档名。

    清单不存在时返回空集 —— 那意味着"向量库里的东西一个都不该在"，
    正是上面说的轮换凭证场景。清单存在但读不出来（写坏了）时同样按空集处理：
    ``load_manifest`` 已经把 JSONDecodeError 吞成 ``{}``，而应用本来就把清单当作
    文档库的唯一事实来源（``sync_documents`` 也只认清单），所以"清单读不出来"
    与"清单里没有"对用户是同一件事。
    """
    manifest = load_manifest(manifest_path)
    if isinstance(manifest, dict):
        return set(manifest)
    logger.warning(
        "清单 %s 的结构不是对象（%s），按空清单处理。", manifest_path, type(manifest).__name__
    )
    return set()


def reconcile_user_vectors(paths: UserPaths) -> list[str]:
    """清掉该用户向量库里的幽灵 source，返回被清掉的名单（排序后）。

    只删「清单里没有」的 source；清单里有的一个都不碰。
    该用户压根没有向量集合时直接跳过 —— 用 ``existing_collection`` 而不是
    ``get_collection``：对账是"只看不改"，绝不能给新账号凭空建出空集合
    （真机验证时踩到过：对账把 `user_3_docs` 建了出来）。
    """
    collection = existing_collection(paths.collection_name)
    if collection is None:
        return []
    ghosts = sorted(collection_sources(collection) - known_sources(paths.manifest_path))
    if not ghosts:
        return []

    for source in ghosts:
        collection.delete(where={"source": source})
    logger.warning(
        "已清理幽灵向量 %d 个（清单里没有、向量库里还留着）：%s —— "
        "通常是因为清库时漏删了 storage/chroma.sqlite3（见 docs/DEPLOY.md 的运维小节）。",
        len(ghosts),
        "、".join(ghosts),
    )
    return ghosts


def reconcile_all_vectors(
    user_ids: Iterable[int], settings_obj=None
) -> dict[int, list[str]]:
    """对每个用户各做一次对账；返回 ``{用户 id: 被清掉的 source 列表}``。

    **绝不让启动失败**：单个用户出错只记日志并跳过 —— 清理是附带动作，
    删不掉几个幽灵分块，远没有"服务起不来"严重。

    只覆盖按用户隔离的集合（``user_<id>_docs``）：旧的单用户集合 ``knowledge_base``
    与 `storage/manifest.json` **不在范围内** —— 改造后没有任何请求路径会用它
    （端点一律传当前用户的集合名），去动它反而要引入第二套对账语义。
    """
    settings_obj = settings_obj or settings
    cleaned: dict[int, list[str]] = {}
    for user_id in user_ids:
        try:
            ghosts = reconcile_user_vectors(user_paths(settings_obj, user_id))
        except BaseException as exc:  # noqa: BLE001 —— SystemExit 也要兜住
            logger.warning("用户 %s 的向量对账失败（已跳过）：%s", user_id, exc)
            continue
        if ghosts:
            cleaned[user_id] = ghosts
    return cleaned


def all_user_ids() -> list[int]:
    """所有账号的 id —— 对账的对象集合（从 auth 库现读，不依赖任何内存状态）。"""
    from sqlalchemy import select

    from src.auth.db import get_session_factory
    from src.auth.models import User

    with get_session_factory()() as session:
        return list(session.scalars(select(User.id)))


__all__ = [
    "all_user_ids",
    "collection_sources",
    "known_sources",
    "reconcile_all_vectors",
    "reconcile_user_vectors",
]
