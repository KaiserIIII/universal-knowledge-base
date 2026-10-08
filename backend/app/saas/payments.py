"""Small Stripe HTTP adapter; credentials remain server-side.

No SDK, retries, user-defined endpoint, or network call at construction time.
"""
import hashlib
import hmac
import re
import time

import httpx


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str | None, *, tolerance: int = 300, now: int | None = None) -> bool:
    if not secret or not signature or len(signature) > 8192:
        return False
    try:
        pairs = [item.strip().split("=", 1) for item in signature.split(",")]
        timestamps = [value for key, value in pairs if key == "t"]
        signatures = [value for key, value in pairs if key == "v1"]
        if len(timestamps) != 1 or not re.fullmatch(r"[0-9]{1,12}", timestamps[0]):
            return False
        timestamp = int(timestamps[0])
        if abs((int(time.time()) if now is None else now) - timestamp) > tolerance:
            return False
        expected = hmac.new(secret.encode(), timestamps[0].encode() + b"." + raw_body, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, supplied) for supplied in signatures)
    except (ValueError, TypeError, UnicodeError):
        return False


class StripePayment:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url="https://api.stripe.com/v1/", timeout=15.0, follow_redirects=False, transport=transport)

    async def _request(self, method, path, *, data=None, idempotency_key=None):
        if not self.settings.stripe_secret_key:
            raise RuntimeError("Billing is not configured")
        headers = {"Authorization": "Bearer " + self.settings.stripe_secret_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self.client.request(method, path, data=data, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid payment response")
        return payload

    async def create_customer(self, *, workspace_id, email):
        return await self._request("POST", "customers", data={"email": email, "metadata[workspace_id]": workspace_id}, idempotency_key="customer-" + workspace_id)

    async def create_checkout(self, *, workspace_id, customer_id, price_id, success_url, cancel_url, idempotency_key):
        return await self._request("POST", "checkout/sessions", data={
            "mode": "subscription", "customer": customer_id, "client_reference_id": workspace_id,
            "metadata[workspace_id]": workspace_id, "subscription_data[metadata][workspace_id]": workspace_id,
            "line_items[0][price]": price_id, "line_items[0][quantity]": "1",
            "success_url": success_url, "cancel_url": cancel_url,
        }, idempotency_key=idempotency_key)

    async def create_portal(self, *, customer_id, return_url):
        return await self._request("POST", "billing_portal/sessions", data={"customer": customer_id, "return_url": return_url})

    async def retrieve_subscription(self, subscription_id):
        if not re.fullmatch(r"sub_[A-Za-z0-9_]+", subscription_id):
            raise ValueError("Invalid subscription ID")
        return await self._request("GET", "subscriptions/" + subscription_id)

    async def retrieve_checkout(self, session_id):
        if not re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id):
            raise ValueError("Invalid checkout ID")
        return await self._request("GET", "checkout/sessions/" + session_id)

    async def expire_checkout(self, session_id):
        if not re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id):
            raise ValueError("Invalid checkout ID")
        return await self._request("POST", "checkout/sessions/" + session_id + "/expire", idempotency_key="expire-" + session_id)

    async def close(self):
        await self.client.aclose()
