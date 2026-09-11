"""FastAPI 服务：提供建库、上传、问答接口，并托管前端页面。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import PROJECT_ROOT, settings
from src.generate import generate
from src.ingest import ingest
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "data_dir": str(settings.data_dir)}


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
