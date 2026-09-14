"""API 接口测试（检索、生成与建库均被替换，不产生真实调用）。

多用户改造后所有业务接口都需要登录态：``alice_headers`` 是每个用例
现注册现登录的普通用户；``admin_h`` 是引导创建的默认管理员。
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from src import config, server
from src.config import settings
from src.generate import Answer
from src.ingest import IngestReport
from src.pipeline import NO_HIT_ANSWER

client = TestClient(server.app)


def _register(username: str, email: str | None = None) -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": "secret123",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def alice():
    return _register("alice")


@pytest.fixture()
def alice_headers(alice) -> dict:
    return _headers(alice["token"])


@pytest.fixture()
def admin_h() -> dict:
    response = client.post(
        "/api/auth/login", json={"account": "admin", "password": "admin123"}
    )
    assert response.status_code == 200
    return _headers(response.json()["token"])


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_serves_chat_page():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------- 鉴权门槛


class TestAuthGates:
    def test_query_without_token_is_401(self):
        response = client.post("/query", json={"question": "问题"})
        assert response.status_code == 401

    def test_upload_without_token_is_401(self):
        response = client.post("/upload")
        assert response.status_code == 401

    def test_ingest_without_token_is_401(self):
        response = client.post("/ingest", json={})
        assert response.status_code == 401

    def test_settings_without_token_is_401(self):
        response = client.get("/settings")
        assert response.status_code == 401

    def test_documents_without_token_is_401(self):
        response = client.get("/api/documents")
        assert response.status_code == 401

    def test_sessions_without_token_is_401(self):
        response = client.get("/api/sessions")
        assert response.status_code == 401

    def test_normal_user_cannot_read_settings(self, alice_headers):
        response = client.get("/settings", headers=alice_headers)
        assert response.status_code == 403

    def test_normal_user_cannot_change_settings(self, alice_headers):
        response = client.post("/settings", json={"top_k": 9}, headers=alice_headers)
        assert response.status_code == 403

    def test_admin_can_read_settings(self, admin_h):
        response = client.get("/settings", headers=admin_h)
        assert response.status_code == 200


# ---------------------------------------------------------------- 问答


def test_query_rejects_blank_question(alice_headers):
    response = client.post(
        "/query", json={"question": "   "}, headers=alice_headers
    )

    assert response.status_code == 400


def test_query_delegates_to_pipeline(monkeypatch, alice_headers):
    """接口层只做参数校验与转发，编排逻辑在 pipeline 里（另有独立用例覆盖）。"""
    captured = {}

    def fake_answer(question, history=None, top_k=None, **kwargs):
        captured["collection"] = kwargs.get("collection")
        return Answer(
            "这是回答 [1]。",
            [{"index": 1, "source": "a.md", "location": "", "score": 0.91, "text": "原文片段"}],
            query="问题",
            rewritten=False,
        )

    monkeypatch.setattr(server, "answer_question", fake_answer)

    response = client.post(
        "/query", json={"question": "问题"}, headers=alice_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "这是回答 [1]。"
    assert body["citations"][0]["source"] == "a.md"
    assert body["query"] == "问题"
    assert body["rewritten"] is False
    assert body["session_id"] >= 1
    assert captured["collection"] is not None, "问答应使用当前用户的向量集合"


def test_query_without_hits_returns_guidance(monkeypatch, alice_headers):
    monkeypatch.setattr(
        server, "answer_question", lambda *args, **kwargs: Answer(NO_HIT_ANSWER)
    )

    response = client.post(
        "/query", json={"question": "没有答案的问题"}, headers=alice_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["citations"] == []
    assert "没有检索到" in body["answer"]


def test_query_history_comes_from_database(monkeypatch, alice_headers):
    """历史不再由前端携带：同一会话第二次提问时服务端从库里取上一轮。"""
    seen_histories = []

    def fake_answer(question, history=None, top_k=None, **kwargs):
        seen_histories.append(history)
        return Answer("回答")

    monkeypatch.setattr(server, "answer_question", fake_answer)

    first = client.post(
        "/query", json={"question": "第一问"}, headers=alice_headers
    ).json()
    client.post(
        "/query",
        json={"question": "第二问", "session_id": first["session_id"]},
        headers=alice_headers,
    )

    assert seen_histories[0] is None, "新会话没有历史"
    assert seen_histories[1] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答"},
    ], "第二轮应从数据库读到第一轮的问答"


def test_query_persists_messages_with_citation_meta(monkeypatch, alice_headers):
    monkeypatch.setattr(
        server,
        "answer_question",
        lambda *args, **kwargs: Answer(
            "答案 [1]",
            [{"index": 1, "source": "a.md", "location": "第 1 页", "score": 0.9, "text": "片段"}],
            query="q",
            rewritten=True,
        ),
    )

    body = client.post(
        "/query", json={"question": "问题"}, headers=alice_headers
    ).json()

    messages = client.get(
        f"/api/sessions/{body['session_id']}/messages", headers=alice_headers
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "问题"
    assert messages[1]["content"] == "答案 [1]"
    assert messages[1]["meta"]["rewritten"] is True
    assert messages[1]["meta"]["citations"][0]["source"] == "a.md"


def test_query_surfaces_runtime_error_as_400(monkeypatch, alice_headers):
    def boom(*args, **kwargs):
        raise RuntimeError("未配置 API Key")

    monkeypatch.setattr(server, "answer_question", boom)

    response = client.post(
        "/query", json={"question": "问题"}, headers=alice_headers
    )

    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


# ---------------------------------------------------------------- 会话


class TestSessions:
    def test_create_and_list(self, alice_headers):
        created = client.post("/api/sessions", json=None, headers=alice_headers).json()
        assert created["title"] == "新会话"

        listed = client.get("/api/sessions", headers=alice_headers).json()["sessions"]
        assert [s["id"] for s in listed] == [created["id"]]

    def test_create_with_title(self, alice_headers):
        created = client.post(
            "/api/sessions", json={"title": "调研笔记"}, headers=alice_headers
        ).json()
        assert created["title"] == "调研笔记"

    def test_rename(self, alice_headers):
        created = client.post("/api/sessions", json=None, headers=alice_headers).json()
        renamed = client.patch(
            f"/api/sessions/{created['id']}",
            json={"title": "改名了"},
            headers=alice_headers,
        ).json()
        assert renamed["title"] == "改名了"

    def test_delete_cascades_messages(self, alice_headers, monkeypatch):
        monkeypatch.setattr(
            server, "answer_question", lambda *args, **kwargs: Answer("回答")
        )
        body = client.post(
            "/query", json={"question": "问题"}, headers=alice_headers
        ).json()

        assert client.delete(
            f"/api/sessions/{body['session_id']}", headers=alice_headers
        ).json()["ok"] is True
        assert client.get(
            f"/api/sessions/{body['session_id']}/messages", headers=alice_headers
        ).status_code == 404
        assert client.get("/api/sessions", headers=alice_headers).json()["sessions"] == []

    def test_title_defaults_to_first_question(self, alice_headers, monkeypatch):
        monkeypatch.setattr(
            server, "answer_question", lambda *args, **kwargs: Answer("回答")
        )
        body = client.post(
            "/query", json={"question": "什么是集线器？"}, headers=alice_headers
        ).json()
        session = client.get("/api/sessions", headers=alice_headers).json()["sessions"][0]
        assert session["title"] == "什么是集线器？"


# ---------------------------------------------------------------- 文档


def _fake_ingest(report: IngestReport):
    """生成一个模拟真实 ingest 契约的桩：把清单写到 manifest_path。

    真实 ingest 会落盘 manifest（来源文件哈希与块数），documents 表的同步
    以清单为准 —— 桩也必须做到这一点，否则同步没有输入。
    """

    def fake_ingest(**kwargs):
        import json as _json

        manifest_path = kwargs["manifest_path"]
        entries = {}
        for name in report.added:
            entries[name] = {"hash": "stub-hash", "chunks": report.chunks_written}
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(_json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return report

    return fake_ingest


class TestDocuments:
    def test_upload_saves_file_and_triggers_ingest(self, monkeypatch, alice, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(
            server,
            "ingest",
            _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=2)),
        )

        response = client.post(
            "/upload",
            files={"files": ("笔记.md", "# 标题\n内容".encode("utf-8"), "text/markdown")},
            headers=alice_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["saved"] == ["笔记.md"]
        assert body["chunks_written"] == 2
        # 落盘进该用户的专属目录
        assert (tmp_path / "users" / str(alice["user"]["id"]) / "笔记.md").read_bytes() == (
            "# 标题\n内容".encode("utf-8")
        )

    def test_upload_rejects_unsupported_extension(self, monkeypatch, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )

        response = client.post(
            "/upload",
            files={"files": ("virus.exe", b"MZ", "application/octet-stream")},
            headers=alice_headers,
        )

        assert response.status_code == 400
        assert list(tmp_path.rglob("*")) == []

    def test_upload_strips_directory_from_filename(self, monkeypatch, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(server, "ingest", lambda **kwargs: IngestReport())

        response = client.post(
            "/upload",
            files={"files": ("../../evil.md", b"x", "text/markdown")},
            headers=alice_headers,
        )

        assert response.status_code == 200
        assert response.json()["saved"] == ["evil.md"]

    def test_upload_surfaces_ingest_errors(self, monkeypatch, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )

        def boom(**kwargs):
            raise RuntimeError("未配置 API Key")

        monkeypatch.setattr(server, "ingest", boom)

        response = client.post(
            "/upload", files={"files": ("a.md", b"x", "text/markdown")}, headers=alice_headers
        )

        assert response.status_code == 400
        assert "API Key" in response.json()["detail"]

    def test_ingest_reports_runtime_error_as_400(self, monkeypatch, alice_headers):
        def boom(**kwargs):
            raise RuntimeError("未配置 API Key")

        monkeypatch.setattr(server, "ingest", boom)

        response = client.post("/ingest", json={"force": False}, headers=alice_headers)

        assert response.status_code == 400
        assert "API Key" in response.json()["detail"]

    def test_ingest_uses_user_paths(self, monkeypatch, alice, alice_headers, tmp_path):
        captured = {}

        def fake_ingest(**kwargs):
            captured.update(kwargs)
            return IngestReport()

        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(server, "ingest", fake_ingest)

        client.post("/ingest", json={}, headers=alice_headers)

        assert captured["data_dir"] == tmp_path / "users" / str(alice["user"]["id"])
        assert captured["manifest_path"] == (
            tmp_path / "storage" / "manifests" / f"user_{alice['user']['id']}.json"
        )
        assert captured["collection"] is not None

    def test_list_documents(self, monkeypatch, alice, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(
            server, "ingest", _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=3))
        )

        client.post(
            "/upload",
            files={"files": ("笔记.md", b"content", "text/markdown")},
            headers=alice_headers,
        )

        docs = client.get("/api/documents", headers=alice_headers).json()["documents"]
        assert len(docs) == 1
        assert docs[0]["filename"] == "笔记.md"
        assert docs[0]["chunks"] == 3
        assert docs[0]["user_id"] == alice["user"]["id"]

    def test_delete_own_document(self, monkeypatch, alice, alice_headers, tmp_path):
        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(
            server, "ingest", _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=1))
        )
        client.post(
            "/upload",
            files={"files": ("笔记.md", b"content", "text/markdown")},
            headers=alice_headers,
        )
        doc_id = client.get("/api/documents", headers=alice_headers).json()["documents"][0]["id"]

        response = client.delete(f"/api/documents/{doc_id}", headers=alice_headers)

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert client.get("/api/documents", headers=alice_headers).json()["documents"] == []
        assert not (tmp_path / "users" / str(alice["user"]["id"]) / "笔记.md").exists()

    def test_delete_document_survives_failing_unlink(self, monkeypatch, alice, alice_headers, tmp_path):
        """删除钩子拦住 unlink 时，删除文档仍要成功（向量与表行清掉）。"""
        from src import documents as documents_module

        monkeypatch.setattr(
            server, "settings", replace(settings, data_dir=tmp_path, storage_dir=tmp_path / "storage")
        )
        monkeypatch.setattr(
            server, "ingest", _fake_ingest(IngestReport(added=["笔记.md"], chunks_written=1))
        )
        client.post(
            "/upload",
            files={"files": ("笔记.md", b"content", "text/markdown")},
            headers=alice_headers,
        )
        doc_id = client.get("/api/documents", headers=alice_headers).json()["documents"][0]["id"]

        def boom(self):
            raise SystemExit("删除钩子")

        monkeypatch.setattr(documents_module.Path, "unlink", boom, raising=False)

        response = client.delete(f"/api/documents/{doc_id}", headers=alice_headers)

        assert response.status_code == 200
        assert response.json()["file_removed"] is False
        assert client.get("/api/documents", headers=alice_headers).json()["documents"] == []


# ---------------------------------------------------------------- 设置


def _temp_env(monkeypatch, tmp_path):
    """把 .env 指向临时文件，防止测试写到真实配置。"""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text("TOP_K=5\n", encoding="utf-8")
    return tmp_path / ".env"


def test_get_settings_never_leaks_full_api_key(monkeypatch, admin_h):
    monkeypatch.setattr(settings, "api_key", "abcdefghijklmnop")

    response = client.get("/settings", headers=admin_h)

    assert response.status_code == 200
    assert response.json()["api_key_hint"] == "abcd****mnop"
    assert "abcdefghijklmnop" not in response.text


def test_get_settings_reports_unset_key(monkeypatch, admin_h):
    monkeypatch.setattr(settings, "api_key", "")

    body = client.get("/settings", headers=admin_h).json()

    assert body["api_key_set"] is False
    assert body["api_key_hint"] == ""


def test_post_settings_persists_to_env_and_resets_client(monkeypatch, admin_h, tmp_path):
    env_path = _temp_env(monkeypatch, tmp_path)
    resets = []
    monkeypatch.setattr(server, "reset_client", lambda: resets.append(1))

    response = client.post(
        "/settings", json={"api_key": "k-123", "top_k": 9}, headers=admin_h
    )

    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] is True
    assert body["top_k"] == 9
    assert resets == [1], "保存后应清掉缓存的模型客户端"

    env_text = env_path.read_text(encoding="utf-8")
    assert "ZHIPUAI_API_KEY=k-123" in env_text
    assert "TOP_K=9" in env_text


def test_post_settings_flags_reindex_when_embedding_changes(monkeypatch, admin_h, tmp_path):
    _temp_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "reset_client", lambda: None)

    body = client.post("/settings", json={"embed_dim": 2048}, headers=admin_h).json()

    assert body["needs_reindex"] is True


def test_post_settings_no_reindex_for_unrelated_change(monkeypatch, admin_h, tmp_path):
    _temp_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "reset_client", lambda: None)

    body = client.post("/settings", json={"top_k": 3}, headers=admin_h).json()

    assert body["needs_reindex"] is False


def test_post_settings_rejects_out_of_range_values(admin_h):
    assert client.post("/settings", json={"top_k": 0}, headers=admin_h).status_code == 422
    assert client.post("/settings", json={"embed_dim": 8}, headers=admin_h).status_code == 422


def test_get_settings_exposes_multi_turn_and_rerank_options(admin_h):
    body = client.get("/settings", headers=admin_h).json()

    assert body["history_turns"] == 6
    assert body["rewrite_enabled"] is True
    assert body["rerank_enabled"] is True
    assert body["rerank_model"] == "rerank"
    assert body["rerank_candidates"] == 20


def test_post_settings_persists_multi_turn_and_rerank(monkeypatch, admin_h, tmp_path):
    env_path = _temp_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "reset_client", lambda: None)

    body = client.post(
        "/settings",
        json={
            "history_turns": 0,
            "rewrite_enabled": False,
            "rerank_enabled": False,
            "rerank_candidates": 30,
        },
        headers=admin_h,
    ).json()

    assert body["history_turns"] == 0, "0 是合法值，不能被当成未填写"
    assert body["rewrite_enabled"] is False
    assert body["rerank_enabled"] is False
    assert body["rerank_candidates"] == 30
    assert body["needs_reindex"] is False, "多轮与精排配置不影响索引"

    text = env_path.read_text(encoding="utf-8")
    assert "HISTORY_TURNS=0" in text
    assert "QUERY_REWRITE=false" in text
    assert "RERANK_ENABLED=false" in text
    assert "RERANK_CANDIDATES=30" in text


def test_post_settings_rejects_invalid_multi_turn_values(admin_h):
    assert client.post("/settings", json={"history_turns": -1}, headers=admin_h).status_code == 422
    assert client.post("/settings", json={"history_turns": 51}, headers=admin_h).status_code == 422
    assert client.post("/settings", json={"rerank_candidates": 0}, headers=admin_h).status_code == 422
    assert client.post("/settings", json={"rerank_candidates": 129}, headers=admin_h).status_code == 422
