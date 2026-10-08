"""Quotas within the caller's transaction; no service here commits.

Mutation callers must start SQLite BEGIN IMMEDIATE before reading. The HTTP
get_db dependency does this; background workers must do it explicitly. All
services additionally take the workspace row lock for PostgreSQL. Keep resource
checks and the corresponding INSERT in that same transaction.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, KnowledgeBase, Workspace
from .billing_models import AnswerReservation, MonthlyUsage, Subscription
from .dependencies import lock_workspace, valid_id
from .models import Membership, new_id, utcnow
from .security import aware


PLAN_LIMITS = {
    "free": {"members": 1, "knowledge_bases": 3, "documents": 50, "monthly_answers": 100},
    "team": {"members": 10, "knowledge_bases": 20, "documents": 1000, "monthly_answers": 5000},
    "business": {"members": 50, "knowledge_bases": 100, "documents": 10000, "monthly_answers": 50000},
}


def price_plans(settings) -> dict[str, str]:
    if settings is None:
        return {}
    team, business = settings.stripe_team_price_id, settings.stripe_business_price_id
    # An ambiguous configuration must never award a higher tier.
    if team and business and team == business:
        return {}
    return {price: plan for price, plan in ((team, "team"), (business, "business")) if price}


async def get_entitlements(db: AsyncSession, workspace_id: str, *, now: datetime | None = None) -> dict:
    workspace_id = valid_id(workspace_id)
    if await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id, Workspace.is_deleted.is_(False))) is None:
        raise HTTPException(404, "Resource not found")
    now = aware(now or utcnow())
    row = await db.get(Subscription, workspace_id)
    plan = "free"
    allowed = price_plans(db.info.get("settings"))
    if (row and row.subscription_id and row.status in {"active", "trialing"}
            and row.current_period_start and row.current_period_end
            and aware(row.current_period_start) <= now < aware(row.current_period_end)
            and allowed.get(row.price_id) == row.plan):
        plan = row.plan
    return {"plan": plan, "limits": dict(PLAN_LIMITS[plan])}


async def resource_usage(db: AsyncSession, workspace_id: str) -> dict:
    members = await db.scalar(select(func.count(Membership.id)).where(Membership.workspace_id == workspace_id))
    kbs = await db.scalar(select(func.count(KnowledgeBase.id)).where(KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.is_deleted.is_(False)))
    documents = await db.scalar(select(func.count(Document.id)).join(KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.is_deleted.is_(False)))
    return {"members": members, "knowledge_bases": kbs, "documents": documents}


async def check_resource_quota(db: AsyncSession, workspace_id: str, resource: str, *, increment: int = 1, now: datetime | None = None) -> None:
    if resource not in {"members", "knowledge_bases", "documents"} or type(increment) is not int or increment < 1:
        raise ValueError("Expected a resource name and positive integer increment")
    await lock_workspace(db, valid_id(workspace_id))
    entitlements = await get_entitlements(db, workspace_id, now=now)
    counts = await resource_usage(db, workspace_id)
    if counts[resource] + increment > entitlements["limits"][resource]:
        raise HTTPException(409, {"code": "quota_exceeded", "resource": resource, "limit": entitlements["limits"][resource]})


async def check_member_quota(db: AsyncSession, workspace_id: str) -> None:
    await check_resource_quota(db, workspace_id, "members")


def month_key(now: datetime | None = None) -> str:
    return aware(now or utcnow()).astimezone(timezone.utc).strftime("%Y-%m")


async def reserve_answer(db: AsyncSession, workspace_id: str, reservation_id: str | None = None, *, now: datetime | None = None) -> AnswerReservation:
    workspace_id = valid_id(workspace_id)
    reservation_id = valid_id(reservation_id) if reservation_id is not None else new_id()
    await lock_workspace(db, workspace_id)
    entitlements = await get_entitlements(db, workspace_id, now=now)
    existing = await db.get(AnswerReservation, reservation_id, populate_existing=True)
    if existing is not None:
        if existing.workspace_id != workspace_id:
            raise HTTPException(404, "Resource not found")
        return existing
    month = month_key(now)
    usage = await db.scalar(select(MonthlyUsage).where(MonthlyUsage.workspace_id == workspace_id, MonthlyUsage.month == month).with_for_update())
    if usage is None:
        usage = MonthlyUsage(workspace_id=workspace_id, month=month, completed_answers=0, reserved_answers=0)
        db.add(usage)
        await db.flush()
    limit = entitlements["limits"]["monthly_answers"]
    changed = await db.execute(update(MonthlyUsage).where(MonthlyUsage.id == usage.id, MonthlyUsage.completed_answers + MonthlyUsage.reserved_answers < limit).values(reserved_answers=MonthlyUsage.reserved_answers + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(409, {"code": "quota_exceeded", "resource": "monthly_answers", "limit": limit})
    reservation = AnswerReservation(id=reservation_id, workspace_id=workspace_id, usage_id=usage.id, status="reserved", created_at=now or utcnow())
    db.add(reservation)
    await db.flush()
    return reservation


async def finish_answer(db: AsyncSession, workspace_id: str, reservation_id: str, *, succeeded: bool) -> AnswerReservation:
    if type(succeeded) is not bool:
        raise ValueError("succeeded must be a boolean")
    await lock_workspace(db, valid_id(workspace_id))
    row = await db.scalar(select(AnswerReservation).where(AnswerReservation.id == valid_id(reservation_id), AnswerReservation.workspace_id == workspace_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "Resource not found")
    if row.status != "reserved":
        return row
    result = await db.execute(update(MonthlyUsage).where(MonthlyUsage.id == row.usage_id, MonthlyUsage.workspace_id == workspace_id, MonthlyUsage.reserved_answers > 0).values(reserved_answers=MonthlyUsage.reserved_answers - 1, completed_answers=MonthlyUsage.completed_answers + int(succeeded)).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "Reservation accounting conflict")
    row.status = "completed" if succeeded else "released"
    row.finished_at = utcnow()
    await db.flush()
    return row
