"""文本分块：滑动窗口 + 自然断句优先，是影响召回效果的关键环节。"""

from __future__ import annotations

import re

from src.config import settings

_BOUNDARY_SEPS = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")


def normalize_text(text: str) -> str:
    """统一换行符并压缩多余空行。"""
    if not text:
        return ""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    unified = re.sub(r"\n{3,}", "\n\n", unified)
    return unified.strip()


def _find_boundary(window: str) -> int:
    """在窗口内寻找最靠后的自然断句点，返回切分位置（含分隔符）；找不到返回 -1。

    分隔符按优先级排列，优先使用更强（更靠前）的分隔符。
    """
    for sep in _BOUNDARY_SEPS:
        idx = window.rfind(sep)
        if idx != -1:
            return idx + len(sep)
    return -1


def split_into_chunks(
    text: str,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """把长文本切成若干块。

    - 每块长度不超过 ``chunk_size``；
    - 相邻块之间有 ``chunk_overlap`` 个字符的重叠，避免语义被硬切断；
    - 切分点优先落在段落、换行或句末标点上。
    """
    chunk_size = settings.chunk_size if chunk_size is None else chunk_size
    chunk_overlap = settings.chunk_overlap if chunk_overlap is None else chunk_overlap

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 不能为负数")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")

    normalized = normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    total = len(normalized)

    while start < total:
        end = min(start + chunk_size, total)
        if end < total:
            cut = _find_boundary(normalized[start:end])
            if cut >= chunk_size // 2:
                end = start + cut

        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= total:
            break
        start = max(end - chunk_overlap, start + 1)

    return chunks
