"""建库流水线：扫描 → 解析 → 分块 → 向量化 → 入库。

通过文件内容哈希实现增量索引：只有新增或内容变化的文件才会重新向量化。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from src.chunking import split_into_chunks
from src.config import settings
from src.llm import embed_texts
from src.loaders import SUPPORTED_SUFFIXES, load_document
from src.store import get_collection

EmbedFn = Callable[[Sequence[str]], list[list[float]]]


def file_hash(path: Path) -> str:
    """计算文件内容的 SHA-256，用于判断文件是否变化。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_files(data_dir: Path) -> list[Path]:
    """递归找出 data/ 下所有受支持的文件。"""
    if not data_dir.exists():
        return []
    return sorted(
        path
        for path in data_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@dataclass
class IngestReport:
    """一次建库的结果摘要。"""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    chunks_written: int = 0

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "chunks_written": self.chunks_written,
            "total_files": len(self.added) + len(self.updated) + len(self.unchanged),
        }


def ingest(
    data_dir: Path | None = None,
    collection=None,
    embed_fn: EmbedFn | None = None,
    manifest_path: Path | None = None,
    force: bool = False,
) -> IngestReport:
    """扫库并写入向量库，返回本次改动摘要。

    ``force=True`` 时忽略清单，强制重建所有文件。
    """
    data_dir = Path(data_dir or settings.data_dir)
    manifest_path = Path(manifest_path or settings.manifest_path)
    collection = collection if collection is not None else get_collection()
    embed_fn = embed_fn or embed_texts

    manifest = {} if force else _load_manifest(manifest_path)
    report = IngestReport()

    current: dict[str, Path] = {
        path.relative_to(data_dir).as_posix(): path
        for path in discover_files(data_dir)
    }

    for rel_path, path in current.items():
        digest = file_hash(path)
        previous = manifest.get(rel_path)

        if previous and previous.get("hash") == digest:
            report.unchanged.append(rel_path)
            continue

        if previous:
            collection.delete(where={"source": rel_path})
            report.updated.append(rel_path)
        else:
            report.added.append(rel_path)

        documents: list[str] = []
        metadatas: list[dict] = []
        for block in load_document(path):
            for chunk in split_into_chunks(block.text):
                documents.append(chunk)
                metadatas.append(
                    {
                        "source": rel_path,
                        "location": block.location,
                        "chunk_index": len(documents) - 1,
                        "hash": digest,
                    }
                )

        if documents:
            collection.add(
                ids=[f"{rel_path}::{i}" for i in range(len(documents))],
                documents=documents,
                embeddings=embed_fn(documents),
                metadatas=metadatas,
            )
        report.chunks_written += len(documents)
        manifest[rel_path] = {"hash": digest, "chunks": len(documents)}

    for rel_path in list(manifest):
        if rel_path not in current:
            collection.delete(where={"source": rel_path})
            del manifest[rel_path]
            report.removed.append(rel_path)

    _save_manifest(manifest_path, manifest)
    return report
