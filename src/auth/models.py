"""ORM 模型：用户、会话与聊天消息。

表名刻意与需求保持一致：``users`` / ``sessions`` / ``messages``。
``ChatSession`` 只是为避开与 SQLAlchemy Session 同名的冲突，落库表名仍是
``sessions``。时间统一用 naive 本地时间 —— 本机部署，界面按本地时间读即可。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now()


class User(Base):
    """注册用户。密码只存 bcrypt 哈希，绝不存明文。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # user：只能操作自己的数据；admin：可管理全站
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChatSession(Base):
    """一次问答会话。归属一个用户，删除时级联删掉全部消息。"""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 默认取首条提问截断；前端可改名
    title: Mapped[str] = mapped_column(String(200), default="新会话", server_default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    """一条聊天记录。

    ``meta`` 存 JSON 文本，放随消息一起持久化的附加信息（项目一放引用卡片，
    项目二放执行轨迹），管理员查看记录时可以一并还原。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    # user / assistant（与聊天语义一致，与 User.role 的取值无关）
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class Document(Base):
    """一个用户上传的文档的元数据（向量库之外的可查询索引）。

    ``rel_path`` 是相对该用户数据目录的路径，与 Chroma metadata 的
    ``source`` 字段一致 —— 删除文档时用它清理向量与清单。
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    rel_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    size: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
