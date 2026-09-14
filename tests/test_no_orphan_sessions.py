"""失败请求不得在库里留下孤儿会话（C2 复核清单 2.1）。

原先的顺序是"先建会话并 commit，再跑问答"，于是任何一次必然失败的提问
（缺 API Key、向量库异常……）都会在库里留下一条**没有任何消息**的会话行。
顺序调整后：只有问答真的产出了答案，才把会话与消息一起落库。
"""

from __future__ import annotations

from src import server
from src.generate import Answer


def _boom(*args, **kwargs):
    raise RuntimeError("缺少 API Key")


def test_failed_query_leaves_no_session(client, alice_headers, monkeypatch):
    monkeypatch.setattr(server, "answer_question", _boom)

    response = client.post("/query", json={"question": "会失败的问题"}, headers=alice_headers)

    assert response.status_code == 400, response.text
    assert client.get("/api/sessions", headers=alice_headers).json()["sessions"] == []


def test_repeated_failures_leave_nothing(client, alice_headers, monkeypatch):
    monkeypatch.setattr(server, "answer_question", _boom)

    for _ in range(3):
        client.post("/query", json={"question": "又失败一次"}, headers=alice_headers)

    assert client.get("/api/sessions", headers=alice_headers).json()["sessions"] == []


def test_success_still_creates_the_session(client, alice_headers, monkeypatch):
    """反向对照：成功路径必须照常落库 —— 否则"修好孤儿"等于把功能删了。"""
    monkeypatch.setattr(
        server,
        "answer_question",
        lambda *a, **k: Answer(text="答案正文", citations=[], query="q"),
    )

    response = client.post("/query", json={"question": "会成功的问题"}, headers=alice_headers)

    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    messages = client.get(
        f"/api/sessions/{sid}/messages", headers=alice_headers
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_failure_does_not_touch_an_existing_session(client, alice_headers, monkeypatch):
    """已有会话时失败：既不该新建会话，也不该往老会话里塞半截记录。"""
    monkeypatch.setattr(
        server,
        "answer_question",
        lambda *a, **k: Answer(text="第一次的答案", citations=[], query="q"),
    )
    sid = client.post(
        "/query", json={"question": "先成功一次"}, headers=alice_headers
    ).json()["session_id"]

    monkeypatch.setattr(server, "answer_question", _boom)
    failed = client.post(
        "/query", json={"question": "这次失败", "session_id": sid}, headers=alice_headers
    )
    assert failed.status_code == 400, failed.text

    messages = client.get(
        f"/api/sessions/{sid}/messages", headers=alice_headers
    ).json()["messages"]
    assert [m["content"] for m in messages] == ["先成功一次", "第一次的答案"]
    assert len(client.get("/api/sessions", headers=alice_headers).json()["sessions"]) == 1
