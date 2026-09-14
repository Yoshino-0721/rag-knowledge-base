"""文档管理测试：上传落盘、路径穿越防护、删除钩子容错、管理员视图。

清理失败不得阻断主流程（AGENTS.md 硬规约）：安全删除钩子抛 ``SystemExit``、
向量库删除抛 ``Exception`` 时，``DELETE /api/documents/{id}`` 仍必须 200 且
数据库行确实被删掉。
"""

from __future__ import annotations

import json

import pytest

from src import server
from src.auth.models import Document
from src.config import settings
from src.ingest import IngestReport


class FakeCollection:
    """向量集合替身：只记录删除调用，不碰磁盘也不碰网络。"""

    def __init__(self, name=None):
        self.name = name
        self.deletions: list[dict] = []

    def delete(self, **kwargs):
        self.deletions.append(kwargs)


def _fake_ingest(report: IngestReport):
    """按真实 ingest 契约把清单写到 manifest_path 的桩。"""

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
def data_root(monkeypatch, tmp_path):
    """数据 / 存储目录指到 tmp_path（绝不碰真实 data/ 与 storage/）。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(server, "get_collection", FakeCollection)
    return tmp_path / "data"


def _upload(client, headers, name, content=b"content", content_type="text/markdown"):
    return client.post(
        "/upload", files={"files": (name, content, content_type)}, headers=headers
    )


# ---------------------------------------------------------------- 上传


def test_upload_writes_file_to_user_dir_and_indexes(
    client, alice, alice_headers, monkeypatch, data_root
):
    ingest_calls: list[dict] = []

    def fake_ingest(**kwargs):
        ingest_calls.append(kwargs)
        manifest_path = kwargs["manifest_path"]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"笔记.md": {"hash": "h1", "chunks": 3}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return IngestReport(added=["笔记.md"], chunks_written=3)

    monkeypatch.setattr(server, "ingest", fake_ingest)
    raw = "# 标题\n内容".encode("utf-8")

    response = _upload(client, alice_headers, "笔记.md", raw)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] == ["笔记.md"]
    assert body["skipped"] == []
    assert body["added"] == ["笔记.md"]
    assert body["chunks_written"] == 3
    assert body["total_files"] == 1

    uid = alice["user"]["id"]
    target = data_root / "users" / str(uid) / "笔记.md"
    assert target.is_file(), "上传的文件必须落到该用户自己的目录"
    assert target.read_bytes() == raw
    assert ingest_calls[0]["data_dir"] == data_root / "users" / str(uid)
    assert ingest_calls[0]["manifest_path"] == (
        data_root.parent / "storage" / "manifests" / f"user_{uid}.json"
    )

    # documents 表写入并能在列表接口里查到
    docs = client.get("/api/documents", headers=alice_headers).json()["documents"]
    assert len(docs) == 1
    assert docs[0]["filename"] == "笔记.md"
    assert docs[0]["rel_path"] == "笔记.md"
    assert docs[0]["chunks"] == 3
    assert docs[0]["file_hash"] == "h1"
    assert docs[0]["size"] == len(raw)
    assert docs[0]["user_id"] == uid


def test_upload_strips_path_traversal_from_filename(
    client, alice, alice_headers, monkeypatch, data_root
):
    monkeypatch.setattr(server, "ingest", lambda **kwargs: IngestReport())

    response = _upload(client, alice_headers, "../../evil.md", b"x")

    assert response.status_code == 200, response.text
    assert response.json()["saved"] == ["evil.md"]

    uid = alice["user"]["id"]
    written = sorted(
        path.relative_to(data_root).as_posix()
        for path in data_root.rglob("*")
        if path.is_file()
    )
    assert written == [f"users/{uid}/evil.md"], "只能落在该用户目录内"
    # 没有往上写到 data/ 或项目目录
    assert not (data_root / "evil.md").exists()
    assert not (data_root / "users" / "evil.md").exists()
    assert not (data_root.parent / "evil.md").exists()


def test_upload_skips_unsupported_and_400_when_nothing_supported(
    client, alice_headers, monkeypatch, data_root
):
    monkeypatch.setattr(
        server, "ingest", _fake_ingest(IngestReport(added=["ok.md"], chunks_written=1))
    )

    response = client.post(
        "/upload",
        files=[
            ("files", ("ok.md", b"x", "text/markdown")),
            ("files", ("virus.exe", b"MZ", "application/octet-stream")),
        ],
        headers=alice_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["saved"] == ["ok.md"]
    assert response.json()["skipped"] == ["virus.exe"]

    only_bad = _upload(
        client, alice_headers, "virus.exe", b"MZ", "application/octet-stream"
    )
    assert only_bad.status_code == 400, only_bad.text
    assert "格式" in only_bad.json()["detail"]
    # 不支持的文件根本没落盘
    assert not any(path.suffix == ".exe" for path in data_root.rglob("*"))


# ---------------------------------------------------------------- 删除容错


def test_delete_document_survives_unlink_hook(
    client, alice, alice_headers, monkeypatch, data_root
):
    """安全删除钩子在 unlink 上抛 SystemExit 时，删除文档仍要成功。"""
    from src import documents as documents_module

    monkeypatch.setattr(
        server, "ingest", _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=1))
    )
    _upload(client, alice_headers, "笔记.md")
    doc_id = client.get("/api/documents", headers=alice_headers).json()["documents"][0]["id"]
    user_dir = data_root / "users" / str(alice["user"]["id"])

    def boom(self):
        raise SystemExit("删除钩子")

    monkeypatch.setattr(documents_module.Path, "unlink", boom, raising=False)

    response = client.delete(f"/api/documents/{doc_id}", headers=alice_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "file_removed": False}

    # 文件被改名挪开（脱离索引），原路径已不存在
    assert not (user_dir / "笔记.md").exists()
    leftovers = list(user_dir.iterdir())
    assert len(leftovers) == 1 and ".deleted_" in leftovers[0].name
    assert leftovers[0].suffix not in {".md", ".markdown", ".txt"}

    # 表行确实删掉了（不是"看起来删了"）
    assert client.get("/api/documents", headers=alice_headers).json()["documents"] == []
    from src.auth.db import get_session_factory

    db = get_session_factory()()
    try:
        assert db.get(Document, doc_id) is None
    finally:
        db.close()


def test_delete_document_survives_vector_delete_failure(
    client, alice, alice_headers, monkeypatch, data_root
):
    from src import documents as documents_module

    monkeypatch.setattr(
        server, "ingest", _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=1))
    )
    _upload(client, alice_headers, "笔记.md")
    doc_id = client.get("/api/documents", headers=alice_headers).json()["documents"][0]["id"]

    class ExplodingCollection:
        def delete(self, **kwargs):
            raise Exception("向量库炸了")

    monkeypatch.setattr(
        documents_module, "get_collection", lambda name=None: ExplodingCollection()
    )

    response = client.delete(f"/api/documents/{doc_id}", headers=alice_headers)

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert client.get("/api/documents", headers=alice_headers).json()["documents"] == []
    assert not (data_root / "users" / str(alice["user"]["id"]) / "笔记.md").exists()


# ---------------------------------------------------------------- 管理员视图


def test_admin_sees_all_users_documents_and_previews_content(
    client,
    alice,
    bob,
    alice_headers,
    bob_headers,
    admin_h,
    monkeypatch,
    data_root,
):
    monkeypatch.setattr(
        server, "ingest", _fake_ingest(IngestReport(added=["alice.md"], chunks_written=2))
    )
    _upload(
        client,
        alice_headers,
        "alice.md",
        "# alice 的文档\nAlice 的正文内容".encode("utf-8"),
    )
    monkeypatch.setattr(
        server, "ingest", _fake_ingest(IngestReport(added=["bob.md"], chunks_written=5))
    )
    _upload(client, bob_headers, "bob.md", "Bob 的正文内容".encode("utf-8"))

    response = client.get("/api/admin/documents", headers=admin_h)

    assert response.status_code == 200, response.text
    docs = response.json()["documents"]
    assert {(d["filename"], d["username"]) for d in docs} == {
        ("alice.md", "alice"),
        ("bob.md", "bob"),
    }
    assert {d["user_id"] for d in docs} == {alice["user"]["id"], bob["user"]["id"]}

    alice_doc = next(d for d in docs if d["username"] == "alice")
    preview = client.get(
        f"/api/admin/documents/{alice_doc['id']}/preview", headers=admin_h
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["username"] == "alice"
    assert "Alice 的正文内容" in body["preview"]
    assert body["preview"] == "# alice 的文档\nAlice 的正文内容"
    assert body["locations"] == [""]

    # 普通用户碰不到管理员视图
    forbidden = client.get(
        f"/api/admin/documents/{alice_doc['id']}/preview", headers=bob_headers
    )
    assert forbidden.status_code == 403, forbidden.text
    assert client.get("/api/admin/documents", headers=bob_headers).status_code == 403
