"""Tenant billing routes and signed, authoritative Stripe lifecycle sync."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .billing_models import CheckoutSession, MonthlyUsage, StripeEvent, Subscription
from .dependencies import AccessContext, get_access, get_db, lock_workspace
from .models import AuditEvent, User, utcnow
from .dependencies import require_role
from .payments import verify_webhook_signature
from .quotas import PLAN_LIMITS, get_entitlements, month_key, price_plans, resource_usage
from .security import aware


router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["team", "business"]


def configured_payment(request):
    if not request.app.state.settings.stripe_secret_key or request.app.state.payment is None:
        raise HTTPException(503, "Billing is not configured")
    return request.app.state.payment


def hosted_url(value, host):
    if not isinstance(value, str):
        raise ValueError("Invalid payment URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("Invalid payment URL")
    return value


def stripe_id(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 255:
        raise ValueError("Invalid Stripe ID")
    return value


async def known_checkout(db, account, session_id):
    binding = await db.get(CheckoutSession, session_id)
    # Additive upgrade: recover the previous version's one retained binding.
    if binding is None and account.checkout_session_id == session_id and account.pending_price_id:
        binding = CheckoutSession(id=session_id, workspace_id=account.workspace_id,
                                  customer_id=account.customer_id, price_id=account.pending_price_id)
        db.add(binding)
        await db.flush()
    if binding is None or binding.workspace_id != account.workspace_id or binding.customer_id != account.customer_id:
        raise ValueError("Unknown checkout session")
    return binding


def checkout_state(session, binding):
    if (session.get("id") != binding.id or session.get("customer") != binding.customer_id
            or session.get("metadata", {}).get("workspace_id") != binding.workspace_id
            or session.get("client_reference_id") != binding.workspace_id or session.get("mode") != "subscription"
            or session.get("status") not in {"open", "complete", "expired"}):
        raise ValueError("Checkout binding mismatch")
    return session["status"]


def checkout_subscription_values(current, account, binding, settings, subscription_id):
    values = subscription_values(current, account, settings, subscription_id)
    if binding.price_id != values["price_id"] or binding.subscription_id not in {None, subscription_id}:
        raise ValueError("Checkout subscription mismatch")
    if account.subscription_id and account.subscription_id != subscription_id and account.status not in {"canceled", "incomplete_expired"}:
        raise ValueError("Subscription mismatch")
    return values


def apply_subscription(account, binding, values):
    for name, value in values.items():
        setattr(account, name, value)
    if binding:
        binding.subscription_id = values["subscription_id"]
        account.checkout_url = None
        account.checkout_expires_at = None
    account.updated_at = utcnow()


@router.get("/subscription")
async def subscription_status(access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    entitlement = await get_entitlements(db, access.workspace_id)
    row = await db.get(Subscription, access.workspace_id)
    month = month_key()
    usage = await db.scalar(select(MonthlyUsage).where(MonthlyUsage.workspace_id == access.workspace_id, MonthlyUsage.month == month))
    return {**entitlement, "status": row.status if row else "none",
            "current_period_end": row.current_period_end if row else None,
            "usage": {**(await resource_usage(db, access.workspace_id)), "month": month,
                      "completed_answers": usage.completed_answers if usage else 0,
                      "reserved_answers": usage.reserved_answers if usage else 0}}


@router.get("/plans")
async def plans_catalog(access: AccessContext = Depends(get_access)):
    return {"plans": [{"id": plan, "name": plan.capitalize(), "limits": dict(limits)}
                      for plan, limits in PLAN_LIMITS.items()]}


@router.post("/checkout")
async def checkout(payload: CheckoutInput, request: Request, access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    payment = configured_payment(request)
    settings = request.app.state.settings
    price = getattr(settings, "stripe_" + payload.plan + "_price_id")
    if not price or price_plans(settings).get(price) != payload.plan:
        raise HTTPException(503, "Requested plan is not configured")
    await lock_workspace(db, access.workspace_id)
    row = await db.get(Subscription, access.workspace_id)
    if row and row.subscription_id and row.status not in {"canceled", "incomplete_expired"}:
        raise HTTPException(409, "Manage the existing subscription in the billing portal")
    if row and row.checkout_url and row.checkout_expires_at and aware(row.checkout_expires_at) > utcnow():
        if row.pending_price_id != price:
            raise HTTPException(409, "Complete the pending checkout or wait for it to expire")
        return {"url": row.checkout_url}
    if row and row.checkout_url and row.checkout_session_id:
        try:
            binding = await known_checkout(db, row, row.checkout_session_id)
            previous = await payment.retrieve_checkout(binding.id)
            state = checkout_state(previous, binding)
            if state == "complete":
                subscription_id = stripe_id(previous.get("subscription"), "sub_")
                current = await payment.retrieve_subscription(subscription_id)
                values = checkout_subscription_values(current, row, binding, settings, subscription_id)
                apply_subscription(row, binding, values)
                if row.status not in {"canceled", "incomplete_expired"}:
                    await db.commit()
                    raise HTTPException(409, "Checkout already completed; manage the subscription in the billing portal")
            elif state != "expired":
                raise HTTPException(409, "The previous checkout is still open")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Cannot reconcile the previous checkout") from None
    try:
        if row is None:
            user = await db.get(User, access.user_id)
            customer = await payment.create_customer(workspace_id=access.workspace_id, email=user.email)
            row = Subscription(workspace_id=access.workspace_id, customer_id=stripe_id(customer.get("id"), "cus_"))
            db.add(row)
            await db.flush()
        # The previous session is a stable generation marker. If an upstream
        # timeout rolls this transaction back, the next request uses the same
        # Stripe idempotency key, rather than opening another payable session.
        key = hashlib.sha256(f"{access.workspace_id}:{price}:{row.checkout_session_id or 'first'}".encode()).hexdigest()
        result = await payment.create_checkout(workspace_id=access.workspace_id, customer_id=row.customer_id,
            price_id=price, success_url=settings.public_origin + "/?billing=success",
            cancel_url=settings.public_origin + "/?billing=canceled", idempotency_key="checkout-" + key)
        session_id = stripe_id(result.get("id"), "cs_")
        url = hosted_url(result.get("url"), "checkout.stripe.com")
        expires_at = result["expires_at"]
        if type(expires_at) is not int or not int(utcnow().timestamp()) < expires_at <= int(utcnow().timestamp()) + 86400:
            raise ValueError("Invalid checkout expiry")
    except Exception:
        raise HTTPException(502, "Payment provider is unavailable") from None
    row.checkout_session_id = session_id
    row.checkout_url = url
    row.checkout_expires_at = datetime.fromtimestamp(expires_at, timezone.utc)
    row.pending_price_id = price
    db.add(CheckoutSession(id=session_id, workspace_id=access.workspace_id, customer_id=row.customer_id, price_id=price))
    db.add(AuditEvent(workspace_id=access.workspace_id, user_id=access.user_id, action="billing.checkout_created", details={"plan": payload.plan}))
    await db.commit()
    return {"url": url}


@router.post("/portal")
async def portal(request: Request, access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    payment = configured_payment(request)
    row = await db.get(Subscription, access.workspace_id)
    if row is None:
        raise HTTPException(409, "No billing account exists")
    try:
        result = await payment.create_portal(customer_id=row.customer_id, return_url=request.app.state.settings.public_origin + "/?view=billing")
        url = hosted_url(result.get("url"), "billing.stripe.com")
    except Exception:
        raise HTTPException(502, "Payment provider is unavailable") from None
    return {"url": url}


def subscription_values(subscription, account, settings, subscription_id):
    if subscription.get("id") != subscription_id or subscription.get("customer") != account.customer_id or subscription.get("metadata", {}).get("workspace_id") != account.workspace_id:
        raise ValueError("Subscription binding mismatch")
    items = subscription["items"]["data"]
    if not isinstance(items, list) or len(items) != 1 or items[0].get("quantity") != 1:
        raise ValueError("Unsupported subscription items")
    price = items[0]["price"]["id"]
    plan = price_plans(settings).get(price)
    if plan is None:
        raise ValueError("Unrecognized subscription price")
    start = subscription.get("current_period_start", items[0].get("current_period_start"))
    end = subscription.get("current_period_end", items[0].get("current_period_end"))
    if type(start) is not int or type(end) is not int or start >= end:
        raise ValueError("Invalid subscription period")
    status = subscription["status"]
    if status not in {"active", "trialing", "past_due", "canceled", "unpaid", "incomplete", "incomplete_expired", "paused"}:
        raise ValueError("Unsupported subscription status")
    return {"subscription_id": subscription_id, "price_id": price, "plan": plan, "status": status,
            "current_period_start": datetime.fromtimestamp(start, timezone.utc),
            "current_period_end": datetime.fromtimestamp(end, timezone.utc)}


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    settings = request.app.state.settings
    raw = await request.body()
    if len(raw) > 1048576 or not verify_webhook_signature(raw, request.headers.get("Stripe-Signature", ""), settings.stripe_webhook_secret, tolerance=settings.stripe_webhook_tolerance_seconds):
        raise HTTPException(400, "Invalid webhook signature")
    try:
        event = json.loads(raw)
        event_id = stripe_id(event["id"], "evt_")
        created = event["created"]
        event_type = event["type"]
        if type(created) is not int or created < 1 or not isinstance(event_type, str) or len(event_type) > 128:
            raise ValueError("Invalid event")
        obj = event["data"]["object"]
        if not isinstance(obj, dict):
            raise ValueError("Invalid event object")
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid webhook payload") from None
    if await db.get(StripeEvent, event_id):
        return {"received": True, "duplicate": True}
    supported = {"checkout.session.completed", "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
    if event_type not in supported:
        return {"received": True, "ignored": True}
    try:
        customer_id = stripe_id(obj.get("customer"), "cus_")
        account = await db.scalar(select(Subscription).where(Subscription.customer_id == customer_id))
        if account is None:
            raise ValueError("Unknown customer")
        await lock_workspace(db, account.workspace_id)
        await db.refresh(account)
        # Check again after the workspace lock when two deliveries race on PG.
        if await db.get(StripeEvent, event_id):
            return {"received": True, "duplicate": True}
        if obj.get("metadata", {}).get("workspace_id") != account.workspace_id:
            raise ValueError("Organization mismatch")
        is_checkout = event_type == "checkout.session.completed"
        if is_checkout:
            binding = await known_checkout(db, account, stripe_id(obj.get("id"), "cs_"))
            if obj.get("client_reference_id") != account.workspace_id or obj.get("mode") != "subscription":
                raise ValueError("Checkout binding mismatch")
            subscription_id = stripe_id(obj.get("subscription"), "sub_")
            if account.subscription_id and account.subscription_id != subscription_id and account.status not in {"canceled", "incomplete_expired"}:
                raise ValueError("Subscription mismatch")
        else:
            subscription_id = stripe_id(obj.get("id"), "sub_")
            if account.subscription_id != subscription_id:
                raise ValueError("Subscription mismatch")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Webhook billing binding is invalid") from None
    if subscription_id == account.subscription_id and created < account.last_event_created:
        db.add(StripeEvent(id=event_id, workspace_id=account.workspace_id, event_type=event_type, created=created))
        await db.commit()
        return {"received": True, "ignored": True}
    payment = configured_payment(request)
    try:
        if is_checkout:
            current_session = await payment.retrieve_checkout(binding.id)
            if checkout_state(current_session, binding) != "complete" or current_session.get("subscription") != subscription_id:
                raise ValueError("Checkout is not completed")
        current = await payment.retrieve_subscription(subscription_id)
    except Exception:
        # No event receipt is committed; Stripe can retry the delivery.
        raise HTTPException(502, "Payment provider is unavailable") from None
    try:
        values = checkout_subscription_values(current, account, binding, settings, subscription_id) if is_checkout else subscription_values(current, account, settings, subscription_id)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, OSError):
        raise HTTPException(400, "Webhook subscription is invalid") from None
    if is_checkout and binding.id != account.checkout_session_id and account.checkout_url:
        # A late known completion must not leave a newer session payable.
        try:
            pending_binding = await known_checkout(db, account, account.checkout_session_id)
            pending = await payment.retrieve_checkout(pending_binding.id)
            state = checkout_state(pending, pending_binding)
            if state == "open":
                state = checkout_state(await payment.expire_checkout(pending_binding.id), pending_binding)
            if state != "expired":
                raise ValueError("Another checkout has completed")
        except Exception:
            raise HTTPException(502, "Cannot reconcile another pending checkout") from None
    apply_subscription(account, binding if is_checkout else None, values)
    account.last_event_created = created
    account.updated_at = utcnow()
    db.add(StripeEvent(id=event_id, workspace_id=account.workspace_id, event_type=event_type, created=created))
    db.add(AuditEvent(workspace_id=account.workspace_id, action="billing.subscription_updated", details={"plan": account.plan, "status": account.status}))
    await db.commit()
    return {"received": True}
