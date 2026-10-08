from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, GUID


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid4())


class User(Base):
    __tablename__ = "saas_users"
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Membership(Base):
    __tablename__ = "saas_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_saas_membership"),
        CheckConstraint("role in ('owner', 'admin', 'editor', 'viewer')", name="ck_saas_membership_role"),
    )
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(GUID(), ForeignKey("saas_users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Session(Base):
    __tablename__ = "saas_sessions"
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(GUID(), ForeignKey("saas_users.id"), nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Invitation(Base):
    __tablename__ = "saas_invitations"
    __table_args__ = (CheckConstraint("role in ('owner', 'admin', 'editor', 'viewer')", name="ck_saas_invite_role"),)
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    invited_by: Mapped[str] = mapped_column(GUID(), ForeignKey("saas_users.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class APIKey(Base):
    __tablename__ = "saas_api_keys"
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(GUID(), ForeignKey("saas_users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "saas_audit_events"
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey("saas_users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(GUID())
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
