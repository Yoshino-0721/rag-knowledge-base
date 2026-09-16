"""FastAPI 服务：提供建库、上传、问答、会话与设置接口，并托管前端页面。

多用户改造后的职责边界：
- 登录态：除 /health 与页面外全部接口需要 Bearer token（``get_current_user``）；
- 数据隔离：上传 / 建库 / 问答全部限定在当前用户的目录与向量集合内；
- 会话持久化：聊天记录存 SQLite（sessions / messages），服务端按 session_id
  读写历史，前端不再携带 history；
- 设置面板：站点级配置（API Key、模型、检索参数）只对管理员开放。
"""

from __future__ import annotations

import contextvars
import logging

import os

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src import upload_guard
from src.admin_api import router as admin_router
from src.auth.api import router as auth_router
from src.auth.db import init_db
from src.auth.deps import get_current_user, get_db, require_admin
from src.auth.models import ChatSession, Document, Message, User
from src.auth.security import verify_security_config
from src.config import PROJECT_ROOT, apply_settings, settings
from src import request_id

logger = logging.getLogger(__name__)

_current_rid = contextvars.ContextVar("request_id_mw", default="-")
from src.documents import delete_document, document_payload, sync_documents
from src.ingest import ingest
from src.llm import reset_client
from src.loaders import SUPPORTED_SUFFIXES
from src.paths import safe_target_name
from src.pipeline import answer_question
from src.reconcile import all_user_ids, reconcile_all_vectors
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
    # 向量对账：清掉「清单里没有、向量库里还留着」的幽灵分块（见 src/reconcile.py）。
    # 放在 init_db 之后（要先有账号才知道该对账谁），并且自己兜异常 —— 清理是附带动作，
    # 不许把启动带崩；测试不跑 lifespan，由 tests/test_reconcile.py 直接调函数。
    reconcile_all_vectors(all_user_ids())
    yield


app = FastAPI(title="团队 RAG 知识库", version="0.1.0", lifespan=lifespan)
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


request_id.install()


@app.middleware("http")
async def _attach_request_id(request: Request, call_next):
    """给每个请求分配 id：优先采信合法的入站头，否则新生成。

    入站头要严格校验（见 ``src/request_id.py`` 的 ``_ALLOWED``）：那是不可信输入，
    直接回显会带来日志注入。id 同时写进 ``scope["state"]``（异常处理器从那里取）
    与 ContextVar（业务日志用），最后回显到响应头。
    """
    rid = request_id.sanitize(request.headers.get(request_id.REQUEST_ID_HEADER)) or request_id.new_id()
    request_id.bind(request.scope, rid)
    token = _current_rid.set(rid)
    try:
        response = await call_next(request)
    finally:
        _current_rid.reset(token)
    response.headers[request_id.REQUEST_ID_HEADER] = rid
    return response


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常一律返回结构化 JSON。

    在此之前 Starlette 会吐 21 字节纯文本 ``Internal Server Error``：前端
    ``resp.json()`` 直接抛 SyntaxError，只能显示"请求失败（HTTP 500）"，
    看着像前端 bug，实际是后端返的不是 JSON。

    **对外只给固定文案 + request_id**：异常消息、URL、堆栈一律只进日志 ——
    用户拿到 id，运维 grep 那个 id 就能捞到完整堆栈，两边都不吃亏。
    """
    rid = request_id.of(request)
    logger.exception("未捕获异常（request_id=%s）：%s", rid, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试", "request_id": rid},
        # 显式加：异常路径上中间件根本没机会给响应加头（探针结论 3）
        headers={request_id.REQUEST_ID_HEADER: rid},
    )


@app.get("/health")
def health() -> dict:
    """公开的健康检查：**只回答"活着吗"**，不吐任何服务器内部信息。

    原先这里连 `data_dir`（服务器绝对路径）一起返回。公开接口不该给匿名调用者
    提供路径情报 —— 它配合任意文件读取类缺陷就是一张攻击地图。
    站点自身的目录信息仍在需要管理员的 `/api/admin/system/health` 里。
    """
    return {"status": "ok"}


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
def upload_endpoint(
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """接收上传的文档，落盘到该用户的目录后立即增量建库。

    体积上限走**两道闸门**（见 ``src/upload_guard.py``）：先按 Content-Length
    快速拒绝，再在读取时按累计字节兜底 —— 只信请求头会被伪造头或
    ``Transfer-Encoding: chunked`` 绕过。

    本端点是**同步阻塞**的：FastAPI 把同步 ``def`` 放进默认线程池（约 40 线程）执行，
    所以它与其他同步端点**共享同一批线程**。上传要落盘、分块、并触发 embedding，
    全是阻塞活，占线程是必然的 —— 除了线程池上限，还有接口级并发闸门
    （见 ``src/concurrency.py``）兜住。代价说清楚：高并发上传时其他同步端点
    （含 ``/health``）会一起变慢。这是刻意取舍：宁可慢，也不把阻塞活塞进事件循环。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")

    upload_guard.enforce_content_length(request)
    # 整次请求共享一份预算：多个文件各自 49 MiB 也仍然写不爆磁盘
    budget = upload_guard.limit_bytes()

    paths = user_paths(settings, user.id)
    saved: list[str] = []
    skipped: list[str] = []

    for upload in files:
        # 只取文件名部分，防止 ../ 之类的路径穿越
        # 文件名归一化复用 src/paths.py 的唯一实现：这里原先手写了一份
        # ``Path(...).name``，与 safe_target_name 的规则迟早分叉 ——
        # 而这类分叉的后果是"一边拦得住、一边拦不住"。
        raw_name = Path(upload.filename or "")
        name = safe_target_name(raw_name)
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(raw_name.name or "(未命名文件)")
            continue
        content = upload_guard.read_within_limit(upload, budget)
        budget -= len(content)
        target = paths.data_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子落盘：先写同目录临时文件，再 os.replace 顶替。直接 write_bytes 覆盖有
        # 两个问题 —— 中途失败留半个文件把好文件顶掉；并发重名上传互相写花。
        # os.replace 在同一文件系统内是原子的，而且**完全不涉及删除**
        # （本机删除会被钩子重定向，批量时还可能抛 SystemExit，见 AGENTS.md §2.9）。
        tmp = target.with_name(target.name + ".incoming")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, target)
        finally:
            # 清理是附带行为：失败不得影响主流程
            if tmp.exists():
                try:
                    tmp.unlink()
                except BaseException:  # noqa: BLE001
                    pass
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


# 并发闸门：查询端点是同步 def，占的是 anyio 线程池（默认 40 槽）。没有闸门时
# 几十个并发提问就能把线程池占满，连 /health 都要排队 —— 而每次提问还会真花钱
# 调模型。满员直接 429 快速失败，不排队（排队只把所有人一起拖慢）。
from src.concurrency import AsyncSlot, limit_concurrency, slot_count

_QUERY_SLOTS = AsyncSlot("query", slot_count("MAX_CONCURRENT_QUERIES", 4))


@app.post("/query")
@limit_concurrency(_QUERY_SLOTS, "并发提问已满，请稍后重试")
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
        # **先不建会话**：跑成功了才落库，失败请求不在库里留任何痕迹。
        # 新会话本来就没有历史，所以延后创建不影响上下文。
        chat_session = None
        history: list[dict] = []
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

    if chat_session is None:
        chat_session = ChatSession(user_id=user.id, title=question[:50])
        db.add(chat_session)
        # flush 只为拿到自增 id，**不提交事务**：上面任何失败都会让整个事务回滚，
        # 不会留下一条没有任何消息的会话行。
        db.flush()
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
