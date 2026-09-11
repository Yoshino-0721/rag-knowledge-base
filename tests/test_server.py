"""API 接口测试（检索、生成与建库均被替换，不产生真实调用）。"""

import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from src import config, server
from src.config import settings
from src.generate import Answer
from src.ingest import IngestReport
from src.retrieve import Hit

client = TestClient(server.app)


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_serves_chat_page():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_query_rejects_blank_question():
    response = client.post("/query", json={"question": "   "})

    assert response.status_code == 400


def test_query_without_hits_returns_guidance(monkeypatch):
    monkeypatch.setattr(server, "search", lambda question, top_k=None: [])

    response = client.post("/query", json={"question": "没有答案的问题"})

    assert response.status_code == 200
    body = response.json()
    assert body["citations"] == []
    assert "没有检索到" in body["answer"]


def test_query_returns_answer_with_citations(monkeypatch):
    hits = [Hit(text="原文片段", source="a.md", location="", score=0.91)]
    monkeypatch.setattr(server, "search", lambda question, top_k=None: hits)
    monkeypatch.setattr(
        server,
        "generate",
        lambda question, matched: Answer(
            "这是回答 [1]。",
            [{"index": 1, "source": "a.md", "location": "", "score": 0.91, "text": "原文片段"}],
        ),
    )

    response = client.post("/query", json={"question": "问题"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "这是回答 [1]。"
    assert body["citations"][0]["source"] == "a.md"


def test_ingest_reports_runtime_error_as_400(monkeypatch):
    def boom(force=False):
        raise RuntimeError("未配置 API Key")

    monkeypatch.setattr(server, "ingest", boom)

    response = client.post("/ingest", json={"force": False})

    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def test_upload_saves_file_and_triggers_ingest(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "settings", replace(settings, data_dir=tmp_path))
    monkeypatch.setattr(
        server,
        "ingest",
        lambda force=False: IngestReport(added=["笔记.md"], chunks_written=2),
    )

    response = client.post(
        "/upload",
        files={"files": ("笔记.md", "# 标题\n内容".encode("utf-8"), "text/markdown")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["saved"] == ["笔记.md"]
    assert body["chunks_written"] == 2
    assert (tmp_path / "笔记.md").read_bytes() == "# 标题\n内容".encode("utf-8")


def test_upload_rejects_unsupported_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "settings", replace(settings, data_dir=tmp_path))

    response = client.post(
        "/upload",
        files={"files": ("virus.exe", b"MZ", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_upload_strips_directory_from_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "settings", replace(settings, data_dir=tmp_path))
    monkeypatch.setattr(server, "ingest", lambda force=False: IngestReport())

    response = client.post(
        "/upload",
        files={"files": ("../../evil.md", b"x", "text/markdown")},
    )

    assert response.status_code == 200
    assert response.json()["saved"] == ["evil.md"]
    assert (tmp_path / "evil.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()


def test_upload_surfaces_ingest_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "settings", replace(settings, data_dir=tmp_path))

    def boom(force=False):
        raise RuntimeError("未配置 API Key")

    monkeypatch.setattr(server, "ingest", boom)

    response = client.post(
        "/upload", files={"files": ("a.md", b"x", "text/markdown")}
    )

    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def _temp_env(monkeypatch, tmp_path):
    """把 .env 指向临时文件，防止测试写到真实配置。"""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text("TOP_K=5\n", encoding="utf-8")
    return tmp_path / ".env"


def test_get_settings_never_leaks_full_api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "abcdefghijklmnop")

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.json()["api_key_hint"] == "abcd****mnop"
    assert "abcdefghijklmnop" not in response.text


def test_get_settings_reports_unset_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")

    body = client.get("/settings").json()

    assert body["api_key_set"] is False
    assert body["api_key_hint"] == ""


def test_post_settings_persists_to_env_and_resets_client(monkeypatch, tmp_path):
    env_path = _temp_env(monkeypatch, tmp_path)
    resets = []
    monkeypatch.setattr(server, "reset_client", lambda: resets.append(1))

    response = client.post("/settings", json={"api_key": "k-123", "top_k": 9})

    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] is True
    assert body["top_k"] == 9
    assert resets == [1], "保存后应清掉缓存的模型客户端"

    env_text = env_path.read_text(encoding="utf-8")
    assert "ZHIPUAI_API_KEY=k-123" in env_text
    assert "TOP_K=9" in env_text


def test_post_settings_flags_reindex_when_embedding_changes(monkeypatch, tmp_path):
    _temp_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "reset_client", lambda: None)

    body = client.post("/settings", json={"embed_dim": 2048}).json()

    assert body["needs_reindex"] is True


def test_post_settings_no_reindex_for_unrelated_change(monkeypatch, tmp_path):
    _temp_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "reset_client", lambda: None)

    body = client.post("/settings", json={"top_k": 3}).json()

    assert body["needs_reindex"] is False


def test_post_settings_rejects_out_of_range_values():
    assert client.post("/settings", json={"top_k": 0}).status_code == 422
    assert client.post("/settings", json={"embed_dim": 8}).status_code == 422
