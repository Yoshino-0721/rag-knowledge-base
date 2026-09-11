"""文档解析：把各种格式的文件统一转成 (文本, 位置) 块。

位置信息用于回答时的引用溯源，例如 PDF 的页码、PPT 的页序。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Block:
    """一段可独立分块的文本，附带人类可读的位置标签。"""

    text: str
    location: str = ""


TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
}
PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}
PPTX_SUFFIXES = {".pptx"}

SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | DOCX_SUFFIXES | PPTX_SUFFIXES


def read_text_file(path: Path) -> str:
    """按常见编码依次尝试读取文本文件，避免中文乱码。"""
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def _load_pdf(path: Path) -> list[Block]:
    import pymupdf as fitz  # PyMuPDF

    blocks: list[Block] = []
    with fitz.open(str(path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                blocks.append(Block(text, f"第 {page_no} 页"))
    return blocks


def _load_docx(path: Path) -> list[Block]:
    import docx

    document = docx.Document(str(path))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    text = "\n\n".join(parts)
    return [Block(text)] if text else []


def _load_pptx(path: Path) -> list[Block]:
    from pptx import Presentation

    blocks: list[Block] = []
    presentation = Presentation(str(path))
    for index, slide in enumerate(presentation.slides, start=1):
        parts = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if parts:
            blocks.append(Block("\n".join(parts), f"第 {index} 张幻灯片"))
    return blocks


def load_document(path: Path) -> list[Block]:
    """按扩展名分发到对应的解析器，返回文本块列表。"""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        text = read_text_file(path).strip()
        return [Block(text)] if text else []
    if suffix in PDF_SUFFIXES:
        return _load_pdf(path)
    if suffix in DOCX_SUFFIXES:
        return _load_docx(path)
    if suffix in PPTX_SUFFIXES:
        return _load_pptx(path)
    return []
