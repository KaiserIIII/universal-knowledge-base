"""Additive configuration and durable ingestion queue; source paths are private."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, GUID
from .models import utcnow


class KnowledgeConfig(Base):
    __tablename__ = 'saas_knowledge_configs'
    kb_id: Mapped[str] = mapped_column(GUID(), ForeignKey('knowledge_bases.id'), primary_key=True)
    chunk_size: Mapped[int] = mapped_column(Integer, default=1000)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=200)
    use_unstructured: Mapped[bool] = mapped_column(Boolean, default=False)
    parser_config: Mapped[dict] = mapped_column(JSON, default=dict)


class IngestionJob(Base):
    __tablename__ = 'saas_ingestion_jobs'
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=lambda: str(uuid4()))
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey('workspaces.id'), index=True)
    kb_id: Mapped[str] = mapped_column(GUID(), ForeignKey('knowledge_bases.id'), index=True)
    # Deliberately retained after physical document deletion for cleanup/audit.
    doc_id: Mapped[str] = mapped_column(GUID(), unique=True)
    filename: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16), default='ingest')
    status: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
