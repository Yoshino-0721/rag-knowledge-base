"""文档解析：把各种格式的文件统一转成 (文本, 位置) 块。

位置信息用于回答时的引用溯源，例如 PDF 的页码、PPT 的页序。
"""

from __future__ import annotations

import codecs
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


def read_text_file(path: Path, max_chars: int | None = None) -> str:
    """按常见编码依次尝试读取文本文件，避免中文乱码。

    ``max_chars`` 给定时**连读入的字节数也被限住**（UTF-8 最坏 4 字节/字符）：
    预览一个几百 MB 的日志不该把整个文件先读进内存。用**增量解码器**而不是
    ``bytes.decode`` —— 它会把结尾那个被切断的多字节字符留在内部而不是报错，
    所以截断点不会误伤编码判定（否则会退化成 GBK 去解码 UTF-8，得到乱码）。
    """
    if max_chars is not None:
        raw = path.open("rb").read(max_chars * 4)
        for encoding in ("utf-8", "utf-8-sig", "gbk"):
            try:
                decoder = codecs.getincrementaldecoder(encoding)()
                return decoder.decode(raw, final=False)[:max_chars]
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")[:max_chars]

    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def _load_pdf(path: Path, max_chars: int | None = None) -> list[Block]:
    """PDF 逐页解析；给了 ``max_chars`` 就**到阈值即停**。

    PyMuPDF 是惰性取页的，所以这里的 break 是真实收益：后面的页根本不会被提取，
    而不是"提取完再丢掉"。
    """
    import pymupdf as fitz  # PyMuPDF

    blocks: list[Block] = []
    total = 0
    with fitz.open(str(path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                blocks.append(Block(text, f"第 {page_no} 页"))
                total += len(text)
            if max_chars is not None and total >= max_chars:
                break
    return blocks


def _load_docx(path: Path, max_chars: int | None = None) -> list[Block]:
    """docx 解析。

    ⚠️ 诚实说明：``python-docx`` 打开文档时就会把整个包读进来，所以这里的
    ``max_chars`` **只能限住文本累积量，限不住库自身的内存占用**。
    真正挡住超大文件的是调用方（预览接口）的体积上限。
    """
    import docx

    document = docx.Document(str(path))
    parts: list[str] = []
    total = 0
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
            total += len(text)
        if max_chars is not None and total >= max_chars:
            break
    if max_chars is None or total < max_chars:
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    joined = " | ".join(cells)
                    parts.append(joined)
                    total += len(joined)
                    if max_chars is not None and total >= max_chars:
                        break
            if max_chars is not None and total >= max_chars:
                break

    text = "\n\n".join(parts)
    return [Block(text)] if text else []


def _load_pptx(path: Path, max_chars: int | None = None) -> list[Block]:
    """pptx 逐页解析；给了 ``max_chars`` 就到阈值即停（同 docx 的库内存说明）。"""
    from pptx import Presentation

    blocks: list[Block] = []
    total = 0
    presentation = Presentation(str(path))
    for index, slide in enumerate(presentation.slides, start=1):
        parts = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if parts:
            block_text = "\n".join(parts)
            blocks.append(Block(block_text, f"第 {index} 张幻灯片"))
            total += len(block_text)
        if max_chars is not None and total >= max_chars:
            break
    return blocks


def load_document(path: Path, max_chars: int | None = None) -> list[Block]:
    """按扩展名分发到对应的解析器，返回文本块列表。

    ``max_chars`` 用于**预览**这类"只要开头"的场景：解析到阈值即停，
    而不是先整份读完再截断（后者在大文件上是一次内存尖峰）。
    不传则行为与以前完全一致。
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        text = read_text_file(path, max_chars=max_chars).strip()
        if max_chars is not None:
            text = text[:max_chars]
        return [Block(text)] if text else []
    if suffix in PDF_SUFFIXES:
        return _load_pdf(path, max_chars=max_chars)
    if suffix in DOCX_SUFFIXES:
        return _load_docx(path, max_chars=max_chars)
    if suffix in PPTX_SUFFIXES:
        return _load_pptx(path, max_chars=max_chars)
    return []
