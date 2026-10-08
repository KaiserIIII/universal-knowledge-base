"""Additive workflow snapshots and leased runs; credentials are never stored."""
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, GUID
from .models import new_id, utcnow


class ModelProfile(Base):
    __tablename__ = 'saas_model_profiles'
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey('workspaces.id'), index=True)
    config: Mapped[dict] = mapped_column(JSON)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workflow(Base):
    __tablename__ = 'saas_workflows'
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey('workspaces.id'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    graph: Mapped[dict] = mapped_column(JSON)
    published_version_id: Mapped[str | None] = mapped_column(GUID())
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowVersion(Base):
    __tablename__ = 'saas_workflow_versions'
    __table_args__ = (UniqueConstraint('workflow_id', 'version', name='uq_workflow_version'),)
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_workflows.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowRun(Base):
    __tablename__ = 'saas_workflow_runs'
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_workflows.id'), index=True)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey('workspaces.id'), index=True)
    version_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey('saas_workflow_versions.id'))
    created_by: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_users.id'))
    reservation_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_answer_reservations.id'), unique=True)
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    worker_id: Mapped[str] = mapped_column(GUID())
    lease_until: Mapped[object] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    graph: Mapped[dict] = mapped_column(JSON)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default='')
    sources: Mapped[list] = mapped_column(JSON, default=list)
    invalid_citations: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
