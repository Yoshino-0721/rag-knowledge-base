"""API 接口测试（检索、生成与建库均被替换，不产生真实调用）。"""

from dataclasses import replace

from fastapi.testclient import TestClient

from src import server
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
