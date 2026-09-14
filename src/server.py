"""FastAPI 服务：提供建库、上传、问答、会话与设置接口，并托管前端页面。

多用户改造后的职责边界：
- 登录态：除 /health 与页面外全部接口需要 Bearer token（``get_current_user``）；
- 数据隔离：上传 / 建库 / 问答全部限定在当前用户的目录与向量集合内；
- 会话持久化：聊天记录存 SQLite（sessions / messages），服务端按 session_id
  读写历史，前端不再携带 history；
- 设置面板：站点级配置（API Key、模型、检索参数）只对管理员开放。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.admin_api import router as admin_router
from src.auth.api import router as auth_router
from src.auth.db import init_db
from src.auth.deps import get_current_user, get_db, require_admin
from src.auth.models import ChatSession, Document, Message, User
from src.auth.security import verify_security_config
from src.config import PROJECT_ROOT, apply_settings, settings
from src.documents import delete_document, document_payload, sync_documents
from src.ingest import ingest
from src.llm import reset_client
from src.loaders import SUPPORTED_SUFFIXES
from src.pipeline import answer_question
from src.serializers import message_payload, session_payload
from src.store import get_collection
from src.userspace import user_paths

WEB_DIR = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动自检先行：生产环境（APP_ENV != dev）没注入 JWT_SECRET 就拒绝启动，
    # 而不是等第一个用户登录才炸。见 src/auth/security.py。
    verify_security_config()
    # 建库 + 管理员引导（幂等）。测试不跑 lifespan，由 fixture 自行初始化。
    init_db()
    yield


app = FastAPI(title="个人 RAG 知识库助手", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(admin_router)


# ---------------------------------------------------------------- 数据模型


class IngestRequest(BaseModel):
    force: bool = False


class SettingsRequest(BaseModel):
    """界面设置面板提交的内容；未填写的字段保持原值。"""

    api_key: str | None = None
    chat_model: str | None = None
    embed_model: str | None = None
    embed_dim: int | None = Field(default=None, ge=16, le=4096)
    top_k: int | None = Field(default=None, ge=1, le=50)
    history_turns: int | None = Field(default=None, ge=0, le=50)
    rewrite_enabled: bool | None = None
    rerank_enabled: bool | None = None
    rerank_candidates: int | None = Field(default=None, ge=1, le=128)


class QueryRequest(BaseModel):
    """问答请求。``session_id`` 缺省时自动开一个新会话。"""

    question: str
    top_k: int | None = Field(default=None, ge=1, le=50)
    session_id: int | None = None


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- 会话工具


def _own_session(db: OrmSession, user: User, session_id: int) -> ChatSession:
    """取属于当前用户的会话；不存在或不属于本人一律 404（不泄露存在性）。"""
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return chat_session


# ---------------------------------------------------------------- 基础接口


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(settings.data_dir)}


# ---------------------------------------------------------------- 设置（仅管理员）


def _settings_state() -> dict:
    """当前配置快照，API Key 只返回脱敏提示。"""
    return {
        "api_key_set": bool(settings.api_key),
        "api_key_hint": settings.api_key_hint,
        "base_url": settings.base_url,
        "chat_model": settings.chat_model,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "top_k": settings.top_k,
        "history_turns": settings.history_turns,
        "rewrite_enabled": settings.rewrite_enabled,
        "rerank_enabled": settings.rerank_enabled,
        "rerank_model": settings.rerank_model,
        "rerank_candidates": settings.rerank_candidates,
        "data_dir": str(settings.data_dir),
    }


@app.get("/settings")
def get_settings_endpoint(_admin: User = Depends(require_admin)) -> dict:
    return _settings_state()


@app.post("/settings")
def update_settings_endpoint(
    payload: SettingsRequest, _admin: User = Depends(require_admin)
) -> dict:
    """保存设置并写回 .env；新配置立即生效，无需重启。"""
    previous_embed = (settings.embed_model, settings.embed_dim)

    apply_settings(
        api_key=payload.api_key,
        chat_model=payload.chat_model,
        embed_model=payload.embed_model,
        embed_dim=payload.embed_dim,
        top_k=payload.top_k,
        history_turns=payload.history_turns,
        rewrite_enabled=payload.rewrite_enabled,
        rerank_enabled=payload.rerank_enabled,
        rerank_candidates=payload.rerank_candidates,
    )
    reset_client()

    # 换了向量模型或维度，旧索引就失效了
    needs_reindex = (settings.embed_model, settings.embed_dim) != previous_embed
    return {**_settings_state(), "needs_reindex": needs_reindex}


# ---------------------------------------------------------------- 文档与建库


@app.post("/upload")
async def upload_endpoint(
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """接收上传的文档，落盘到该用户的目录后立即增量建库。"""
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")

    paths = user_paths(settings, user.id)
    saved: list[str] = []
    skipped: list[str] = []

    for upload in files:
        # 只取文件名部分，防止 ../ 之类的路径穿越
        name = Path(upload.filename or "").name
        if not name or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(name or "(未命名文件)")
            continue
        target = paths.data_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await upload.read())
        saved.append(name)

    if not saved:
        raise HTTPException(
            status_code=400,
            detail="没有可处理的文件，请上传 PDF / Word / Markdown 等受支持的格式",
        )

    try:
        report = ingest(
            data_dir=paths.data_dir,
            collection=get_collection(paths.collection_name),
            manifest_path=paths.manifest_path,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sync_documents(db, user.id, paths.data_dir, paths.manifest_path)
    return {"saved": saved, "skipped": skipped, **report.as_dict()}


@app.post("/ingest")
def ingest_endpoint(
    payload: IngestRequest | None = None,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """为当前用户重建（默认增量）索引。"""
    paths = user_paths(settings, user.id)
    try:
        report = ingest(
            data_dir=paths.data_dir,
            collection=get_collection(paths.collection_name),
            manifest_path=paths.manifest_path,
            force=bool(payload and payload.force),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sync_documents(db, user.id, paths.data_dir, paths.manifest_path)
    return report.as_dict()


@app.get("/api/documents")
def list_documents(
    user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> dict:
    docs = db.query(Document).filter_by(user_id=user.id).order_by(Document.id).all()
    return {"documents": [document_payload(doc) for doc in docs]}


@app.delete("/api/documents/{doc_id}")
def delete_document_endpoint(
    doc_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    doc = db.get(Document, doc_id)
    if doc is None or doc.user_id != user.id:
        raise HTTPException(status_code=404, detail="文档不存在")

    paths = user_paths(settings, user.id)
    return delete_document(
        db, doc,
        data_dir=paths.data_dir,
        manifest_path=paths.manifest_path,
        collection_name=paths.collection_name,
    )


# ---------------------------------------------------------------- 会话与问答


@app.get("/api/sessions")
def list_sessions(
    user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> dict:
    sessions = (
        db.query(ChatSession)
        .filter_by(user_id=user.id)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .all()
    )
    return {"sessions": [session_payload(s) for s in sessions]}


@app.post("/api/sessions")
def create_session(
    payload: SessionCreate | None = None,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = ChatSession(
        user_id=user.id,
        title=(payload.title.strip() if payload and payload.title else None) or "新会话",
    )
    db.add(chat_session)
    db.commit()
    db.refresh(chat_session)
    return session_payload(chat_session)


@app.get("/api/sessions/{session_id}/messages")
def list_messages(
    session_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    messages = (
        db.query(Message)
        .filter_by(session_id=chat_session.id)
        .order_by(Message.id)
        .all()
    )
    return {"session": session_payload(chat_session), "messages": [message_payload(m) for m in messages]}


@app.patch("/api/sessions/{session_id}")
def rename_session(
    session_id: int,
    payload: SessionRename,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    chat_session.title = payload.title.strip()
    db.commit()
    return session_payload(chat_session)


@app.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    db.delete(chat_session)  # messages 级联删除
    db.commit()
    return {"ok": True}


@app.post("/query")
def query_endpoint(
    payload: QueryRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """问答接口：历史从数据库读，问答结果写回数据库。

    检索与生成限定在当前用户自己的向量集合里；会话不存在或不属于本人
    一律 404。
    """
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    if payload.session_id is None:
        chat_session = ChatSession(user_id=user.id, title=question[:50])
        db.add(chat_session)
        db.commit()
        db.refresh(chat_session)
    else:
        chat_session = _own_session(db, user, payload.session_id)

    history = [
        {"role": m.role, "content": m.content}
        for m in db.query(Message)
        .filter_by(session_id=chat_session.id)
        .order_by(Message.id)
        .all()
    ]

    paths = user_paths(settings, user.id)
    collection = get_collection(paths.collection_name)

    try:
        answer = answer_question(
            question, history=history or None, top_k=payload.top_k, collection=collection
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.add(Message(session_id=chat_session.id, role="user", content=question))
    db.add(
        Message(
            session_id=chat_session.id,
            role="assistant",
            content=answer.text,
            meta=json.dumps(
                {
                    "citations": answer.citations,
                    "query": answer.query,
                    "rewritten": answer.rewritten,
                },
                ensure_ascii=False,
            ),
        )
    )
    if chat_session.title == "新会话":
        chat_session.title = question[:50]
    chat_session.updated_at = datetime.now()
    db.commit()

    return {**answer.as_dict(), "session_id": chat_session.id}


# ---------------------------------------------------------------- 页面


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(WEB_DIR / "login.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    """管理后台页面。

    这里不做服务端跳转：页面本身会去问 /api/auth/me，非管理员看到明确的
    无权限提示。真正的闸门是 /api/admin/* 上的 require_admin（一律 403）。
    """
    return FileResponse(WEB_DIR / "admin.html")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
