"""Isolated async API fixture; never reads checkout runtime data or .env."""
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class FakeRetriever:
    def __init__(self):
        self.results = []
        self.search_calls = []
        self.chunks = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.results

    async def parse(self, **kwargs):
        return self.chunks

    async def upsert(self, chunks):
        return len(chunks)

    async def delete_document(self, doc_id):
        return 0

    async def delete_kb(self, kb_id):
        return 0

    async def close(self):
        pass


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.content = "A supported answer."

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": self.content, "usage": {"total_tokens": 10}}

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        yield self.content


class FakePayment:
    def __init__(self):
        self.calls = []


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 1.0
        try:
            from app.saas.application import create_app
            from app.saas.config import AppSettings
        except ModuleNotFoundError:
            self.fail("SaaS application factory is not implemented")
        self.temporary = tempfile.TemporaryDirectory(prefix="knowledge-saas-tests-")
        self.addCleanup(self.temporary.cleanup)
        folder = Path(self.temporary.name)
        self.settings = AppSettings(
            _env_file=None,
            _env_prefix="KNOWLEDGE_TEST_ISOLATED_",
            database_url=f"sqlite+aiosqlite:///{(folder / 'test.db').as_posix()}",
            chroma_persist_dir=str(folder / "chroma"),
            upload_temp_dir=str(folder / "uploads"),
            cookie_secure=False,
            ingestion_worker_enabled=False,
        )
        self.retriever = FakeRetriever()
        self.llm = FakeLLM()
        self.payment = FakePayment()
        self.app = create_app(self.settings, self.retriever, self.llm, self.payment)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.clients = []
        self.client = await self.new_client()
        self.headers = {}

    async def new_client(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.clients.append(client)
        self.addAsyncCleanup(client.aclose)
        return client

    async def register_with(self, client, email, organization_name="Example Team", password="ExampleStrong123!"):
        response = await client.post("/api/v1/auth/register", json={
            "email": email, "password": password, "organization_name": organization_name,
        })
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        client.headers.update({"X-CSRF-Token": result["csrf_token"], "X-Workspace-ID": result["organizations"][0]["id"]})
        return result

    async def register(self, email, organization_name="Example Team", password="ExampleStrong123!"):
        result = await self.register_with(self.client, email, organization_name, password)
        self.headers = {"X-CSRF-Token": result["csrf_token"], "X-Workspace-ID": result["organizations"][0]["id"]}
        self.user = result["user"]
        self.workspace_id = result["organizations"][0]["id"]
        return result

    async def invite(self, email, role="viewer"):
        response = await self.client.post(f"/api/v1/organizations/{self.workspace_id}/invitations", json={"email": email, "role": role})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def seed_subscription(self, plan="team"):
        """Explicit paid fixture for identity tests that need multiple members."""
        from datetime import timedelta
        from app.saas.billing_models import Subscription
        from app.saas.models import utcnow
        price = "price_" + plan + "_test"
        setattr(self.settings, "stripe_" + plan + "_price_id", price)
        async with self.app.state.session_factory() as db:
            row = await db.get(Subscription, self.workspace_id)
            if row is None:
                row = Subscription(workspace_id=self.workspace_id, customer_id="cus_fixture_" + self.workspace_id)
                db.add(row)
            row.subscription_id = "sub_fixture_" + self.workspace_id
            row.plan, row.price_id, row.status = plan, price, "active"
            row.current_period_start = utcnow() - timedelta(days=1)
            row.current_period_end = utcnow() + timedelta(days=30)
            await db.commit()

    async def join(self, email, role="viewer", *, paid_fixture=False):
        if paid_fixture:
            await self.seed_subscription("team")
        invitation = await self.invite(email, role)
        client = await self.new_client()
        user = await self.register_with(client, email)
        response = await client.post("/api/v1/invitations/accept", json={"token": invitation["token"]})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-Workspace-ID"] = self.workspace_id
        return client, user
