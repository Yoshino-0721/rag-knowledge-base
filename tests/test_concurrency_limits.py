"""T1 的测试：单次调用上限、上传端点同步性、查询端点并发闸门。

三条都属于"平时看不出来、出事就是全站级"的性质，靠测试钉住。
"""

from __future__ import annotations

import inspect

from src import server, upload_guard
from src.concurrency import slot_count
from tests.conftest import auth_headers, register_and_login


def test_chat_client_sets_explicit_timeout_and_retries(monkeypatch):
    """构造客户端时必须显式传 timeout / max_retries。

    SDK 默认是 read=600s + 重试 2 次，而这两条默认值在我们这里是错的：
    一次提问最坏拖半小时、成本还翻倍。显式写出来才有人能看见、能改。
    """
    from src import llm

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "OpenAI", FakeOpenAI)
    llm.get_client.cache_clear()
    try:
        llm.get_client()
    finally:
        llm.get_client.cache_clear()

    assert captured["timeout"] == llm.CHAT_TIMEOUT_SECONDS
    assert captured["max_retries"] == llm.CHAT_MAX_RETRIES
    assert 30 <= llm.CHAT_TIMEOUT_SECONDS <= 60, "超时取 30-60s（SDK 默认 600s 太长）"
    assert 0 <= llm.CHAT_MAX_RETRIES <= 1, "重试只能是 0 或 1（默认 2 会让成本翻倍）"


def test_upload_path_is_synchronous():
    """上传端点与它的读取 helper 都必须是同步的。

    上传做的是阻塞活（落盘 + 分块 + 后续 embedding）。留在 async 端点里就是
    在事件循环里干阻塞事 —— 一个用户传大文件，全站（连 /health）都得等。
    """
    assert not inspect.iscoroutinefunction(server.upload_endpoint)
    assert not inspect.iscoroutinefunction(upload_guard.read_within_limit)


def test_query_slot_rejects_when_saturated(client):
    """并发闸门占满时快速失败 429，而不是把线程池排满。"""
    slot = server._QUERY_SLOTS
    held = 0
    while slot.acquire():
        held += 1
    try:
        token = register_and_login(
            client, username="concurrency", password="Concurrency-1!"
        )["token"]
        response = client.post(
            "/query", json={"question": "并发闸门测试"}, headers=auth_headers(token)
        )
        assert response.status_code == 429, response.text
        assert "并发" in response.json()["detail"]
    finally:
        for _ in range(held):
            slot.release()


def test_slot_count_is_defensive(monkeypatch):
    """槽位数解析：非法值回退默认，且下限是 1（0 会让端点永久 429）。"""
    monkeypatch.setenv("SLOT_SIZE_PROBE", "not-a-number")
    assert slot_count("SLOT_SIZE_PROBE", 4) == 4

    monkeypatch.setenv("SLOT_SIZE_PROBE", "0")
    assert slot_count("SLOT_SIZE_PROBE", 4) == 1

    monkeypatch.setenv("SLOT_SIZE_PROBE", "-3")
    assert slot_count("SLOT_SIZE_PROBE", 4) == 1
