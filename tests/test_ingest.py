"""建库流水线与增量索引测试（用假集合与假向量函数，不触碰真实 API）。"""

import json
from pathlib import Path

import pytest

from src.ingest import discover_files, file_hash, ingest


class FakeCollection:
    """最小可用的 Chroma 集合替身。"""

    def __init__(self):
        self.documents: dict[str, str] = {}
        self.embeddings: dict[str, list] = {}
        self.metadatas: dict[str, dict] = {}

    def add(self, ids, documents, embeddings, metadatas):
        for identifier, document, embedding, metadata in zip(
            ids, documents, embeddings, metadatas
        ):
            self.documents[identifier] = document
            self.embeddings[identifier] = embedding
            self.metadatas[identifier] = metadata

    def delete(self, where=None):
        source = (where or {}).get("source")
        targets = [k for k, m in self.metadatas.items() if m.get("source") == source]
        for key in targets:
            self.documents.pop(key, None)
            self.embeddings.pop(key, None)
            self.metadatas.pop(key, None)

    def count(self):
        return len(self.documents)


def fake_embed(texts):
    """占位向量：长度固定，便于断言调用次数。"""
    return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "a.md").write_text("第一份文档的内容。" * 20, encoding="utf-8")
    return {
        "data_dir": data_dir,
        "manifest": tmp_path / "storage" / "manifest.json",
        "collection": FakeCollection(),
    }


def run_ingest(env, **kwargs):
    return ingest(
        data_dir=env["data_dir"],
        collection=env["collection"],
        embed_fn=fake_embed,
        manifest_path=env["manifest"],
        **kwargs,
    )


def test_discover_files_filters_unsupported(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "keep.md").write_text("内容", encoding="utf-8")
    (data_dir / "skip.png").write_bytes(b"\x89PNG")
    (data_dir / "nested").mkdir()
    (data_dir / "nested" / "deep.py").write_text("print(1)", encoding="utf-8")

    found = {p.name for p in discover_files(data_dir)}

    assert found == {"keep.md", "deep.py"}


def test_file_hash_changes_with_content(tmp_path: Path):
    path = tmp_path / "x.txt"
    path.write_text("a", encoding="utf-8")
    first = file_hash(path)
    path.write_text("b", encoding="utf-8")

    assert first != file_hash(path)


def test_first_ingest_adds_and_writes_chunks(env):
    report = run_ingest(env)

    assert report.added == ["a.md"]
    assert report.updated == []
    assert report.removed == []
    assert report.chunks_written >= 1
    assert env["collection"].count() == report.chunks_written


def test_second_ingest_is_unchanged_noop(env):
    run_ingest(env)
    before = env["collection"].count()

    report = run_ingest(env)

    assert report.unchanged == ["a.md"]
    assert report.chunks_written == 0
    assert env["collection"].count() == before


def test_modified_file_is_reindexed(env):
    run_ingest(env)
    (env["data_dir"] / "a.md").write_text("内容已被替换。" * 20, encoding="utf-8")

    report = run_ingest(env)

    assert report.updated == ["a.md"]
    assert report.added == []


def test_deleted_file_is_purged(env):
    run_ingest(env)
    (env["data_dir"] / "a.md").unlink()

    report = run_ingest(env)

    assert report.removed == ["a.md"]
    assert env["collection"].count() == 0


def test_force_flag_rebuilds_everything(env):
    run_ingest(env)

    report = run_ingest(env, force=True)

    assert report.added == ["a.md"]
    assert report.unchanged == []


def test_manifest_is_persisted(env):
    run_ingest(env)

    manifest = json.loads(env["manifest"].read_text(encoding="utf-8"))

    assert "a.md" in manifest
    assert manifest["a.md"]["chunks"] >= 1


def test_chunk_ids_are_unique(env):
    (env["data_dir"] / "b.txt").write_text("第二份文档。" * 30, encoding="utf-8")

    report = run_ingest(env)

    assert env["collection"].count() == report.chunks_written
    assert len(set(env["collection"].documents)) == report.chunks_written


def test_metadata_carries_source_and_location(env):
    run_ingest(env)

    metadata = next(iter(env["collection"].metadatas.values()))

    assert metadata["source"] == "a.md"
    assert metadata["location"] == ""
    assert metadata["hash"]


def test_empty_data_dir_produces_empty_report(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    report = ingest(
        data_dir=data_dir,
        collection=FakeCollection(),
        embed_fn=fake_embed,
        manifest_path=tmp_path / "manifest.json",
    )

    assert report.as_dict()["total_files"] == 0
    assert report.chunks_written == 0
