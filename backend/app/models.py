"""
企业级知识库 — SQLAlchemy 2.0 ORM 核心领域模型 (SQLite 兼容版)
支持: 多工作空间隔离 / 多知识库分类 / 文档状态机 / 软删除 / 审计字段
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import List, Optional
import json

from sqlalchemy import (
    Boolean,
    CHAR,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    event,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import TypeDecorator


# ══════════════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════════════
class Base(DeclarativeBase):
    """Declarative base shared by every domain model."""


class GUID(TypeDecorator):
    """Store UUID strings as native UUID on PostgreSQL and CHAR(36) on SQLite."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=False))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid UUID value: {value!r}") from error

    def process_result_value(self, value, dialect):
        return str(value) if value is not None else None


# ══════════════════════════════════════════════════════════════════
# SQLite JSON 序列化辅助
# ══════════════════════════════════════════════════════════════════
def _json_dumps(obj):
    """将 Python list/dict 转成 JSON 字符串存入 SQLite"""
    if obj is None:
        return "[]"
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(text):
    """将 SQLite TEXT 列转回 Python list/dict"""
    if not text or text == "":
        return []
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []


# ══════════════════════════════════════════════════════════════════
# 枚举定义
# ══════════════════════════════════════════════════════════════════
class DocStatus(str, enum.Enum):
    UPLOADING  = "uploading"
    PARSING    = "parsing"
    EMBEDDING  = "embedding"
    COMPLETED  = "completed"
    FAILED     = "failed"


class KBType(str, enum.Enum):
    TECHNICAL  = "technical"
    HR         = "hr"
    PRODUCT    = "product"
    OPERATIONS = "operations"
    FINANCE    = "finance"
    LEGAL      = "legal"
    GENERAL    = "general"
    CUSTOM     = "custom"


# ══════════════════════════════════════════════════════════════════
# Mixin
# ══════════════════════════════════════════════════════════════════
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class SoftDeleteMixin:
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ══════════════════════════════════════════════════════════════════
# 1. Workspace
# ══════════════════════════════════════════════════════════════════
class Workspace(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    knowledge_bases: Mapped[List["KnowledgeBase"]] = relationship(
        "KnowledgeBase", back_populates="workspace",
        cascade="all, delete-orphan", lazy="raise", passive_deletes=True,
    )


# ══════════════════════════════════════════════════════════════════
# 2. KnowledgeBase
# ══════════════════════════════════════════════════════════════════
class KnowledgeBase(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kb_type: Mapped[KBType] = mapped_column(
        SQLEnum(KBType), default=KBType.GENERAL, nullable=False, index=True
    )
    # SQLite 用 TEXT 存 JSON
    _category_tags: Mapped[str] = mapped_column("category_tags", Text, default="[]")
    avatar: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    total_chars: Mapped[int] = mapped_column(Integer, default=0)

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="knowledge_bases")
    documents: Mapped[List["Document"]] = relationship(
        "Document", back_populates="knowledge_base",
        cascade="all, delete-orphan", lazy="raise", passive_deletes=True,
    )

    @property
    def category_tags(self) -> list:
        return _json_loads(self._category_tags)

    @category_tags.setter
    def category_tags(self, value: list):
        self._category_tags = _json_dumps(value)


# ══════════════════════════════════════════════════════════════════
# 3. Document
# ══════════════════════════════════════════════════════════════════
class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "file_hash", name="uq_documents_kb_file_hash"),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=lambda: str(uuid.uuid4()))
    kb_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[DocStatus] = mapped_column(
        SQLEnum(DocStatus), default=DocStatus.UPLOADING, nullable=False, index=True
    )
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    word_count: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    knowledge_base: Mapped["KnowledgeBase"] = relationship("KnowledgeBase", back_populates="documents")
    chunks: Mapped[List["DocumentChunk"]] = relationship(
        "DocumentChunk", back_populates="document",
        cascade="all, delete-orphan", lazy="raise", passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )


# ══════════════════════════════════════════════════════════════════
# 4. DocumentChunk
# ══════════════════════════════════════════════════════════════════
class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index", name="uq_document_chunks_doc_index"),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=lambda: str(uuid.uuid4()))
    doc_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")
