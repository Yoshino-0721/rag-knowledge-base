"""共享测试夹具。"""

from __future__ import annotations

import pytest

from src.config import settings

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


@pytest.fixture(autouse=True)
def isolated_auth_db(tmp_path_factory, monkeypatch):
    """每个用例独享一个全新 SQLite，绝不让测试碰到真实的 storage/app.db。

    同时固定 JWT_SECRET，避免签发密钥落盘到真实 storage/ 目录。
    目录取自 tmp_path_factory，与用例自己的 tmp_path 互不干扰。
    """
    from src.auth import db as auth_db

    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
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
    """默认管理员（引导创建的 admin/admin123）登录后的请求头。"""
    response = client.post(
        "/api/auth/login", json={"account": "admin", "password": "admin123"}
    )
    assert response.status_code == 200, response.text
    return auth_headers(response.json()["token"])
