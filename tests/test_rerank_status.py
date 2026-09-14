"""R3：精排失败必须**可见**（响应字段）、**不泄露**（只给分类词）、**有日志**。

背景：精排是锦上添花，失败就退回向量序 —— 但"悄悄降级"是危险的：运营看到的是
"系统正常"，实际检索质量已经掉了，而且分不清是网络抖动还是配置写错。
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src import retrieve
from src.generate import Answer


class _Boom(Exception):
    """没有 httpx 特征的自造异常，用来验证兜底分类。"""


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://internal.example.invalid/api/paas/v4/rerank")
    response = httpx.Response(
        code,
        request=request,
        json={"error": {"message": "SECRET-UPSTREAM-DETAIL"}},
    )
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.TimeoutException("read timed out"), "timeout"),
        # 两条路径都可能抛超时：httpx 的与内置的，都要归到 timeout
        (TimeoutError("read timed out"), "timeout"),
        (httpx.ConnectTimeout("connect timed out"), "timeout"),
        (_status_error(401), "auth"),
        (_status_error(403), "auth"),
        (_status_error(404), "model_not_found"),
        (_status_error(500), "other"),
        (_Boom("模型不存在"), "model_not_found"),
        (_Boom("invalid model name"), "model_not_found"),
        (_Boom("unauthorized"), "auth"),
        (_Boom("something weird"), "other"),
    ],
)
def test_classify_rerank_error(exc, expected):
    assert retrieve.classify_rerank_error(exc) == expected


def _six_hits() -> list[retrieve.Hit]:
    return [
        retrieve.Hit(text=f"片段{i}", source="doc.pdf", location=f"第 {i} 页", score=1.0 - i / 10)
        for i in range(6)
    ]


def _force_rerank(monkeypatch, exc: BaseException) -> None:
    def boom(question, texts, limit):
        raise exc

    monkeypatch.setattr(retrieve, "search", lambda *a, **k: _six_hits())
    monkeypatch.setattr(retrieve.settings, "rerank_enabled", True)
    monkeypatch.setattr(retrieve.settings, "rerank_candidates", 6)
    monkeypatch.setattr(retrieve, "rerank_documents", boom)


def test_failure_degrades_but_is_reported(monkeypatch):
    """失败 → 退回向量序（结果还在），同时把分类词带出来。"""
    _force_rerank(monkeypatch, httpx.TimeoutException("read timed out"))

    hits, error = retrieve.search_ranked_ex("问题", top_k=2)

    assert len(hits) == 2, "降级后仍要给出向量序的前 k 条"
    assert error == "timeout"

    payload = Answer(text="答案", rerank_error=error).as_dict()
    assert payload["rerank_failed"] is True
    assert payload["rerank_error"] == "timeout"


def test_success_reports_false_and_null(monkeypatch):
    """成功（或未启用）→ rerank_failed=false、rerank_error=null。"""

    def scored(question, texts, limit):
        return [(index, 1.0 - index / 10) for index in range(len(texts))]

    monkeypatch.setattr(retrieve, "search", lambda *a, **k: _six_hits())
    monkeypatch.setattr(retrieve.settings, "rerank_enabled", True)
    monkeypatch.setattr(retrieve.settings, "rerank_candidates", 6)
    monkeypatch.setattr(retrieve, "rerank_documents", scored)

    hits, error = retrieve.search_ranked_ex("问题", top_k=2)

    assert error is None
    assert len(hits) == 2
    payload = Answer(text="答案", rerank_error=error).as_dict()
    assert payload["rerank_failed"] is False
    assert payload["rerank_error"] is None


def test_response_never_carries_upstream_details(monkeypatch):
    """响应里只许出现分类词 —— 异常消息、URL、堆栈一律不进响应体。"""
    secret = "https://internal.example.invalid/api/paas/v4/rerank?api_key=SECRET-KEY"
    _force_rerank(monkeypatch, httpx.TimeoutException(secret))

    _hits, error = retrieve.search_ranked_ex("问题", top_k=2)
    blob = repr(Answer(text="答案", rerank_error=error).as_dict())

    assert error == "timeout"
    assert secret not in blob
    assert "SECRET-KEY" not in blob
    assert "internal.example.invalid" not in blob
    assert "Traceback" not in blob


def test_failure_is_logged_with_context(monkeypatch, caplog):
    """日志要带足够上下文（kind / 异常类型 / query_len / candidates），便于排查。"""
    _force_rerank(monkeypatch, _status_error(404))

    with caplog.at_level(logging.WARNING, logger="src.retrieve"):
        retrieve.search_ranked_ex("这是一句用来算长度的问题", top_k=2)

    messages = [record.getMessage() for record in caplog.records]
    assert any("kind=model_not_found" in message for message in messages), messages
    assert any("exc=HTTPStatusError" in message for message in messages), messages
    assert any("query_len=" in message and "candidates=6" in message for message in messages), messages
