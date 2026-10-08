"""Offline billing and real transactional quota behavior."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib
import json
import time
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select, text

from tests.support import ApiTestCase


class BillingTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.settings.stripe_secret_key = "sk_test_offline"
        self.settings.stripe_webhook_secret = "whsec_offline"
        self.settings.stripe_team_price_id = "price_team_test"
        self.settings.stripe_business_price_id = "price_business_test"
        self.payment = OfflinePayment()
        self.app.state.payment = self.payment

    def module(self, name):
        try:
            return importlib.import_module("app.saas." + name)
        except ModuleNotFoundError:
            self.fail(name + " is not implemented")

    async def transaction(self, function, *args, **kwargs):
        async with self.app.state.session_factory() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            result = await function(db, self.workspace_id, *args, **kwargs)
            await db.commit()
            return result

    async def status(self):
        response = await self.client.get("/api/v1/billing/subscription")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def checkout(self, plan="team"):
        response = await self.client.post("/api/v1/billing/checkout", json={"plan": plan})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def subscription(self, **changes):
        now = int(time.time())
        result = {"id": "sub_offline", "object": "subscription", "customer": "cus_offline",
                  "metadata": {"workspace_id": self.workspace_id}, "status": "active",
                  "current_period_start": now - 60, "current_period_end": now + 86400,
                  "items": {"data": [{"price": {"id": "price_team_test"}, "quantity": 1,
                                      "current_period_start": now - 60, "current_period_end": now + 86400}]}}
        result.update(changes)
        return result

    async def event(self, subscription=None, *, event_id=None, created=None, event_type="checkout.session.completed", changes=None):
        subscription = subscription or self.subscription()
        self.payment.subscriptions[subscription["id"]] = subscription
        obj = subscription if event_type.startswith("customer.subscription.") else {
            "id": self.payment.checkout_id, "object": "checkout.session", "mode": "subscription",
            "customer": "cus_offline", "subscription": subscription["id"],
            "client_reference_id": self.workspace_id, "metadata": {"workspace_id": self.workspace_id},
        }
        if changes:
            obj = {**obj, **changes}
        if event_type == "checkout.session.completed" and obj["id"] in self.payment.sessions:
            self.payment.sessions[obj["id"]].update(status="complete", subscription=subscription["id"])
        payload = {"id": event_id or "evt_" + uuid4().hex, "created": created or int(time.time()),
                   "type": event_type, "data": {"object": obj}}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = int(time.time())
        digest = hmac.new(b"whsec_offline", str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
        return await self.client.post("/api/v1/billing/webhook", content=raw,
                                      headers={"Stripe-Signature": f"t={timestamp},v1={digest}"})

    async def activate(self, **changes):
        await self.checkout()
        response = await self.event(self.subscription(**changes))
        self.assertEqual(response.status_code, 200, response.text)

    async def test_free_status_reports_limits_and_usage(self):
        await self.register("free@example.test")
        result = await self.status()
        self.assertEqual(result["plan"], "free")
        self.assertEqual(result["limits"], {"members": 1, "knowledge_bases": 3, "documents": 50, "monthly_answers": 100})
        self.assertEqual(result["usage"]["completed_answers"], 0)
        self.assertNotIn("sk_test", str(result))

    async def test_authenticated_plans_catalog_matches_current_tenant_and_never_exposes_prices(self):
        anonymous = await self.new_client()
        self.assertEqual((await anonymous.get("/api/v1/billing/plans")).status_code, 401)
        await self.register("plans@example.test")
        response = await self.client.get("/api/v1/billing/plans")
        self.assertEqual(response.status_code, 200, response.text)
        plans = response.json()["plans"]
        self.assertEqual([plan["id"] for plan in plans], ["free", "team", "business"])
        self.assertEqual(plans[0]["limits"], (await self.status())["limits"])
        self.assertEqual([plan["limits"]["monthly_answers"] for plan in plans], [100, 5000, 50000])
        self.assertNotIn("price_team_test", response.text)
        self.assertNotIn("sk_test", response.text)

    async def test_free_member_quota_is_enforced_on_acceptance(self):
        await self.register("free-owner@example.test")
        response = await self.client.post(f"/api/v1/organizations/{self.workspace_id}/invitations", json={"email": "free-invitee@example.test", "role": "viewer"})
        invited = await self.new_client()
        await self.register_with(invited, "free-invitee@example.test")
        rejected = await invited.post("/api/v1/invitations/accept", json={"token": response.json()["token"]})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        members = await self.client.get(f"/api/v1/organizations/{self.workspace_id}/members")
        self.assertEqual(len(members.json()["members"]), 1)

    async def test_only_valid_paid_periods_receive_entitlements(self):
        await self.register("entitlements@example.test")
        await self.activate()
        result = await self.status()
        self.assertEqual(result["plan"], "team")
        self.assertEqual(result["limits"]["monthly_answers"], 5000)
        for status in ("past_due", "canceled", "unpaid", "incomplete"):
            response = await self.event(self.subscription(status=status), event_type="customer.subscription.updated")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((await self.status())["plan"], "free", status)
        expired = self.subscription(current_period_end=int(time.time()) - 1)
        self.assertEqual((await self.event(expired, event_type="customer.subscription.updated")).status_code, 200)
        self.assertEqual((await self.status())["plan"], "free")
        business = self.subscription(status="trialing", items={"data": [{"price": {"id": "price_business_test"}, "quantity": 1}]})
        self.assertEqual((await self.event(business, event_type="customer.subscription.updated")).status_code, 200)
        self.assertEqual((await self.status())["limits"], {"members": 50, "knowledge_bases": 100, "documents": 10000, "monthly_answers": 50000})
        self.settings.stripe_business_price_id = "price_rotated"
        self.assertEqual((await self.status())["plan"], "free")

    async def test_resource_quotas_count_only_live_tenant_resources(self):
        from app.models import Document, KnowledgeBase, Workspace
        await self.register("resources@example.test")
        quotas = self.module("quotas")
        async with self.app.state.session_factory() as db:
            foreign = Workspace(name="Foreign")
            db.add(foreign)
            await db.flush()
            db.add(KnowledgeBase(workspace_id=foreign.id, name="Foreign KB"))
            db.add(KnowledgeBase(workspace_id=self.workspace_id, name="Deleted", is_deleted=True))
            live = [KnowledgeBase(workspace_id=self.workspace_id, name=f"KB {i}") for i in range(3)]
            db.add_all(live)
            await db.flush()
            for i in range(50):
                db.add(Document(kb_id=live[0].id, filename=f"{i}.txt", file_type="txt", file_hash=f"{i:064x}"))
            await db.commit()
        for resource in ("knowledge_bases", "documents"):
            with self.assertRaises(HTTPException) as error:
                await self.transaction(quotas.check_resource_quota, resource)
            self.assertEqual(error.exception.status_code, 409)
        await self.activate()
        await self.transaction(quotas.check_resource_quota, "knowledge_bases", increment=17)
        with self.assertRaises(HTTPException):
            await self.transaction(quotas.check_resource_quota, "knowledge_bases", increment=18)

    async def test_answer_reservations_release_failures_and_finalize_once(self):
        await self.register("usage@example.test")
        quotas = self.module("quotas")
        reservation_id = str(uuid4())
        first = await self.transaction(quotas.reserve_answer, reservation_id)
        second = await self.transaction(quotas.reserve_answer, reservation_id)
        self.assertEqual(first.id, second.id)
        self.assertEqual((await self.status())["usage"]["reserved_answers"], 1)
        await self.transaction(quotas.finish_answer, reservation_id, succeeded=False)
        await self.transaction(quotas.finish_answer, reservation_id, succeeded=True)
        self.assertEqual((await self.status())["usage"]["completed_answers"], 0)
        success = await self.transaction(quotas.reserve_answer)
        await self.transaction(quotas.finish_answer, success.id, succeeded=True)
        await self.transaction(quotas.finish_answer, success.id, succeeded=True)
        usage = (await self.status())["usage"]
        self.assertEqual((usage["completed_answers"], usage["reserved_answers"]), (1, 0))

    async def test_concurrent_answer_reservations_never_exceed_limit(self):
        await self.register("concurrent@example.test")
        quotas = self.module("quotas")
        models = self.module("billing_models")
        now = datetime.now(timezone.utc)
        async with self.app.state.session_factory() as db:
            db.add(models.MonthlyUsage(workspace_id=self.workspace_id, month=now.strftime("%Y-%m"), completed_answers=99, reserved_answers=0))
            await db.commit()
        results = await asyncio.gather(self.transaction(quotas.reserve_answer), self.transaction(quotas.reserve_answer), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results), 1)
        self.assertEqual((await self.status())["usage"]["reserved_answers"], 1)

    async def test_monthly_rollover_finishes_original_month_and_hides_other_tenant_ids(self):
        await self.register("rollover@example.test")
        quotas = self.module("quotas")
        models = self.module("billing_models")
        first = await self.transaction(quotas.reserve_answer, now=datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc))
        await self.transaction(quotas.reserve_answer, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        await self.transaction(quotas.finish_answer, first.id, succeeded=True)
        async with self.app.state.session_factory() as db:
            rows = (await db.scalars(select(models.MonthlyUsage).order_by(models.MonthlyUsage.month))).all()
        self.assertEqual([(row.month, row.completed_answers, row.reserved_answers) for row in rows], [("2026-01", 1, 0), ("2026-02", 0, 1)])
        original = self.workspace_id
        other = await self.new_client()
        foreign = await self.register_with(other, "foreign-reservation@example.test")
        self.workspace_id = foreign["organizations"][0]["id"]
        with self.assertRaises(HTTPException) as error:
            await self.transaction(quotas.finish_answer, first.id, succeeded=True)
        self.assertEqual(error.exception.status_code, 404)
        self.workspace_id = original

    async def test_checkout_server_owned_fields_and_portal_binding(self):
        await self.register("checkout@example.test")
        rejected = await self.client.post("/api/v1/billing/checkout", json={"plan": "team", "price_id": "price_attacker", "success_url": "https://attacker.test", "customer_id": "cus_attacker"})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual((await self.client.post("/api/v1/billing/portal")).status_code, 409)
        result = await self.checkout()
        self.assertEqual(result["url"], "https://checkout.stripe.com/c/pay/offline")
        checkout = self.payment.calls[-1]
        self.assertEqual(checkout["workspace_id"], self.workspace_id)
        self.assertEqual(checkout["price_id"], "price_team_test")
        self.assertEqual(checkout["customer_id"], "cus_offline")
        self.assertEqual(checkout["success_url"], "http://localhost:8000/?billing=success")
        self.assertEqual(checkout["cancel_url"], "http://localhost:8000/?billing=canceled")
        self.assertEqual((await self.status())["plan"], "free")
        portal = await self.client.post("/api/v1/billing/portal")
        self.assertEqual(portal.status_code, 200, portal.text)
        self.assertEqual(self.payment.calls[-1]["return_url"], "http://localhost:8000/?view=billing")

    async def test_billing_roles_and_cross_tenant_access(self):
        await self.register("billing-owner@example.test")
        viewer, _ = await self.join("billing-viewer@example.test", paid_fixture=True)
        self.assertEqual((await viewer.get("/api/v1/billing/subscription")).status_code, 200)
        for route in ("checkout", "portal"):
            response = await viewer.post("/api/v1/billing/" + route, json={"plan": "team"} if route == "checkout" else None)
            self.assertEqual(response.status_code, 403)
        other = await self.new_client()
        await self.register_with(other, "billing-foreign@example.test")
        other.headers["X-Workspace-ID"] = self.workspace_id
        self.assertEqual((await other.get("/api/v1/billing/subscription")).status_code, 404)
        self.assertEqual((await other.post("/api/v1/billing/checkout", json={"plan": "team"})).status_code, 404)

    async def test_invalid_signature_never_changes_subscription(self):
        await self.register("signature@example.test")
        response = await self.client.post("/api/v1/billing/webhook", content=b'{"id":"evt_fake"}', headers={"Stripe-Signature": "t=1,v1=bad"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.status())["plan"], "free")

    async def test_signature_checks_exact_body_age_and_multiple_signatures(self):
        payments = self.module("payments")
        now = 1800000000
        raw = b'{"id":"evt_test"}'
        digest = hmac.new(b"secret", str(now).encode() + b"." + raw, hashlib.sha256).hexdigest()
        self.assertTrue(payments.verify_webhook_signature(raw, f"t={now},v1=bad,v1={digest}", "secret", now=now))
        for body, timestamp in ((raw + b" ", now), (raw, now + 301), (raw, now - 301)):
            self.assertFalse(payments.verify_webhook_signature(body, f"t={now},v1={digest}", "secret", now=timestamp))
        self.assertFalse(payments.verify_webhook_signature(raw, f"t={now},t={now},v1={digest}", "secret", now=now))

    async def test_webhook_replays_and_older_events_cannot_restore_paid_state(self):
        await self.register("replay@example.test")
        await self.checkout()
        created = int(time.time())
        first = await self.event(event_id="evt_repeat", created=created)
        self.assertEqual(first.status_code, 200, first.text)
        canceled = self.subscription(status="canceled")
        self.assertEqual((await self.event(canceled, created=created + 2, event_type="customer.subscription.deleted")).status_code, 200)
        self.assertEqual((await self.event(event_id="evt_repeat", created=created)).status_code, 200)
        self.assertEqual((await self.event(created=created + 1, event_type="customer.subscription.updated")).status_code, 200)
        self.assertEqual((await self.status())["plan"], "free")

    async def test_authoritative_subscription_wins_over_stale_event_snapshot(self):
        await self.register("authoritative@example.test")
        await self.activate()
        snapshot = self.subscription()
        authoritative = self.subscription(status="past_due")
        self.payment.forced_subscription = authoritative
        response = await self.event(snapshot, event_type="customer.subscription.updated")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.status())["plan"], "free")

    async def test_webhook_rejects_wrong_customer_subscription_metadata_and_price(self):
        await self.register("binding@example.test")
        await self.checkout()
        for changes in ({"customer": "cus_wrong"}, {"id": "cs_wrong"}, {"metadata": {"workspace_id": str(uuid4())}}, {"client_reference_id": str(uuid4())}):
            response = await self.event(changes=changes)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual((await self.status())["plan"], "free")
        unknown = self.subscription(items={"data": [{"price": {"id": "price_unknown"}, "quantity": 1}]})
        self.assertEqual((await self.event(unknown)).status_code, 400)
        self.assertEqual((await self.event()).status_code, 200)
        for subscription in (self.subscription(id="sub_wrong"), self.subscription(customer="cus_wrong"), self.subscription(metadata={"workspace_id": str(uuid4())})):
            self.assertEqual((await self.event(subscription, event_type="customer.subscription.updated")).status_code, 400)
        self.assertEqual((await self.status())["plan"], "team")

    async def test_payment_failures_are_sanitized_and_missing_config_fails_closed(self):
        await self.register("disabled@example.test")
        self.settings.stripe_team_price_id = None
        response = await self.client.post("/api/v1/billing/checkout", json={"plan": "team"})
        self.assertEqual(response.status_code, 503)
        self.settings.stripe_team_price_id = "price_team_test"
        self.payment.error = RuntimeError("sk_live_never_return_this")
        response = await self.client.post("/api/v1/billing/checkout", json={"plan": "team"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sk_live", response.text)

    async def test_pending_checkout_is_reused_and_different_plan_waits_for_expiry(self):
        await self.register("checkout-repeat@example.test")
        first = await self.checkout()
        second = await self.checkout()
        self.assertEqual(first, second)
        self.assertEqual(sum(call["method"] == "checkout" for call in self.payment.calls), 1)
        different = await self.client.post("/api/v1/billing/checkout", json={"plan": "business"})
        self.assertEqual(different.status_code, 409)
        self.assertEqual((await self.event()).status_code, 200)
        self.assertEqual((await self.client.post("/api/v1/billing/checkout", json={"plan": "team"})).status_code, 409)

    async def expire_local_checkout(self):
        from app.saas.billing_models import Subscription
        async with self.app.state.session_factory() as db:
            row = await db.get(Subscription, self.workspace_id)
            row.checkout_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

    async def test_completed_checkout_is_reconciled_before_expired_local_session_replacement(self):
        await self.register("delayed-paid@example.test")
        await self.checkout()
        session_a = self.payment.checkout_id
        subscription = self.subscription()
        self.payment.subscriptions[subscription["id"]] = subscription
        # Payment completed while A was payable; webhook delivery is delayed.
        self.payment.sessions[session_a].update(status="complete", subscription=subscription["id"])
        await self.expire_local_checkout()
        replacement = await self.client.post("/api/v1/billing/checkout", json={"plan": "team"})
        self.assertEqual(replacement.status_code, 409, replacement.text)
        self.assertEqual(sum(call["method"] == "checkout" for call in self.payment.calls), 1)
        self.assertEqual((await self.status())["plan"], "team")
        delayed = await self.event(subscription, changes={"id": session_a}, event_id="evt_delayed_a")
        self.assertEqual(delayed.status_code, 200, delayed.text)
        self.assertEqual((await self.status())["plan"], "team")

    async def test_known_expired_session_binding_survives_replacement_for_delayed_completion(self):
        await self.register("known-delayed@example.test")
        await self.checkout()
        session_a = self.payment.checkout_id
        self.payment.sessions[session_a]["status"] = "expired"
        await self.expire_local_checkout()
        await self.checkout()
        session_b = self.payment.checkout_id
        self.assertNotEqual(session_a, session_b)
        self.assertEqual(sum(call["method"] == "checkout" for call in self.payment.calls), 2)
        delayed = await self.event(changes={"id": session_a}, event_id="evt_late_known")
        self.assertEqual(delayed.status_code, 200, delayed.text)
        self.assertEqual((await self.status())["plan"], "team")
        self.assertEqual(self.payment.sessions[session_b]["status"], "expired")
        self.assertEqual((await self.event(changes={"id": "cs_unknown"})).status_code, 400)
        self.assertEqual((await self.event(changes={"id": session_a, "metadata": {"workspace_id": str(uuid4())}})).status_code, 400)
        self.assertEqual((await self.event(changes={"id": session_a}, event_id="evt_late_known")).status_code, 200)

    async def test_expired_checkout_reconciliation_provider_failure_blocks_replacement(self):
        await self.register("failed-reconcile@example.test")
        await self.checkout()
        await self.expire_local_checkout()
        self.payment.error = RuntimeError("private_failure")
        response = await self.client.post("/api/v1/billing/checkout", json={"plan": "team"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private_failure", response.text)
        self.assertEqual(sum(call["method"] == "checkout" for call in self.payment.calls), 1)

    async def test_failed_webhook_retries_without_marking_event_processed(self):
        await self.register("retry-webhook@example.test")
        await self.checkout()
        self.payment.error = RuntimeError("secret_provider_detail")
        failed = await self.event(event_id="evt_retry")
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("secret_provider", failed.text)
        self.assertEqual((await self.status())["plan"], "free")
        self.payment.error = None
        self.assertEqual((await self.event(event_id="evt_retry")).status_code, 200)
        self.assertEqual((await self.status())["plan"], "team")

    async def test_canceled_subscription_can_start_a_fresh_checkout(self):
        await self.register("replace-subscription@example.test")
        await self.activate()
        created = int(time.time())
        canceled = self.subscription(status="canceled")
        self.assertEqual((await self.event(canceled, created=created + 2, event_type="customer.subscription.deleted")).status_code, 200)
        await self.checkout()
        self.assertEqual(sum(call["method"] == "checkout" for call in self.payment.calls), 2)
        self.assertEqual((await self.event(self.subscription(id="sub_new"), created=created + 1)).status_code, 200)
        self.assertEqual((await self.status())["plan"], "team")
        self.assertEqual((await self.event(canceled, created=created + 3, event_type="customer.subscription.deleted")).status_code, 400)

    async def test_current_stripe_item_periods_and_future_period_fallback(self):
        await self.register("item-period@example.test")
        await self.checkout()
        item_only = self.subscription()
        del item_only["current_period_start"]
        del item_only["current_period_end"]
        self.assertEqual((await self.event(item_only)).status_code, 200)
        self.assertEqual((await self.status())["plan"], "team")
        future = self.subscription(current_period_start=int(time.time()) + 300)
        self.assertEqual((await self.event(future, event_type="customer.subscription.updated")).status_code, 200)
        self.assertEqual((await self.status())["plan"], "free")

    async def test_concurrent_finish_charges_one_and_concurrent_member_acceptance_respects_team_limit(self):
        from app.saas.models import Membership, User
        await self.register("finish-concurrent@example.test")
        quotas = self.module("quotas")
        reservation = await self.transaction(quotas.reserve_answer)
        await asyncio.gather(self.transaction(quotas.finish_answer, reservation.id, succeeded=True), self.transaction(quotas.finish_answer, reservation.id, succeeded=True))
        self.assertEqual((await self.status())["usage"]["completed_answers"], 1)
        await self.activate()
        async with self.app.state.session_factory() as db:
            for index in range(8):
                user = User(email=f"existing-{index}@example.test", password_hash="unused")
                db.add(user)
                await db.flush()
                db.add(Membership(workspace_id=self.workspace_id, user_id=user.id, role="viewer"))
            await db.commit()
        invitees = []
        for index in range(2):
            email = f"race-invite-{index}@example.test"
            invite = await self.invite(email)
            client = await self.new_client()
            await self.register_with(client, email)
            invitees.append((client, invite["token"]))
        results = await asyncio.gather(*(client.post("/api/v1/invitations/accept", json={"token": token}) for client, token in invitees))
        self.assertEqual(sorted(result.status_code for result in results), [200, 409])


class OfflinePayment:
    def __init__(self):
        self.calls = []
        self.subscriptions = {}
        self.forced_subscription = None
        self.error = None
        self.checkout_id = "cs_offline"
        self.sessions = {}

    async def create_customer(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append({"method": "customer", **kwargs})
        return {"id": "cus_offline"}

    async def create_checkout(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append({"method": "checkout", **kwargs})
        number = sum(call["method"] == "checkout" for call in self.calls)
        self.checkout_id = "cs_offline" if number == 1 else f"cs_offline_{number}"
        self.sessions[self.checkout_id] = {"id": self.checkout_id, "customer": kwargs["customer_id"],
            "client_reference_id": kwargs["workspace_id"], "metadata": {"workspace_id": kwargs["workspace_id"]},
            "mode": "subscription", "status": "open", "subscription": None}
        return {"id": self.checkout_id, "url": "https://checkout.stripe.com/c/pay/offline", "expires_at": int(time.time()) + 86400}

    async def retrieve_checkout(self, session_id):
        if self.error:
            raise self.error
        return dict(self.sessions[session_id])

    async def expire_checkout(self, session_id):
        if self.error:
            raise self.error
        if self.sessions[session_id]["status"] != "open":
            raise RuntimeError("Session is not open")
        self.sessions[session_id]["status"] = "expired"
        return dict(self.sessions[session_id])

    async def create_portal(self, **kwargs):
        self.calls.append({"method": "portal", **kwargs})
        return {"url": "https://billing.stripe.com/p/session/offline"}

    async def retrieve_subscription(self, subscription_id):
        if self.error:
            raise self.error
        return self.forced_subscription or self.subscriptions[subscription_id]


class PaymentAdapterTests(ApiTestCase):
    async def test_stripe_adapter_uses_fixed_host_form_metadata_and_no_secret_response(self):
        try:
            from app.saas.payments import StripePayment
        except ModuleNotFoundError:
            self.fail("Stripe payment adapter is not implemented")
        self.settings.stripe_secret_key = "sk_test_local"
        requests = []
        def handle(request):
            requests.append(request)
            if request.url.path.endswith("customers"):
                return httpx.Response(200, json={"id": "cus_test"})
            if request.url.path.endswith("checkout/sessions"):
                return httpx.Response(200, json={"id": "cs_test", "url": "https://checkout.stripe.com/c/pay/test"})
            if request.url.path.endswith("billing_portal/sessions"):
                return httpx.Response(200, json={"url": "https://billing.stripe.com/p/session/test"})
            return httpx.Response(200, json={"id": "sub_test"})
        payment = StripePayment(self.settings, transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(payment.close)
        await payment.create_customer(workspace_id="organization", email="owner@example.test")
        await payment.create_checkout(workspace_id="organization", customer_id="cus_test", price_id="price_server", success_url="https://app.test/success", cancel_url="https://app.test/cancel", idempotency_key="checkout_123")
        await payment.create_portal(customer_id="cus_test", return_url="https://app.test/")
        await payment.retrieve_subscription("sub_test")
        await payment.retrieve_checkout("cs_test")
        await payment.expire_checkout("cs_test")
        self.assertEqual([str(request.url) for request in requests], ["https://api.stripe.com/v1/customers", "https://api.stripe.com/v1/checkout/sessions", "https://api.stripe.com/v1/billing_portal/sessions", "https://api.stripe.com/v1/subscriptions/sub_test", "https://api.stripe.com/v1/checkout/sessions/cs_test", "https://api.stripe.com/v1/checkout/sessions/cs_test/expire"])
        from urllib.parse import parse_qs
        form = parse_qs(requests[1].content.decode())
        self.assertEqual(form["subscription_data[metadata][workspace_id]"], ["organization"])
        self.assertEqual(form["metadata[workspace_id]"], ["organization"])
        self.assertEqual(form["line_items[0][price]"], ["price_server"])
        self.assertEqual(requests[1].headers["idempotency-key"], "checkout_123")
        self.assertTrue(all(request.headers["authorization"] == "Bearer sk_test_local" for request in requests))
