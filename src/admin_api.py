"""管理员后台 API：用户管理、全会话查看、全文档管理与系统操作。

整条路由挂 ``require_admin`` 依赖 —— 普通用户访问任何子路径都会 403。
管理员对自己也有保护：不能降级 / 禁用自己的账号，避免把全站锁死。
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession

from src.auth.deps import get_db, require_admin
from src.auth.models import ChatSession, Document, Message, User
from src.auth.security import hash_password
from src.config import settings
from src.documents import delete_document, document_payload, sync_documents
from src.ingest import ingest
from src.llm import reset_client
from src.loaders import load_document
from src.serializers import message_payload, session_payload
from src.store import get_collection
from src.userspace import user_paths

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# 管理员预览文档时的最大字符数 —— 只读模式，避免一次把超大文档整个吐出去
PREVIEW_MAX_CHARS = 4000


# ---------------------------------------------------------------- 数据模型


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=64)


class ReindexRequest(BaseModel):
    force: bool = False


# ---------------------------------------------------------------- 用户管理


def _user_row(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/users")
def list_users(q: str = "", db: OrmSession = Depends(get_db)) -> dict:
    query = select(User).order_by(User.id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(User.username.like(like), User.email.like(like)))
    users = db.scalars(query).all()
    return {"users": [_user_row(u) for u in users]}


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdate,
    admin: User = Depends(require_admin),
    db: OrmSession = Depends(get_db),
) -> dict:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    self_lockout = user_id == admin.id and (
        (payload.role is not None and payload.role != "admin")
        or (payload.is_active is False)
    )
    if self_lockout:
        raise HTTPException(status_code=400, detail="不能降级或禁用自己的管理员账号")

    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
    db.commit()
    return _user_row(target)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: PasswordReset | None = None,
    db: OrmSession = Depends(get_db),
) -> dict:
    """重置密码。不传 password 时生成 10 位随机密码，只在响应里出现一次。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if payload and payload.password:
        new_password = payload.password
    else:
        alphabet = string.ascii_letters + string.digits
        new_password = "".join(secrets.choice(alphabet) for _ in range(10))

    target.password_hash = hash_password(new_password)
    db.commit()
    return {"ok": True, "username": target.username, "password": new_password}


# ---------------------------------------------------------------- 会话管理


@router.get("/sessions")
def list_all_sessions(
    user_id: int | None = None, db: OrmSession = Depends(get_db)
) -> dict:
    query = select(ChatSession, User.username).join(User, ChatSession.user_id == User.id)
    if user_id is not None:
        query = query.where(ChatSession.user_id == user_id)
    rows = db.execute(
        query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
    ).all()

    return {
        "sessions": [
            {**session_payload(chat_session), "username": username}
            for chat_session, username in rows
        ]
    }


@router.get("/sessions/{session_id}/messages")
def read_any_session(session_id: int, db: OrmSession = Depends(get_db)) -> dict:
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    owner = db.get(User, chat_session.user_id)
    messages = (
        db.query(Message).filter_by(session_id=session_id).order_by(Message.id).all()
    )
    return {
        "session": session_payload(chat_session),
        "username": owner.username if owner else "(已删除)",
        "messages": [message_payload(m) for m in messages],
    }


@router.delete("/sessions/{session_id}")
def delete_any_session(session_id: int, db: OrmSession = Depends(get_db)) -> dict:
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete(chat_session)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 文档管理


@router.get("/documents")
def list_all_documents(
    user_id: int | None = None, db: OrmSession = Depends(get_db)
) -> dict:
    query = select(Document, User.username).join(User, Document.user_id == User.id)
    if user_id is not None:
        query = query.where(Document.user_id == user_id)
    rows = db.execute(query.order_by(Document.id)).all()
    return {
        "documents": [document_payload(doc, username=username) for doc, username in rows]
    }


@router.delete("/documents/{doc_id}")
def delete_any_document(doc_id: int, db: OrmSession = Depends(get_db)) -> dict:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    paths = user_paths(settings, doc.user_id)
    return delete_document(
        db, doc,
        data_dir=paths.data_dir,
        manifest_path=paths.manifest_path,
        collection_name=paths.collection_name,
    )


@router.get("/documents/{doc_id}/preview")
def preview_document(doc_id: int, db: OrmSession = Depends(get_db)) -> dict:
    """以管理员身份只读查看文档内容（不做检索，只做原文预览）。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    paths = user_paths(settings, doc.user_id)
    file_path = paths.data_dir / doc.rel_path
    blocks = load_document(file_path) if file_path.is_file() else []
    text = "\n\n".join(block.text for block in blocks)
    preview = text[:PREVIEW_MAX_CHARS]
    if len(text) > PREVIEW_MAX_CHARS:
        preview += f"\n\n…（已截断，全文 {len(text)} 字符）"

    owner = db.get(User, doc.user_id)
    return {
        **document_payload(doc, username=owner.username if owner else "(已删除)"),
        "preview": preview,
        "locations": [block.location for block in blocks[:20]],
    }


# ---------------------------------------------------------------- 系统操作


@router.post("/system/reindex")
def reindex_all(payload: ReindexRequest | None = None, db: OrmSession = Depends(get_db)) -> dict:
    """对全部用户分别执行（默认增量的）建库。"""
    force = bool(payload and payload.force)
    results: dict[str, dict] = {}
    for user in db.scalars(select(User).order_by(User.id)).all():
        paths = user_paths(settings, user.id)
        try:
            report = ingest(
                data_dir=paths.data_dir,
                collection=get_collection(paths.collection_name),
                manifest_path=paths.manifest_path,
                force=force,
            )
            sync_documents(db, user.id, paths.data_dir, paths.manifest_path)
            results[user.username] = report.as_dict()
        except RuntimeError as exc:
            results[user.username] = {"error": str(exc)}
    return {"ok": True, "reports": results}


@router.post("/system/clear-cache")
def clear_cache() -> dict:
    """清掉缓存的模型客户端（改配置后或排查时用）。"""
    reset_client()
    return {"ok": True}


@router.get("/system/health")
def system_health() -> dict:
    return {
        "status": "ok",
        "api_key_set": bool(settings.api_key),
        "chat_model": settings.chat_model,
        "embed_model": settings.embed_model,
        "data_dir": str(settings.data_dir),
        "storage_dir": str(settings.storage_dir),
    }


@router.get("/stats")
def stats(db: OrmSession = Depends(get_db)) -> dict:
    since = datetime.now() - timedelta(hours=24)
    return {
        "users": db.scalar(select(func.count(User.id))),
        "active_users": db.scalar(
            select(func.count(User.id)).where(User.is_active)
        ),
        "sessions": db.scalar(select(func.count(ChatSession.id))),
        "messages": db.scalar(select(func.count(Message.id))),
        "documents": db.scalar(select(func.count(Document.id))),
        "sessions_last_24h": db.scalar(
            select(func.count(ChatSession.id)).where(ChatSession.updated_at >= since)
        ),
    }
