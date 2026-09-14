"""会话与消息持久化测试。

多用户改造后「历史由服务端按 session_id 从库里读」，前端不再携带 history。
本文件固化四件事：消息入库（含 meta）、重启不丢、标题规则与级联删除、
以及第二轮问答的 history 确实来自数据库。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src import server
from src.auth.models import Message
from src.config import settings
from src.generate import Answer


class FakeCollection:
    """向量集合替身：问答链路已被打桩，不需要真实 Chroma。"""

    def __init__(self, name=None):
        self.name = name


@pytest.fixture()
def stub_query(monkeypatch, tmp_path):
    """替掉检索 / 生成，并记录每次 answer_question 收到的 history。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(server, "get_collection", FakeCollection)

    calls: list[dict] = []

    def fake_answer(question, history=None, top_k=None, collection=None):
        calls.append({"question": question, "history": history, "top_k": top_k})
        return Answer(
            text=f"回答：{question}",
            citations=[{"index": 1, "source": "a.md", "text": "片段"}],
            query=question,
            rewritten=False,
        )

    monkeypatch.setattr(server, "answer_question", fake_answer)
    return calls


def _db_messages(session_id: int) -> list[dict]:
    """直接查库读历史 —— 断言「消息真的落库了」，而不是只看接口回显。"""
    from src.auth.db import get_session_factory

    db = get_session_factory()()
    try:
        rows = (
            db.query(Message)
            .filter_by(session_id=session_id)
            .order_by(Message.id)
            .all()
        )
        return [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "meta": json.loads(row.meta) if row.meta else None,
            }
            for row in rows
        ]
    finally:
        db.close()


# ---------------------------------------------------------------- 落库


def test_query_persists_user_and_assistant_messages_with_meta(client, alice_headers, stub_query):
    response = client.post(
        "/query", json={"question": "什么是 RAG？"}, headers=alice_headers
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]

    rows = _db_messages(session_id)
    assert [row["role"] for row in rows] == ["user", "assistant"], rows
    assert rows[0]["content"] == "什么是 RAG？"
    assert rows[0]["meta"] is None
    assert rows[1]["content"] == "回答：什么是 RAG？"
    assert rows[1]["meta"] == {
        "citations": [{"index": 1, "source": "a.md", "text": "片段"}],
        "query": "什么是 RAG？",
        "rewritten": False,
    }

    # 接口返回的 session_id 与库里那条会话对得上
    body = client.get(f"/api/sessions/{session_id}/messages", headers=alice_headers)
    assert body.status_code == 200, body.text
    assert body.json()["session"]["id"] == session_id
    assert [m["role"] for m in body.json()["messages"]] == ["user", "assistant"]
    assert body.json()["messages"][1]["meta"] == rows[1]["meta"]


def test_messages_survive_new_client_same_db(client, alice_headers, stub_query):
    """换一个 TestClient 后历史仍在且顺序一致（历史存库，不存进程内存）。"""
    session_id = client.post(
        "/query", json={"question": "重启前的提问"}, headers=alice_headers
    ).json()["session_id"]
    before = client.get(f"/api/sessions/{session_id}/messages", headers=alice_headers)
    assert before.status_code == 200, before.text

    from src.server import app

    # 不进 with：不触发 lifespan，保证用的还是同一套 AUTH_DB（autouse 夹具建的）
    fresh = TestClient(app)
    after = fresh.get(f"/api/sessions/{session_id}/messages", headers=alice_headers)

    assert after.status_code == 200, after.text
    assert [m["id"] for m in after.json()["messages"]] == [
        m["id"] for m in before.json()["messages"]
    ]
    assert [m["content"] for m in after.json()["messages"]] == [
        "重启前的提问",
        "回答：重启前的提问",
    ]


def test_history_comes_from_database(client, alice_headers, stub_query):
    """核心断言：第二轮拿到的是库里第一轮的一问一答，前端没带任何历史。"""
    first = client.post("/query", json={"question": "第一问"}, headers=alice_headers)
    assert first.status_code == 200, first.text
    session_id = first.json()["session_id"]
    assert stub_query[0]["history"] is None, "首轮不该有历史"

    second = client.post(
        "/query", json={"question": "第二问", "session_id": session_id}, headers=alice_headers
    )

    assert second.status_code == 200, second.text
    assert stub_query[1]["history"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答：第一问"},
    ], "第二轮 history 必须来自数据库"
    assert len(_db_messages(session_id)) == 4


# ---------------------------------------------------------------- 标题


def test_title_defaults_to_first_50_chars(client, alice_headers, stub_query):
    question = "问" * 120

    response = client.post("/query", json={"question": question}, headers=alice_headers)

    assert response.status_code == 200, response.text
    sessions = client.get("/api/sessions", headers=alice_headers).json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["title"] == "问" * 50
    assert len(sessions[0]["title"]) == 50


def test_query_replaces_placeholder_title(client, alice_headers, stub_query):
    created = client.post("/api/sessions", json={}, headers=alice_headers)
    assert created.status_code == 200, created.text
    assert created.json()["title"] == "新会话"

    question = "首问" * 40
    body = client.post(
        "/query",
        json={"question": question, "session_id": created.json()["id"]},
        headers=alice_headers,
    ).json()

    sessions = client.get("/api/sessions", headers=alice_headers).json()["sessions"]
    assert [s["id"] for s in sessions] == [body["session_id"]]
    assert sessions[0]["title"] == question[:50]


def test_patch_renames_session(client, alice_headers, stub_query):
    session_id = client.post("/api/sessions", json={}, headers=alice_headers).json()["id"]

    response = client.patch(
        f"/api/sessions/{session_id}", json={"title": "重命名后的标题"}, headers=alice_headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "重命名后的标题"
    listed = client.get("/api/sessions", headers=alice_headers).json()["sessions"]
    assert listed[0]["title"] == "重命名后的标题"
    # 空标题不合法
    assert (
        client.patch(
            f"/api/sessions/{session_id}", json={"title": ""}, headers=alice_headers
        ).status_code
        == 422
    )


# ---------------------------------------------------------------- 删除


def test_delete_session_removes_its_messages(client, alice_headers, stub_query):
    session_id = client.post(
        "/query", json={"question": "会被删掉的会话"}, headers=alice_headers
    ).json()["session_id"]
    assert len(_db_messages(session_id)) == 2

    response = client.delete(f"/api/sessions/{session_id}", headers=alice_headers)

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert _db_messages(session_id) == [], "messages 应随会话级联删除"
    assert client.get(f"/api/sessions/{session_id}/messages", headers=alice_headers).status_code == 404
    assert client.get("/api/sessions", headers=alice_headers).json()["sessions"] == []


# ---------------------------------------------------------------- 自动建会话


def test_query_without_session_id_creates_one(client, alice_headers, stub_query):
    response = client.post(
        "/query", json={"question": "自动开新会话"}, headers=alice_headers
    )

    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    sessions = client.get("/api/sessions", headers=alice_headers).json()["sessions"]
    assert [s["id"] for s in sessions] == [session_id]
    assert sessions[0]["title"] == "自动开新会话"

    body = client.get(f"/api/sessions/{session_id}/messages", headers=alice_headers).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    # 带 session_id 的第二轮不会再建会话
    client.post(
        "/query", json={"question": "追问", "session_id": session_id}, headers=alice_headers
    )
    assert len(client.get("/api/sessions", headers=alice_headers).json()["sessions"]) == 1
    assert len(_db_messages(session_id)) == 4
