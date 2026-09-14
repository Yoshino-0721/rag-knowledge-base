"""会话与消息的 JSON 序列化（用户接口与管理接口共用）。"""

from __future__ import annotations

import json

from src.auth.models import ChatSession, Message


def message_payload(message: Message) -> dict:
    meta = None
    if message.meta:
        try:
            meta = json.loads(message.meta)
        except json.JSONDecodeError:
            meta = None
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "meta": meta,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def session_payload(chat_session: ChatSession, *, username: str | None = None) -> dict:
    data = {
        "id": chat_session.id,
        "title": chat_session.title,
        "created_at": chat_session.created_at.isoformat() if chat_session.created_at else None,
        "updated_at": chat_session.updated_at.isoformat() if chat_session.updated_at else None,
    }
    if username is not None:
        data["username"] = username
    return data
