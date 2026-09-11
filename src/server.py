"""FastAPI 服务：提供建库、上传、问答与设置接口，并托管前端页面。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import PROJECT_ROOT, apply_settings, settings
from src.generate import generate
from src.ingest import ingest
from src.llm import reset_client
from src.loaders import SUPPORTED_SUFFIXES
from src.retrieve import search

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="个人 RAG 知识库助手", version="0.1.0")

_NO_HIT_ANSWER = "知识库中没有检索到相关内容。请先上传或放入文档并重建索引，再提问。"


class QueryRequest(BaseModel):
    question: str
    top_k: int | None = Field(default=None, ge=1, le=50)


class IngestRequest(BaseModel):
    force: bool = False


class SettingsRequest(BaseModel):
    """界面设置面板提交的内容；未填写的字段保持原值。"""

    api_key: str | None = None
    chat_model: str | None = None
    embed_model: str | None = None
    embed_dim: int | None = Field(default=None, ge=16, le=4096)
    top_k: int | None = Field(default=None, ge=1, le=50)


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
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    try:
        hits = search(question, top_k=payload.top_k)
        if not hits:
            return {"answer": _NO_HIT_ANSWER, "citations": []}
        return generate(question, hits).as_dict()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
