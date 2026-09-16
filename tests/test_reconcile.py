"""启动期向量对账（T9）：清掉「清单里没有、向量库里还留着」的幽灵分块。

背景（真实事故，见 docs/optimization-backlog.md 的 T9）：轮换凭证那次只删了
``storage/app.db``、``storage/manifests/``、``data/users/``，**漏删了
``storage/chroma.sqlite3``** —— 于是旧分块留在向量库里，清单里却没有它们，
检索仍会命中、引用来源仍会列出来。应用自己的删除路径是干净的
（``documents.delete_document`` / ``ingest`` 重灌前都会 delete），
所以这是"绕过应用清库"留下的残留，必须在启动时对账兜住。

这些用例**直接调 ``src.reconcile``**，不走 lifespan —— 与项目其余部分一致，
lifespan 不参与测试，其接线由文件末尾那条用例单独钉住。
"""

from __future__ import annotations

import itertools
import logging

import pytest

from src import reconcile
from src.config import settings
from src.ingest import save_manifest
from src.store import get_collection
from src.userspace import user_paths

# conftest 的 storage_root 是**会话级**的（整个测试会话共用一个 Chroma 目录），
# 所以每个用例必须拿一个自己的用户 id —— 否则前一个用例留下的分块会被
# "清单里没有"的对账当成幽灵，断言就会串味（这是真实踩到过的：单独跑全绿、
# 整文件跑红）。这里用模块内自增计数器保证同一进程内不重复。
_USER_IDS = itertools.count(1000)


@pytest.fixture()
def paths():
    """本用例专属的用户路径（独立集合 + 独立清单）。"""
    return user_paths(settings, next(_USER_IDS))


def _add_chunks(paths, source: str, chunks: int = 1) -> None:
    """往该用户的向量集合里塞 ``chunks`` 条分块，元数据与 src/ingest.py 一致。

    显式传 ``embeddings=``：不这么做 Chroma 会去调默认嵌入函数（下载模型），
    测试要的是"库里有什么"，不是"向量算得对不对"。
    """
    collection = get_collection(paths.collection_name)
    collection.add(
        ids=[f"{source}::{i}" for i in range(chunks)],
        documents=[f"{source} 的第 {i} 段正文" for i in range(chunks)],
        embeddings=[[0.1, 0.2, 0.3] for _ in range(chunks)],
        metadatas=[
            {"source": source, "location": f"第 {i + 1} 页", "chunk_index": i, "hash": "h"}
            for i in range(chunks)
        ],
    )


def _write_manifest(paths, mapping: dict) -> None:
    save_manifest(paths.manifest_path, mapping)


def _stored_sources(paths) -> set[str]:
    return reconcile.collection_sources(get_collection(paths.collection_name))


def _collection_names() -> set[str]:
    import chromadb

    client = chromadb.PersistentClient(path=str(settings.storage_dir))
    return {collection.name for collection in client.list_collections()}


def test_ghost_source_is_purged_and_logged(paths, caplog):
    """向量库有、清单没有的 source：清掉 + 打日志；清单里有的一个都不许动。"""
    _add_chunks(paths, "FX.pdf", chunks=2)
    _add_chunks(paths, "p1-upload.pdf", chunks=1)  # 幽灵：清单里没有它
    _write_manifest(paths, {"FX.pdf": {"hash": "h", "chunks": 2}})

    with caplog.at_level(logging.WARNING, logger="src.reconcile"):
        ghosts = reconcile.reconcile_user_vectors(paths)

    assert ghosts == ["p1-upload.pdf"]
    assert _stored_sources(paths) == {"FX.pdf"}, "只该清幽灵，真文档的分块必须原样保留"
    assert "p1-upload.pdf" in caplog.text, "清理必须留下日志，否则没人知道库里被改过"


def test_every_ghost_of_a_multi_source_library_is_reported(paths, caplog):
    """真实事故里是两个幽灵（``p1-upload.pdf`` 与 ``e2e-p1-upload.pdf``），一并清掉。"""
    _add_chunks(paths, "FX.pdf", chunks=2)
    _add_chunks(paths, "p1-upload.pdf", chunks=6)
    _add_chunks(paths, "e2e-p1-upload.pdf", chunks=6)
    _write_manifest(paths, {"FX.pdf": {"hash": "h", "chunks": 2}})

    with caplog.at_level(logging.WARNING, logger="src.reconcile"):
        ghosts = reconcile.reconcile_user_vectors(paths)

    assert ghosts == ["e2e-p1-upload.pdf", "p1-upload.pdf"]
    assert _stored_sources(paths) == {"FX.pdf"}


def test_clean_library_is_left_alone(paths, caplog):
    """没有幽灵时必须是空操作 —— 对账不许有副作用。"""
    _add_chunks(paths, "FX.pdf", chunks=3)
    _write_manifest(paths, {"FX.pdf": {"hash": "h", "chunks": 3}})

    with caplog.at_level(logging.WARNING, logger="src.reconcile"):
        assert reconcile.reconcile_user_vectors(paths) == []

    assert _stored_sources(paths) == {"FX.pdf"}
    assert "幽灵" not in caplog.text


def test_manifest_without_any_document_purges_everything(paths):
    """清库时把 manifests/ 删了（只剩空清单）→ 向量库里的一切都是幽灵。"""
    _add_chunks(paths, "a.pdf", chunks=2)
    _add_chunks(paths, "b.pdf", chunks=1)
    _write_manifest(paths, {})

    assert sorted(reconcile.reconcile_user_vectors(paths)) == ["a.pdf", "b.pdf"]
    assert _stored_sources(paths) == set()


def test_missing_manifest_file_purges_everything(paths):
    """清单文件整个不见了，语义与空清单一致。"""
    _add_chunks(paths, "a.pdf", chunks=1)

    assert reconcile.reconcile_user_vectors(paths) == ["a.pdf"]
    assert _stored_sources(paths) == set()


def test_reconcile_is_idempotent(paths, caplog):
    """对账要能反复跑（每次启动都跑）：第二次无事可做。"""
    _add_chunks(paths, "FX.pdf", chunks=1)
    _add_chunks(paths, "ghost.pdf", chunks=1)
    _write_manifest(paths, {"FX.pdf": {"hash": "h", "chunks": 1}})

    assert reconcile.reconcile_user_vectors(paths) == ["ghost.pdf"]
    with caplog.at_level(logging.WARNING, logger="src.reconcile"):
        assert reconcile.reconcile_user_vectors(paths) == []


def test_user_without_any_stored_data_is_skipped(paths):
    """账号存在但没有任何落盘数据（新账号）不该报错，也不该凭空建出幽灵。

    附带钉住一条真机踩到的副作用：对账必须用**不创建**集合的读取方式 ——
    否则每个没上传过文档的账号都会被"对账"出一份空集合（`user_3_docs`）。
    """
    assert reconcile.reconcile_user_vectors(paths) == []
    assert paths.collection_name not in _collection_names(), "对账不该创建集合"


def test_reconcile_user_vectors_tolerates_corrupt_manifest(paths):
    """清单被写坏（JSONDecodeError）时按"清单里什么都没有"处理，而不是崩。"""
    _add_chunks(paths, "a.pdf", chunks=1)
    paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert reconcile.reconcile_user_vectors(paths) == ["a.pdf"]


@pytest.mark.parametrize("raw", ["[]", "null"])
def test_manifest_with_a_json_but_wrong_shape_still_reconciles(paths, raw):
    """清单是合法 JSON 但结构不是对象（``[]`` / ``null``）时按空清单处理，不许 500。"""
    _add_chunks(paths, "a.pdf", chunks=1)
    paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.write_text(raw, encoding="utf-8")

    assert reconcile.reconcile_user_vectors(paths) == ["a.pdf"]


def test_all_vectors_never_breaks_startup(monkeypatch, caplog):
    """单个账号对账炸了必须跳过并记日志：清理是附带动作，不许把启动带崩。"""
    seen: list[int] = []

    def flaky(paths):
        seen.append(int(paths.data_dir.name))
        if len(seen) == 1:
            raise RuntimeError("磁盘炸了")
        return ["ghost.pdf"]

    monkeypatch.setattr(reconcile, "reconcile_user_vectors", flaky)
    with caplog.at_level(logging.WARNING, logger="src.reconcile"):
        cleaned = reconcile.reconcile_all_vectors([3, 5])

    assert seen == [3, 5], "一个账号失败不该中断其余账号"
    assert cleaned == {5: ["ghost.pdf"]}
    assert "磁盘炸了" in caplog.text


def test_all_vectors_reports_a_mapping_of_cleaned_sources(paths):
    """返回 {用户 id: 清掉的 source}，便于调用方记录/断言。"""
    first = user_paths(settings, next(_USER_IDS))
    second = user_paths(settings, next(_USER_IDS))
    for target, ghost in ((first, "ghost-a.pdf"), (second, "ghost-b.pdf")):
        _add_chunks(target, ghost, chunks=1)
        _write_manifest(target, {})

    ids = [int(first.data_dir.name), int(second.data_dir.name)]
    assert reconcile.reconcile_all_vectors(ids) == {
        ids[0]: ["ghost-a.pdf"],
        ids[1]: ["ghost-b.pdf"],
    }


def test_all_user_ids_reads_every_account_from_the_auth_db(client):
    """对账对象是「所有账号」：从 auth 库现读，不依赖任何内存状态。"""
    from tests.conftest import register_and_login

    register_and_login(client, "reconcileprobe")

    ids = reconcile.all_user_ids()
    assert ids, "引导出来的 admin 至少应在列表里"
    assert len(ids) == len(set(ids)), "同一账号只该对账一次"
    assert min(ids) == 1, "引导管理员的 id 是 1"


def test_lifespan_wires_reconciliation_at_startup(monkeypatch):
    """接线检查：启动时确实用「所有账号」跑了一次对账。

    这里把 lifespan 的前两步（安全自检、建库）替换掉，只验证**对账这一步接上了** ——
    前两步由 tests/test_auth_security.py 等各自覆盖，且都不该在单测里碰真实磁盘。
    """
    from fastapi.testclient import TestClient

    from src import server

    calls: list[list[int]] = []
    monkeypatch.setattr(server, "verify_security_config", lambda: None)
    monkeypatch.setattr(server, "init_db", lambda: None)
    monkeypatch.setattr(server, "all_user_ids", lambda: [1, 2])
    monkeypatch.setattr(server, "reconcile_all_vectors", lambda ids: calls.append(list(ids)))

    with TestClient(server.app):
        pass

    assert calls == [[1, 2]]


def test_reconcile_does_not_touch_the_real_storage():
    """守卫：测试期的对账必须落在临时目录里。

    ``storage/`` 在 .gitignore 里，误写真实目录 git status 看不出来；
    这里直接钉住"不是仓库里那个 storage/"，防止有人绕开 conftest 的隔离。
    """
    from src.config import PROJECT_ROOT

    assert settings.storage_dir != PROJECT_ROOT / "storage"
    assert settings.storage_dir != settings.data_dir
