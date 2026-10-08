"""Additive, tenant-scoped payment bindings and answer accounting."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, GUID
from .models import new_id, utcnow


class Subscription(Base):
    __tablename__ = "saas_subscriptions"
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    checkout_session_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    checkout_url: Mapped[str | None] = mapped_column(String(2048))
    checkout_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_price_id: Mapped[str | None] = mapped_column(String(255))
    price_id: Mapped[str | None] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class StripeEvent(Base):
    __tablename__ = "saas_stripe_events"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(GUID(), ForeignKey("workspaces.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    created: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CheckoutSession(Base):
    """Retain immutable server-created session/customer/tenant/price bindings."""
    __tablename__ = "saas_checkout_sessions"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    price_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MonthlyUsage(Base):
    __tablename__ = "saas_monthly_usage"
    __table_args__ = (
        UniqueConstraint("workspace_id", "month", name="uq_saas_usage_month"),
        CheckConstraint("completed_answers >= 0 AND reserved_answers >= 0", name="ck_saas_usage_nonnegative"),
    )
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    completed_answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AnswerReservation(Base):
    __tablename__ = "saas_answer_reservations"
    __table_args__ = (CheckConstraint("status in ('reserved', 'completed', 'released')", name="ck_saas_reservation_status"),)
    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(GUID(), ForeignKey("workspaces.id"), nullable=False, index=True)
    usage_id: Mapped[str] = mapped_column(GUID(), ForeignKey("saas_monthly_usage.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
