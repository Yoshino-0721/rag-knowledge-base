"""管理后台 API 测试：统计、用户管理、密码重置、全站会话 / 文档与系统操作。

还固化了「首次启动」这条路径：``tests/conftest.py`` 的 client 夹具是裸
``TestClient(app)``，不会跑 lifespan，所以「建库 + 引导默认管理员」必须由
本文件的 ``test_lifespan_*`` 覆盖（且不得污染真实 storage/）。
"""

from __future__ import annotations

import json

import pytest

from src import server
from src.config import PROJECT_ROOT, settings
from src.generate import Answer
from src.ingest import IngestReport
from tests.conftest import TEST_ADMIN_PASSWORD


class FakeCollection:
    def __init__(self, name=None):
        self.name = name
        self.deletions: list[dict] = []

    def delete(self, **kwargs):
        self.deletions.append(kwargs)


def _json_manifest(report: IngestReport):
    def fake_ingest(**kwargs):
        manifest_path = kwargs["manifest_path"]
        entries = {
            name: {"hash": f"stub-{name}", "chunks": report.chunks_written}
            for name in report.added
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return report

    return fake_ingest


@pytest.fixture()
def stub_pipeline(monkeypatch, tmp_path):
    """隔离目录 + 替掉向量集合与生成，问答链路不产生真实调用。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(server, "get_collection", FakeCollection)

    def fake_answer(question, history=None, top_k=None, collection=None):
        return Answer(
            text=f"回答：{question}",
            citations=[{"index": 1, "source": "a.md"}],
            query=question,
            rewritten=False,
        )

    monkeypatch.setattr(server, "answer_question", fake_answer)
    return tmp_path


def _make_query(client, headers, question, session_id=None):
    payload = {"question": question}
    if session_id is not None:
        payload["session_id"] = session_id
    response = client.post("/query", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


# ---------------------------------------------------------------- 统计


def test_stats_counts_everything(
    client, alice, bob, alice_headers, bob_headers, admin_h, stub_pipeline, monkeypatch
):
    monkeypatch.setattr(
        server, "ingest", _json_manifest(IngestReport(added=["笔记.md"], chunks_written=2))
    )
    client.post(
        "/upload",
        files={"files": ("笔记.md", b"content", "text/markdown")},
        headers=alice_headers,
    )

    session_id = client.post("/api/sessions", json={}, headers=alice_headers).json()["id"]
    _make_query(client, alice_headers, "alice 的第一问", session_id)
    _make_query(client, bob_headers, "bob 的第一问")

    response = client.get("/api/admin/stats", headers=admin_h)

    assert response.status_code == 200, response.text
    body = response.json()
    # admin 也是 users 表里的一行
    assert body["users"] == 3
    assert body["active_users"] == 3
    assert body["sessions"] == 2
    assert body["messages"] == 4
    assert body["documents"] == 1
    assert body["sessions_last_24h"] == 2


# ---------------------------------------------------------------- 用户管理


def test_search_users_by_username_and_email(client, alice, bob, admin_h):
    by_name = client.get("/api/admin/users", params={"q": "alice"}, headers=admin_h)
    assert by_name.status_code == 200, by_name.text
    assert [u["username"] for u in by_name.json()["users"]] == ["alice"]

    by_email = client.get(
        "/api/admin/users", params={"q": "bob@example.com"}, headers=admin_h
    )
    assert by_email.status_code == 200, by_email.text
    assert [u["username"] for u in by_email.json()["users"]] == ["bob"]

    # 空白 q 等同于不过滤
    all_users = client.get("/api/admin/users", params={"q": "   "}, headers=admin_h)
    assert len(all_users.json()["users"]) == 3


def test_patch_user_role_and_is_active_takes_effect(client, admin_h, bob, bob_headers):
    bob_id = bob["user"]["id"]
    assert client.get("/settings", headers=bob_headers).status_code == 403

    promoted = client.patch(
        f"/api/admin/users/{bob_id}", json={"role": "admin"}, headers=admin_h
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"
    assert client.get("/settings", headers=bob_headers).status_code == 200

    disabled = client.patch(
        f"/api/admin/users/{bob_id}", json={"is_active": False}, headers=admin_h
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["is_active"] is False
    assert client.get("/settings", headers=bob_headers).status_code == 403
    assert (
        client.post(
            "/api/auth/login", json={"account": "bob", "password": "secret123"}
        ).status_code
        == 403
    )

    # 恢复启用后能重新登录
    assert (
        client.patch(
            f"/api/admin/users/{bob_id}", json={"is_active": True}, headers=admin_h
        ).json()["is_active"]
        is True
    )
    assert (
        client.post(
            "/api/auth/login", json={"account": "bob", "password": "secret123"}
        ).status_code
        == 200
    )

    # 非法 role 被拦下
    assert (
        client.patch(
            f"/api/admin/users/{bob_id}", json={"role": "root"}, headers=admin_h
        ).status_code
        == 422
    )


def test_admin_cannot_demote_or_disable_self(client, admin_h):
    users = client.get("/api/admin/users", headers=admin_h).json()["users"]
    admin_id = next(u["id"] for u in users if u["username"] == "admin")

    demote = client.patch(
        f"/api/admin/users/{admin_id}", json={"role": "user"}, headers=admin_h
    )
    assert demote.status_code == 400, demote.text
    assert "不能降级或禁用" in demote.json()["detail"]

    disable = client.patch(
        f"/api/admin/users/{admin_id}", json={"is_active": False}, headers=admin_h
    )
    assert disable.status_code == 400, disable.text

    # 保持 admin 的改动是允许的
    same = client.patch(
        f"/api/admin/users/{admin_id}",
        json={"role": "admin", "is_active": True},
        headers=admin_h,
    )
    assert same.status_code == 200, same.text
    assert same.json()["role"] == "admin"


# ---------------------------------------------------------------- 重置密码


def test_reset_password_generates_ten_char_code(client, admin_h, alice):
    response = client.post(
        f"/api/admin/users/{alice['user']['id']}/reset-password", json={}, headers=admin_h
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["username"] == "alice"
    code = body["password"]
    assert len(code) == 10 and code.isalnum(), code

    assert (
        client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        ).status_code
        == 401
    )
    ok = client.post("/api/auth/login", json={"account": "alice", "password": code})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["username"] == "alice"


def test_reset_password_with_explicit_password(client, admin_h, alice):
    response = client.post(
        f"/api/admin/users/{alice['user']['id']}/reset-password",
        json={"password": "brandnew123"},
        headers=admin_h,
    )

    assert response.status_code == 200, response.text
    assert response.json()["password"] == "brandnew123"
    assert (
        client.post(
            "/api/auth/login", json={"account": "alice", "password": "brandnew123"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        ).status_code
        == 401
    )

    # 不存在的用户
    assert (
        client.post("/api/admin/users/9999/reset-password", json={}, headers=admin_h).status_code
        == 404
    )


# ---------------------------------------------------------------- 会话与文档


def test_admin_reads_any_users_session_messages(
    client, alice, alice_headers, admin_h, stub_pipeline
):
    session_id = _make_query(client, alice_headers, "alice 的提问")

    response = client.get(f"/api/admin/sessions/{session_id}/messages", headers=admin_h)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == "alice"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "alice 的提问"

    listed = client.get("/api/admin/sessions", headers=admin_h).json()["sessions"]
    assert [(s["id"], s["username"]) for s in listed] == [(session_id, "alice")]
    filtered = client.get(
        "/api/admin/sessions",
        params={"user_id": alice["user"]["id"]},
        headers=admin_h,
    ).json()["sessions"]
    assert [s["id"] for s in filtered] == [session_id]


# ---------------------------------------------------------------- 系统操作


def test_reindex_covers_every_user_with_own_collection(
    client, alice, bob, admin_h, monkeypatch, tmp_path
):
    from src import admin_api

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    seen: list[dict] = []
    monkeypatch.setattr(
        admin_api, "ingest", lambda **kwargs: (seen.append(kwargs), IngestReport())[1]
    )
    monkeypatch.setattr(admin_api, "get_collection", FakeCollection)

    response = client.post("/api/admin/system/reindex", json={"force": True}, headers=admin_h)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    # admin / alice / bob 三个用户都要被处理到
    assert set(body["reports"]) == {"admin", "alice", "bob"}
    for report in body["reports"].values():
        assert report["total_files"] == 0

    assert len(seen) == 3
    assert len({call["collection"].name for call in seen}) == 3, "每人一个集合"
    assert len({call["data_dir"] for call in seen}) == 3
    assert len({call["manifest_path"] for call in seen}) == 3
    assert all(call["force"] is True for call in seen)


def test_clear_cache_and_system_health(client, admin_h, monkeypatch):
    from src import admin_api

    resets: list[int] = []
    monkeypatch.setattr(admin_api, "reset_client", lambda: resets.append(1))

    cleared = client.post("/api/admin/system/clear-cache", headers=admin_h)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"ok": True}
    assert resets == [1], "清缓存要真的清掉模型客户端"

    health = client.get("/api/admin/system/health", headers=admin_h)

    assert health.status_code == 200, health.text
    body = health.json()
    assert body["status"] == "ok"
    assert set(body) == {
        "status",
        "api_key_set",
        "chat_model",
        "embed_model",
        "data_dir",
        "storage_dir",
    }


# ---------------------------------------------------------------- 启动路径


def test_lifespan_creates_db_and_bootstraps_admin(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.server import app

    real_db = PROJECT_ROOT / "storage" / "app.db"
    real_state = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns) if real_db.exists() else None
    )

    # lifespan 里 init_db() 走 settings.storage_dir/app.db，必须先指到 tmp_path
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        assert (tmp_path / "storage" / "app.db").is_file()
        r = client.post(
            "/api/auth/login",
            json={"account": "admin", "password": TEST_ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "admin"
        # 口令由 ADMIN_PASSWORD 显式提供 → 不应被"必须先改密"的闸门拦住
        assert r.json()["user"]["must_change_password"] is False
    # 幂等：再进一次不该多出管理员
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"account": "admin", "password": TEST_ADMIN_PASSWORD},
        ).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert len(client.get("/api/admin/users", headers=h).json()["users"]) == 1

    # 没有污染真实 storage/
    now_state = (
        (real_db.stat().st_size, real_db.stat().st_mtime_ns) if real_db.exists() else None
    )
    assert now_state == real_state, "lifespan 不得把库建到真实 storage/ 下"
