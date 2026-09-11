"""文档解析测试：覆盖 Markdown / 代码 / PDF / Word / PPT。"""

from pathlib import Path

import docx
import pymupdf as fitz
import pytest
from pptx import Presentation

from src.loaders import SUPPORTED_SUFFIXES, load_document


def test_required_suffixes_supported():
    for suffix in (".md", ".txt", ".py", ".pdf", ".docx", ".pptx"):
        assert suffix in SUPPORTED_SUFFIXES


def test_markdown_is_loaded(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("# 标题\n\n这是正文内容。", encoding="utf-8")

    blocks = load_document(path)

    assert len(blocks) == 1
    assert "这是正文内容。" in blocks[0].text
    assert blocks[0].location == ""


def test_code_file_is_loaded(tmp_path: Path):
    path = tmp_path / "script.py"
    path.write_text("print('hello')", encoding="utf-8")

    blocks = load_document(path)

    assert blocks[0].text == "print('hello')"


def test_gbk_encoded_file_is_readable(tmp_path: Path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("中文内容测试".encode("gbk"))

    blocks = load_document(path)

    assert "中文内容测试" in blocks[0].text


def test_empty_file_returns_no_blocks(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n  ", encoding="utf-8")

    assert load_document(path) == []


def test_pdf_is_split_by_page(tmp_path: Path):
    path = tmp_path / "sample.pdf"
    doc = fitz.open()
    page1 = doc.new_page()
    page1.insert_text((72, 72), "Page one content")
    page2 = doc.new_page()
    page2.insert_text((72, 72), "Page two content")
    doc.save(str(path))
    doc.close()

    blocks = load_document(path)

    assert len(blocks) == 2
    assert "Page one content" in blocks[0].text
    assert blocks[0].location == "第 1 页"
    assert blocks[1].location == "第 2 页"


def test_docx_paragraphs_are_extracted(tmp_path: Path):
    path = tmp_path / "sample.docx"
    document = docx.Document()
    document.add_paragraph("第一段内容")
    document.add_paragraph("第二段内容")
    document.save(str(path))

    blocks = load_document(path)

    assert "第一段内容" in blocks[0].text
    assert "第二段内容" in blocks[0].text


def test_pptx_is_split_by_slide(tmp_path: Path):
    path = tmp_path / "sample.pptx"
    presentation = Presentation()
    layout = presentation.slide_layouts[1]

    first = presentation.slides.add_slide(layout)
    first.shapes.title.text = "第一页标题"
    second = presentation.slides.add_slide(layout)
    second.shapes.title.text = "第二页标题"
    presentation.save(str(path))

    blocks = load_document(path)

    assert len(blocks) == 2
    assert blocks[0].location == "第 1 张幻灯片"
    assert "第一页标题" in blocks[0].text
    assert "第二页标题" in blocks[1].text


def test_unsupported_suffix_returns_empty(tmp_path: Path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG")

    assert load_document(path) == []
