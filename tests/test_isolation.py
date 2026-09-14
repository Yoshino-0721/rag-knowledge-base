"""多用户隔离与鉴权边界测试。

三条不可退让的规则：
1. 越权访问他人资源一律 **404**（不泄露资源是否存在，避免探测）；
2. 未登录 / 坏 token **401**；
3. 普通用户访问管理接口与站点设置 **403**。

另外固化「向量集合按用户隔离」这一架构决策：每个用户的 Chroma 集合、
数据目录与清单文件都必须互不相同。
"""

from __future__ import annotations

import json

import pytest

from src import server
from src.config import settings
from src.ingest import IngestReport
from src.userspace import user_paths


class FakeCollection:
    """向量集合替身：只记录调用，不碰磁盘也不碰网络。"""

    def __init__(self, name=None):
        self.name = name
        self.deletions: list[dict] = []

    def delete(self, **kwargs):
        self.deletions.append(kwargs)


def _fake_ingest(*names):
    """把清单写到 manifest_path 的 ingest 桩（清单是 documents 表同步的唯一输入）。"""

    def fake_ingest(**kwargs):
        entries = {name: {"hash": f"stub-{name}", "chunks": 1} for name in names}
        manifest_path = kwargs["manifest_path"]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return IngestReport(added=list(names), chunks_written=len(names))

    return fake_ingest


@pytest.fixture()
def tmp_storage(monkeypatch, tmp_path):
    """数据 / 存储目录一律落到 tmp_path，并替掉向量集合。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(server, "get_collection", FakeCollection)
    return tmp_path


@pytest.fixture()
def alice_data(client, alice, alice_headers, monkeypatch, tmp_storage):
    """alice 拥有一个会话与一个文档，供越权用例当靶子。"""
    monkeypatch.setattr(server, "ingest", _fake_ingest("alice.md"))

    created = client.post(
        "/api/sessions", json={"title": "alice 的会话"}, headers=alice_headers
    )
    assert created.status_code == 200, created.text

    uploaded = client.post(
        "/upload",
        files={"files": ("alice.md", "alice 的机密内容".encode("utf-8"), "text/markdown")},
        headers=alice_headers,
    )
    assert uploaded.status_code == 200, uploaded.text

    documents = client.get("/api/documents", headers=alice_headers).json()["documents"]
    assert len(documents) == 1, documents
    return {"session_id": created.json()["id"], "doc_id": documents[0]["id"]}


# ---------------------------------------------------------------- 越权：会话


def test_bob_cannot_read_alice_session_messages(client, alice_headers, bob_headers, alice_data):
    sid = alice_data["session_id"]

    response = client.get(f"/api/sessions/{sid}/messages", headers=bob_headers)

    assert response.status_code == 404, response.text
    assert "不存在" in response.json()["detail"]
    # 对照组：alice 自己读得到
    assert client.get(f"/api/sessions/{sid}/messages", headers=alice_headers).status_code == 200


def test_bob_cannot_rename_or_delete_alice_session(client, alice_headers, bob_headers, alice_data):
    sid = alice_data["session_id"]

    renamed = client.patch(
        f"/api/sessions/{sid}", json={"title": "bob 改的标题"}, headers=bob_headers
    )
    assert renamed.status_code == 404, renamed.text

    deleted = client.delete(f"/api/sessions/{sid}", headers=bob_headers)
    assert deleted.status_code == 404, deleted.text

    # alice 的会话既没被改名也没被删掉
    body = client.get(f"/api/sessions/{sid}/messages", headers=alice_headers).json()
    assert body["session"]["title"] == "alice 的会话"
    assert client.get("/api/sessions", headers=alice_headers).json()["sessions"] != []


def test_bob_cannot_query_into_alice_session(client, alice_headers, bob_headers, alice_data):
    sid = alice_data["session_id"]

    response = client.post(
        "/query", json={"question": "偷偷问一句", "session_id": sid}, headers=bob_headers
    )

    assert response.status_code == 404, response.text
    # alice 的会话里没有多出任何消息
    assert client.get(f"/api/sessions/{sid}/messages", headers=alice_headers).json()["messages"] == []


def test_bob_cannot_delete_alice_document(client, alice_headers, bob_headers, alice_data, tmp_storage):
    doc_id = alice_data["doc_id"]

    response = client.delete(f"/api/documents/{doc_id}", headers=bob_headers)

    assert response.status_code == 404, response.text
    docs = client.get("/api/documents", headers=alice_headers).json()["documents"]
    assert [d["id"] for d in docs] == [doc_id]
    assert any(p.name == "alice.md" for p in (tmp_storage / "data").rglob("*"))


def test_bob_lists_leak_nothing_about_alice(client, bob_headers, alice_data):
    sessions = client.get("/api/sessions", headers=bob_headers)
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["sessions"] == []
    assert "alice 的会话" not in sessions.text

    documents = client.get("/api/documents", headers=bob_headers)
    assert documents.status_code == 200, documents.text
    assert documents.json()["documents"] == []
    assert "alice.md" not in documents.text


# ---------------------------------------------------------------- 越权：管理接口

ADMIN_CALLS = [
    ("get", "/settings", {}),
    ("post", "/settings", {"json": {"top_k": 3}}),
    ("get", "/api/admin/users", {}),
    ("get", "/api/admin/sessions", {}),
    ("get", "/api/admin/documents", {}),
    ("get", "/api/admin/stats", {}),
    ("get", "/api/admin/system/health", {}),
    ("post", "/api/admin/system/reindex", {"json": {}}),
    ("post", "/api/admin/system/clear-cache", {}),
    ("patch", "/api/admin/users/1", {"json": {"role": "user"}}),
    ("post", "/api/admin/users/1/reset-password", {"json": {}}),
]


@pytest.mark.parametrize(
    "method,path,kwargs",
    ADMIN_CALLS,
    ids=[f"{method.upper()} {path}" for method, path, _ in ADMIN_CALLS],
)
def test_normal_user_gets_403_on_admin_surface(client, alice_headers, method, path, kwargs):
    response = getattr(client, method)(path, headers=alice_headers, **kwargs)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "需要管理员权限"


# ---------------------------------------------------------------- 未登录 / 坏 token

PROTECTED_CALLS = [
    ("get", "/settings", {}),
    ("post", "/settings", {"json": {"top_k": 3}}),
    ("post", "/query", {"json": {"question": "什么是 RAG？"}}),
    ("post", "/upload", {"files": {"files": ("a.md", b"x", "text/markdown")}}),
    ("post", "/ingest", {"json": {}}),
    ("get", "/api/sessions", {}),
    ("post", "/api/sessions", {"json": {}}),
    ("get", "/api/documents", {}),
]
PROTECTED_IDS = [f"{method.upper()} {path}" for method, path, _ in PROTECTED_CALLS]


@pytest.mark.parametrize("method,path,kwargs", PROTECTED_CALLS, ids=PROTECTED_IDS)
def test_missing_token_is_401(client, method, path, kwargs):
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "未登录，请先登录"


@pytest.mark.parametrize("method,path,kwargs", PROTECTED_CALLS, ids=PROTECTED_IDS)
def test_garbage_token_is_401(client, method, path, kwargs):
    response = getattr(client, method)(
        path, headers={"Authorization": "Bearer not.a.valid.token"}, **kwargs
    )

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "登录状态已失效，请重新登录"


# ---------------------------------------------------------------- 向量集合隔离


def test_user_paths_are_disjoint_per_user(alice, bob, tmp_storage):
    alice_paths = user_paths(settings, alice["user"]["id"])
    bob_paths = user_paths(settings, bob["user"]["id"])

    assert alice_paths.collection_name != bob_paths.collection_name
    assert alice_paths.data_dir != bob_paths.data_dir
    assert alice_paths.manifest_path != bob_paths.manifest_path
    assert alice_paths.collection_name == f"user_{alice['user']['id']}_docs"
    assert bob_paths.collection_name == f"user_{bob['user']['id']}_docs"


def test_ingest_uses_per_user_collection_and_paths(
    client, alice, alice_headers, bob, bob_headers, monkeypatch, tmp_storage
):
    """两条 /ingest 必须落到各自的集合、目录与清单，绝不共用一个。"""
    captured: list[dict] = []
    monkeypatch.setattr(
        server, "ingest", lambda **kwargs: (captured.append(kwargs), IngestReport())[1]
    )

    assert client.post("/ingest", json={}, headers=alice_headers).status_code == 200
    assert client.post("/ingest", json={}, headers=bob_headers).status_code == 200

    alice_call, bob_call = captured
    alice_paths = user_paths(settings, alice["user"]["id"])
    bob_paths = user_paths(settings, bob["user"]["id"])

    assert alice_call["collection"].name == alice_paths.collection_name
    assert bob_call["collection"].name == bob_paths.collection_name
    assert alice_call["collection"].name != bob_call["collection"].name
    assert alice_call["data_dir"] == alice_paths.data_dir
    assert bob_call["data_dir"] == bob_paths.data_dir
    assert alice_call["data_dir"] != bob_call["data_dir"]
    assert alice_call["manifest_path"] != bob_call["manifest_path"]
