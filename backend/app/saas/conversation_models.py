"""Additive organization conversation, answer and per-member feedback tables."""
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base, GUID
from .models import new_id, utcnow


class Conversation(Base):
    __tablename__ = 'saas_conversations'
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey('workspaces.id'), index=True)
    title: Mapped[str] = mapped_column(String(200), default='New conversation')
    created_by: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_users.id'))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = 'saas_messages'
    __table_args__ = (
        UniqueConstraint('conversation_id', 'position', name='uq_message_position'),
        CheckConstraint("role in ('user','assistant')", name='ck_message_role'),
        CheckConstraint("status in ('completed','running','insufficient_evidence','error','canceled')", name='ck_message_status'),
    )
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_conversations.id'), index=True)
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(32), default='completed')
    query: Mapped[str] = mapped_column(Text, default='')
    sources: Mapped[list] = mapped_column(JSON, default=list)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(64))
    invalid_citations: Mapped[bool] = mapped_column(Boolean, default=False)
    reservation_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey('saas_answer_reservations.id'), unique=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class Feedback(Base):
    __tablename__ = 'saas_feedback'
    __table_args__ = (
        UniqueConstraint('message_id', 'user_id', name='uq_feedback_member'),
        CheckConstraint("rating in ('positive','negative')", name='ck_feedback_rating'),
    )
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_messages.id'), index=True)
    user_id: Mapped[str] = mapped_column(GUID(), ForeignKey('saas_users.id'))
    rating: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
