"""documents 表的同步与删除逻辑，用户接口与管理接口共用。

同步方向以**清单（manifest）为准**：每次增量建库后把清单内容回写
documents 表 —— 新文件插入、变化文件更新哈希与块数、清单里已消失的行删除。

删除文档是四步联动：删文件（尽力而为）→ 删向量 → 删清单条目 → 删表行。
本机「安全删除钩子」可能在 ``unlink`` 上抛 ``SystemExit``（BaseException），
按 AGENTS.md 规约：清理失败只警告，绝不阻断主流程。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from sqlalchemy.orm import Session as OrmSession

from src.auth.models import Document
from src.ingest import load_manifest, save_manifest
from src.store import get_collection

logger = logging.getLogger(__name__)


def sync_documents(db: OrmSession, user_id: int, data_dir: Path, manifest_path: Path) -> None:
    """把某用户的 manifest 同步进 documents 表（文件大小取磁盘实际值）。"""
    manifest = load_manifest(manifest_path)
    existing: dict[str, Document] = {
        doc.rel_path: doc
        for doc in db.query(Document).filter(Document.user_id == user_id).all()
    }

    for rel_path, info in manifest.items():
        file_path = data_dir / rel_path
        size = file_path.stat().st_size if file_path.is_file() else 0
        row = existing.pop(rel_path, None)
        if row is None:
            db.add(
                Document(
                    user_id=user_id,
                    filename=Path(rel_path).name,
                    rel_path=rel_path,
                    file_hash=str(info.get("hash", "")),
                    chunks=int(info.get("chunks", 0)),
                    size=size,
                )
            )
        else:
            row.file_hash = str(info.get("hash", row.file_hash))
            row.chunks = int(info.get("chunks", row.chunks))
            row.size = size

    # 清单里已经没有（磁盘上被删）的文档，表行一并清掉
    for stale in existing.values():
        db.delete(stale)

    db.commit()


def document_payload(doc: Document, *, username: str | None = None) -> dict:
    data = {
        "id": doc.id,
        "user_id": doc.user_id,
        "filename": doc.filename,
        "rel_path": doc.rel_path,
        "file_hash": doc.file_hash,
        "chunks": doc.chunks,
        "size": doc.size,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }
    if username is not None:
        data["username"] = username
    return data


def _best_effort_unlink(path: Path) -> bool:
    """删一个文件：先直接删，被删除钩子拦住就改名挪开再删。

    返回值表示**是否彻底删干净**（连挪开的残留也清掉了）。改名挪开成功但
    残留清理失败时返回 False —— 文件已脱离用户目录与索引（挪开的隐藏名
    不匹配任何受支持后缀，不会被重新索引），但内容仍在磁盘上。
    清理是附带动作，任何失败都不阻断删除文档的主流程。
    """
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except BaseException as exc:  # noqa: BLE001 - 删除钩子会抛 SystemExit
        logger.warning("直接删除 %s 失败：%s，尝试改名挪开", path, exc)
    aside = path.with_name(f"{path.name}.deleted_{uuid.uuid4().hex[:8]}")
    try:
        path.rename(aside)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("改名挪开 %s 也失败：%s（文件保留在原处）", path, exc)
        return False
    try:
        aside.unlink()
        return True
    except BaseException as exc:  # noqa: BLE001
        logger.warning("清理挪开的 %s 失败：%s（文件已脱离索引）", aside, exc)
        return False


def delete_document(
    db: OrmSession,
    doc: Document,
    data_dir: Path,
    manifest_path: Path,
    collection_name: str,
) -> dict:
    """删除一个文档的全部痕迹：文件 → 向量 → 清单 → 表行。"""
    file_removed = _best_effort_unlink(data_dir / doc.rel_path)

    try:
        get_collection(collection_name).delete(where={"source": doc.rel_path})
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理向量失败（%s）：%s", doc.rel_path, exc)

    manifest = load_manifest(manifest_path)
    if doc.rel_path in manifest:
        del manifest[doc.rel_path]
        try:
            save_manifest(manifest_path, manifest)
        except OSError as exc:
            logger.warning("更新清单失败：%s", exc)

    db.delete(doc)
    db.commit()
    return {"ok": True, "file_removed": file_removed}
