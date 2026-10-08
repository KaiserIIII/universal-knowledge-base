import asyncio
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict, deque
from datetime import timezone

from fastapi import HTTPException


ITERATIONS = 600000


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def session_csrf(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"knowledge-saas:csrf:v1", hashlib.sha256).hexdigest()


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_password, password)


def _verify_password(password, stored):
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != ITERATIONS:
            return False
        actual = _hash_password(password, salt).split("$")[-1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def verify_password(password: str, stored: str) -> bool:
    return await asyncio.to_thread(_verify_password, password, stored)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class AuthThrottle:
    """Bounded worker-local limit. A reverse proxy must add distributed limits."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.buckets = OrderedDict()

    def check(self, request, action):
        settings = request.app.state.settings
        now = self.clock()
        address = request.client.host if request.client else "unknown"
        key = (action, address)
        bucket = self.buckets.pop(key, deque())
        cutoff = now - settings.auth_attempt_window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        self.buckets[key] = bucket
        if len(bucket) >= settings.auth_attempt_limit:
            raise HTTPException(429, "Too many authentication attempts", headers={"Retry-After": str(settings.auth_attempt_window_seconds)})
        bucket.append(now)
        while len(self.buckets) > settings.auth_attempt_max_buckets:
            self.buckets.popitem(last=False)
