"""共享测试夹具。"""

from __future__ import annotations

import pytest

from src.config import settings

# 测试期统一使用的管理员口令。
#
# 两个刻意的选择：① 它满足强口令要求（≥12 位、≥3 类字符），因为弱口令现在会让
# 引导直接抛 RuntimeError；② 它由 ADMIN_PASSWORD **显式提供**，所以引导出来的
# 管理员 `must_change_password=False`，不会被"必须先改密"的硬闸门拦住 ——
# 否则所有调用管理接口的既有用例都会 403。强制改密那条链路由
# tests/test_auth_security.py 用"不设 ADMIN_PASSWORD"专门覆盖。
TEST_ADMIN_PASSWORD = "Unit-Test-Admin-9x!"

_TRACKED_FIELDS = (
    "api_key",
    "base_url",
    "chat_model",
    "embed_model",
    "embed_dim",
    "top_k",
    "history_turns",
    "rewrite_enabled",
    "rerank_enabled",
    "rerank_model",
    "rerank_candidates",
)

# 每个用例开始前强制回到这组基线值，避免受开发者本机 .env 影响。
_BASELINE = {
    "history_turns": 6,
    "rewrite_enabled": True,
    "rerank_enabled": True,
    "rerank_model": "rerank",
    "rerank_candidates": 20,
}


@pytest.fixture(autouse=True)
def restore_global_settings():
    """settings 是全局单例，用例改动后必须还原，避免互相污染。"""
    snapshot = {name: getattr(settings, name) for name in _TRACKED_FIELDS}
    for name, value in _BASELINE.items():
        setattr(settings, name, value)
    yield
    for name, value in snapshot.items():
        setattr(settings, name, value)


@pytest.fixture(scope="session")
def storage_root(tmp_path_factory):
    """整个测试会话共用一个临时落盘根（向量库、清单、SQLite 都放这里）。"""
    return tmp_path_factory.mktemp("storage-root")


@pytest.fixture(autouse=True)
def isolated_storage(storage_root, monkeypatch):
    """把全局 settings 的数据/存储目录指到临时根，**任何用例都不许碰真实 storage/**。

    这条是补齐出来的：``src.store.get_collection`` 读的是全局 ``settings.storage_dir``，
    而早期用例只替换了 ``server`` 模块上那个引用 —— 两者是同一个对象时没问题，
    一旦有人写法不一致，测试就会在仓库的真实 ``storage/`` 里建出 Chroma 集合
    （``storage/`` 在 .gitignore 里，所以 git status 看不出来，很容易长期没人发现）。
    统一在这里兜住：跑测试不再写入任何真实数据目录。
    """
    monkeypatch.setattr(settings, "storage_dir", storage_root / "storage")
    monkeypatch.setattr(settings, "data_dir", storage_root / "data")


@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    """测试期把 bcrypt 成本降到最低 —— **生产代码一行不改**。

    ``hash_password`` 在多个模块里是 ``from ... import`` 绑定的，所以每一处
    绑定都要替换；漏掉一处，那条路径就还在用 rounds=12 真算一遍。
    不这么做的话：autouse 的 ``isolated_auth_db`` 每个用例都要引导一次默认管理员，
    288 个用例光这一项就要多花一百多秒。
    """
    import bcrypt

    from src import admin_api
    from src.auth import db as auth_db, security

    def cheap_hash(password: str) -> str:
        return bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("ascii")

    monkeypatch.setattr(security, "hash_password", cheap_hash)
    monkeypatch.setattr(auth_db, "hash_password", cheap_hash)
    monkeypatch.setattr(admin_api, "hash_password", cheap_hash)


@pytest.fixture(autouse=True)
def open_registration_and_clear_throttle(monkeypatch):
    """测试期放开自助注册、并清空登录节流状态。

    这两条默认值（注册默认关闭、登录失败到阈值锁定）都会让**既有用例互相干扰**：
    注册会直接 403；某个用例故意试错几次，后面的用例就会被锁在 429 上。
    针对这两条本身的用例在 tests/test_auth_throttle.py 里把它们单独调回来 ——
    默认值仍保持"关闭 / 开启节流"，测试期只是显式地打开它。
    """
    from src.auth import throttle

    monkeypatch.setattr(settings, "allow_registration", True)
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture(autouse=True)
def isolated_auth_db(fast_bcrypt, tmp_path_factory, monkeypatch):
    """每个用例独享一个全新 SQLite，绝不让测试碰到真实的 storage/app.db。

    同时固定 JWT_SECRET，避免签发密钥落盘到真实 storage/ 目录。
    目录取自 tmp_path_factory，与用例自己的 tmp_path 互不干扰。
    显式依赖 fast_bcrypt：引导默认管理员要哈希口令，顺序反了就白等一次 bcrypt。
    """
    from src.auth import db as auth_db

    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    auth_db.init_db(tmp_path_factory.mktemp("authdb") / "auth.db")
    yield
    auth_db.dispose_engine()


@pytest.fixture()
def client():
    """带独立认证库的 TestClient。"""
    from fastapi.testclient import TestClient

    from src.server import app

    return TestClient(app)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(client, username="alice", email=None, password="secret123") -> dict:
    """注册并登录一个用户，返回 {token, user}。"""
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def admin_headers(client) -> dict:
    """引导创建的管理员登录后的请求头（口令见 TEST_ADMIN_PASSWORD）。"""
    response = client.post(
        "/api/auth/login",
        json={"account": "admin", "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return auth_headers(response.json()["token"])


# ---------------------------------------------------------------- 常用账号夹具
#
# 多用户接口的用例基本都需要「两个普通用户 + 一个管理员」，统一提到这里
# 避免每个测试文件各写一份。普通用户现注册现登录，管理员是引导创建的那个。


@pytest.fixture()
def alice(client) -> dict:
    return register_and_login(client, "alice")


@pytest.fixture()
def alice_headers(alice) -> dict:
    return auth_headers(alice["token"])


@pytest.fixture()
def bob(client) -> dict:
    return register_and_login(client, "bob")


@pytest.fixture()
def bob_headers(bob) -> dict:
    return auth_headers(bob["token"])


@pytest.fixture()
def admin_h(client) -> dict:
    return admin_headers(client)
