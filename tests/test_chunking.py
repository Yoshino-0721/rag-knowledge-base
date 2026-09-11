"""分块逻辑测试：长度约束、内容覆盖与重叠。"""

import string

import pytest

from src.chunking import normalize_text, split_into_chunks


def test_empty_and_blank_text_return_no_chunks():
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n  \t ") == []


def test_short_text_stays_single_chunk():
    text = "这是一段很短的文本，应该只产生一个分块。"
    assert split_into_chunks(text, chunk_size=200, chunk_overlap=20) == [text]


def test_normalize_collapses_extra_blank_lines():
    assert normalize_text("a\r\n\r\n\r\n\r\nb") == "a\n\nb"


def test_every_chunk_within_size_limit():
    text = ("这是一句测试用的中文内容。" * 100)
    chunks = split_into_chunks(text, chunk_size=150, chunk_overlap=30)

    assert len(chunks) > 1
    for chunk in chunks:
        assert 0 < len(chunk) <= 150


def test_adjacent_chunks_overlap():
    text = string.ascii_letters * 6
    chunks = split_into_chunks(text, chunk_size=100, chunk_overlap=30)

    assert len(chunks) >= 3
    assert chunks[0][-30:] == chunks[1][:30]
    assert chunks[1][-30:] == chunks[2][:30]


def test_chunks_are_substrings_of_source():
    text = "段落一。\n\n段落二。\n\n段落三。" * 30
    normalized = normalize_text(text)
    chunks = split_into_chunks(text, chunk_size=120, chunk_overlap=20)

    assert chunks[0].strip() == normalized[: len(chunks[0])]
    for chunk in chunks:
        assert chunk in normalized


def test_prefers_natural_boundary():
    text = "第一段内容。" * 30 + "\n\n" + "第二段内容。" * 30
    chunks = split_into_chunks(text, chunk_size=120, chunk_overlap=10)
    assert all(chunk.strip() for chunk in chunks)


@pytest.mark.parametrize(
    "size,overlap",
    [(0, 0), (-10, 0), (100, -1), (100, 100), (100, 150)],
)
def test_invalid_parameters_raise(size, overlap):
    with pytest.raises(ValueError):
        split_into_chunks("内容" * 100, chunk_size=size, chunk_overlap=overlap)
