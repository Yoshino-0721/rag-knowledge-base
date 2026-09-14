"""未预期异常的对外契约（R2）。

⚠️ 必须 `TestClient(app, raise_server_exceptions=False)`：默认 True 会把异常直接抛给
测试代码，而不是让你拿到那个 500 响应。真机踩过。
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from src.server import app
from tests.conftest import auth_headers, register_and_login

SENTINEL = "SECRET-INTERNAL-URL"


def test_unhandled_exception_hides_details_but_logs_them(caplog):
    """对外只给固定文案 + request_id；异常细节只进日志。

    这条同时守住两件事：**屏蔽**（响应体里没有哨兵消息）与**保留**（日志里有）——
    只守前者会逼出"把日志也删掉"的错误修法，只守后者则等于没屏蔽。
    """
    client = TestClient(app, raise_server_exceptions=False)
    token = register_and_login(client, username="errcontract", password="Err-Contract-1!")["token"]

    def boom(*args, **kwargs):
        raise ValueError(SENTINEL)

    import src.server as server_module

    original = server_module.answer_question
    server_module.answer_question = boom
    try:
        with caplog.at_level(logging.ERROR, logger="src.server"):
            resp = client.post(
                "/query",
                json={"question": "触发未预期异常"},
                headers=auth_headers(token),
            )
    finally:
        server_module.answer_question = original

    assert resp.status_code == 500, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert "detail" in body, body
    assert "request_id" in body, body

    # 屏蔽：细节不出现在响应里（正文与响应头都不得出现）
    assert SENTINEL not in resp.text
    assert SENTINEL not in str(dict(resp.headers))
    # 保留：细节进了日志
    assert any(SENTINEL in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
