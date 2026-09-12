"""FastAPI 服务：提供建库、上传、问答与设置接口，并托管前端页面。"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import PROJECT_ROOT, apply_settings, settings
from src.ingest import ingest
from src.llm import reset_client
from src.loaders import SUPPORTED_SUFFIXES
from src.pipeline import answer_question

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="个人 RAG 知识库助手", version="0.1.0")


def _password_ok(request: Request) -> bool:
    """未设置访问口令时一律放行；设置后校验 HTTP Basic 的密码部分。

    用户名不校验，密码即 ``APP_PASSWORD``。这样公网部署时浏览器会弹出原生
    登录框，无需在前端另写一套登录页。
    """
    expected = settings.app_password
    if not expected:
        return True

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False

    _, _, password = decoded.partition(":")
    return secrets.compare_digest(password, expected)


@app.middleware("http")
async def require_password(request: Request, call_next):
    if _password_ok(request):
        return await call_next(request)
    return Response(
        content="需要访问口令",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="RAG Knowledge Base"'},
    )


class ChatTurn(BaseModel):
    """一轮对话历史，由前端带上，服务端不保存会话状态。"""

    role: Literal["user", "assistant"]
    content: str


class QueryRequest(BaseModel):
    question: str
    top_k: int | None = Field(default=None, ge=1, le=50)
    history: list[ChatTurn] | None = None


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(settings.data_dir)}


@app.get("/settings")
def get_settings_endpoint() -> dict:
    return _settings_state()


@app.post("/settings")
def update_settings_endpoint(payload: SettingsRequest) -> dict:
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


@app.post("/upload")
async def upload_endpoint(files: list[UploadFile] = File(...)) -> dict:
    """接收上传的文档，落盘到 data/ 目录后立即增量建库。"""
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    skipped: list[str] = []

    for upload in files:
        # 只取文件名部分，防止 ../ 之类的路径穿越
        name = Path(upload.filename or "").name
        if not name or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(name or "(未命名文件)")
            continue
        (settings.data_dir / name).write_bytes(await upload.read())
        saved.append(name)

    if not saved:
        raise HTTPException(
            status_code=400,
            detail="没有可处理的文件，请上传 PDF / Word / Markdown 等受支持的格式",
        )

    try:
        report = ingest()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"saved": saved, "skipped": skipped, **report.as_dict()}


@app.post("/ingest")
def ingest_endpoint(payload: IngestRequest | None = None) -> dict:
    try:
        report = ingest(force=bool(payload and payload.force))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report.as_dict()


@app.post("/query")
def query_endpoint(payload: QueryRequest) -> dict:
    """问答接口。

    会话状态由前端携带（``history``），服务端无状态 —— 这样重启服务
    也不会丢上下文，且天然支持多开标签页。
    """
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    history = [turn.model_dump() for turn in payload.history] if payload.history else None

    try:
        return answer_question(question, history=history, top_k=payload.top_k).as_dict()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
