import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from tests.support import ApiTestCase


class IdentityTests(ApiTestCase):
    async def test_register_login_logout_and_me(self):
        registered = await self.register("Owner@EXAMPLE.test")
        self.assertEqual(registered["user"]["email"], "owner@example.test")
        self.assertEqual(registered["organizations"][0]["role"], "owner")
        self.assertNotIn("password", registered["user"])
        me = await self.client.get("/api/v1/auth/me")
        self.assertEqual(me.json()["user"]["id"], registered["user"]["id"])
        self.assertEqual(me.json()["csrf_token"], registered["csrf_token"])
        self.assertEqual((await self.client.post("/api/v1/auth/logout")).status_code, 204)
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        wrong = await self.client.post("/api/v1/auth/login", json={"email": "owner@example.test", "password": "wrong"})
        self.assertEqual(wrong.status_code, 401)
        login = await self.client.post("/api/v1/auth/login", json={"email": "owner@example.test", "password": "ExampleStrong123!"})
        self.assertEqual(login.status_code, 200)

    async def test_new_account_cannot_read_other_organization(self):
        first = await self.register("first@example.test")
        foreign_id = first["organizations"][0]["id"]
        second_client = await self.new_client()
        await self.register_with(second_client, "second@example.test")
        response = await second_client.get(f"/api/v1/organizations/{foreign_id}")
        self.assertEqual(response.status_code, 404)

    async def test_legacy_workspace_has_no_new_membership(self):
        from app.models import Workspace
        async with self.app.state.session_factory() as db:
            legacy = Workspace(name="Unclaimed legacy organization")
            db.add(legacy)
            await db.commit()
            legacy_id = legacy.id
        await self.register("new@example.test")
        response = await self.client.get("/api/v1/organizations")
        self.assertNotIn(legacy_id, [item["id"] for item in response.json()["organizations"]])
        self.assertEqual((await self.client.get(f"/api/v1/organizations/{legacy_id}")).status_code, 404)

    async def test_cookie_writes_require_csrf(self):
        await self.register("csrf@example.test")
        self.client.headers.pop("X-CSRF-Token")
        response = await self.client.post("/api/v1/auth/logout")
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 200)
        self.client.headers["X-CSRF-Token"] = "forged"
        self.assertEqual((await self.client.post("/api/v1/auth/logout")).status_code, 403)

    async def test_sessions_expire_and_revoke_immediately(self):
        from app.saas.models import Session
        registered = await self.register("session@example.test")
        old_token = self.client.cookies.get(self.settings.session_cookie_name)
        sessions = (await self.client.get("/api/v1/auth/sessions")).json()["sessions"]
        response = await self.client.delete(f"/api/v1/auth/sessions/{sessions[0]['id']}")
        self.assertEqual(response.status_code, 204)
        self.client.cookies.set(self.settings.session_cookie_name, old_token)
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        await self.client.post("/api/v1/auth/login", json={"email": registered["user"]["email"], "password": "ExampleStrong123!"})
        async with self.app.state.session_factory() as db:
            rows = (await db.scalars(select(Session))).all()
            for row in rows:
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)

    async def test_invitation_acceptance_is_one_time_email_bound(self):
        await self.register("owner@example.test")
        await self.seed_subscription("team")
        invite = await self.invite("invited@example.test", "editor")
        other = await self.new_client()
        await self.register_with(other, "other@example.test")
        self.assertEqual((await other.post("/api/v1/invitations/accept", json={"token": invite["token"]})).status_code, 404)
        invited = await self.new_client()
        await self.register_with(invited, "invited@example.test")
        accepted = await invited.post("/api/v1/invitations/accept", json={"token": invite["token"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["organization"]["role"], "editor")
        self.assertEqual((await invited.post("/api/v1/invitations/accept", json={"token": invite["token"]})).status_code, 404)
        listed = (await self.client.get(f"/api/v1/organizations/{self.workspace_id}/invitations")).json()
        self.assertNotIn("token", str(listed))

    async def test_expired_and_revoked_invites_cannot_be_accepted(self):
        from app.saas.models import Invitation
        await self.register("invite-owner@example.test")
        invite = await self.invite("invitee@example.test")
        async with self.app.state.session_factory() as db:
            row = await db.get(Invitation, invite["id"])
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        invited = await self.new_client()
        await self.register_with(invited, "invitee@example.test")
        self.assertEqual((await invited.post("/api/v1/invitations/accept", json={"token": invite["token"]})).status_code, 404)
        another = await self.invite("invitee@example.test")
        self.assertEqual((await self.client.delete(f"/api/v1/organizations/{self.workspace_id}/invitations/{another['id']}")).status_code, 204)
        self.assertEqual((await invited.post("/api/v1/invitations/accept", json={"token": another["token"]})).status_code, 404)

    async def test_role_changes_member_removal_and_last_owner_guard(self):
        await self.register("roles-owner@example.test")
        viewer, user = await self.join("viewer@example.test", paid_fixture=True)
        path = f"/api/v1/organizations/{self.workspace_id}/members/{user['user']['id']}"
        self.assertEqual((await viewer.patch(path, json={"role": "admin"})).status_code, 403)
        self.assertEqual((await self.client.patch(path, json={"role": "admin"})).status_code, 200)
        own = f"/api/v1/organizations/{self.workspace_id}/members/{self.user['id']}"
        self.assertEqual((await self.client.patch(own, json={"role": "viewer"})).status_code, 409)
        self.assertEqual((await self.client.delete(own)).status_code, 409)
        self.assertEqual((await viewer.patch(own, json={"role": "viewer"})).status_code, 403)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        self.assertEqual((await viewer.get(f"/api/v1/organizations/{self.workspace_id}")).status_code, 404)

    async def test_api_key_secret_once_revoke_and_membership_checks(self):
        await self.register("key-owner@example.test")
        created = await self.client.post("/api/v1/api-keys", json={"name": "integration"})
        self.assertEqual(created.status_code, 201, created.text)
        key = created.json()
        listed = (await self.client.get("/api/v1/api-keys")).json()
        self.assertNotIn(key["key"], str(listed))
        api_client = await self.new_client()
        api_client.headers.update({"Authorization": f"Bearer {key['key']}", "X-Workspace-ID": self.workspace_id})
        self.assertEqual((await api_client.get(f"/api/v1/organizations/{self.workspace_id}")).status_code, 200)
        self.assertEqual((await api_client.post("/api/v1/api-keys", json={"name": "second"})).status_code, 201)
        self.assertEqual((await self.client.delete(f"/api/v1/api-keys/{key['id']}")).status_code, 204)
        self.assertEqual((await api_client.get("/api/v1/auth/me")).status_code, 401)

    async def test_create_update_organization_and_duplicate_invitation(self):
        await self.register("multiple@example.test")
        created = await self.client.post("/api/v1/organizations", json={"name": "Second team", "description": "Second"})
        self.assertEqual(created.status_code, 201, created.text)
        organization_id = created.json()["id"]
        self.assertEqual(created.json()["role"], "owner")
        updated = await self.client.patch(f"/api/v1/organizations/{organization_id}", json={"name": "Renamed team"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "Renamed team")
        self.assertEqual(len((await self.client.get("/api/v1/organizations")).json()["organizations"]), 2)
        await self.invite("duplicate@example.test")
        duplicate = await self.client.post(f"/api/v1/organizations/{self.workspace_id}/invitations", json={"email": "duplicate@example.test", "role": "viewer"})
        self.assertEqual(duplicate.status_code, 409)

    async def test_registration_validation_disabled_and_throttling(self):
        self.app.state.settings.registration_enabled = False
        response = await self.client.post("/api/v1/auth/register", json={"email": "blocked@example.test", "password": "ExampleStrong123!", "organization_name": "Blocked"})
        self.assertEqual(response.status_code, 403)
        self.app.state.settings.registration_enabled = True
        invalid = await self.client.post("/api/v1/auth/register", json={"email": "invalid", "password": "short", "organization_name": ""})
        self.assertEqual(invalid.status_code, 422)
        self.app.state.settings.auth_attempt_limit = 2
        for _ in range(2):
            self.assertEqual((await self.client.post("/api/v1/auth/login", json={"email": "missing@example.test", "password": "incorrect"})).status_code, 401)
        self.assertEqual((await self.client.post("/api/v1/auth/login", json={"email": "missing@example.test", "password": "incorrect"})).status_code, 429)

    async def test_access_helper_validates_current_membership_when_called_directly(self):
        from app.saas.dependencies import get_access
        from starlette.requests import Request
        await self.register("direct@example.test")
        token = self.client.cookies.get(self.settings.session_cookie_name)
        request = Request({"type": "http", "method": "GET", "app": self.app, "headers": [(b"cookie", f"{self.settings.session_cookie_name}={token}".encode()), (b"x-workspace-id", self.workspace_id.encode())]})
        async with self.app.state.session_factory() as db:
            access = await get_access(request, db)
        self.assertEqual(access.workspace_id, self.workspace_id)
        self.assertEqual(access.role, "owner")

    async def test_member_removal_revokes_keys_even_after_rejoining(self):
        await self.register("remove-owner@example.test")
        member, user = await self.join("remove-admin@example.test", "admin", paid_fixture=True)
        key = (await member.post("/api/v1/api-keys", json={"name": "old integration"})).json()
        api_client = await self.new_client()
        api_client.headers.update({"Authorization": f"Bearer {key['key']}", "X-Workspace-ID": self.workspace_id})
        path = f"/api/v1/organizations/{self.workspace_id}/members/{user['user']['id']}"
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        self.assertEqual((await api_client.get("/api/v1/auth/me")).status_code, 401)
        invite = await self.invite(user["user"]["email"], "admin")
        self.assertEqual((await member.post("/api/v1/invitations/accept", json={"token": invite["token"]})).status_code, 200)
        self.assertEqual((await api_client.get("/api/v1/auth/me")).status_code, 401)

    async def test_owner_concurrent_demotion_preserves_an_owner(self):
        from app.saas.models import Membership
        await self.register("owner-one@example.test")
        other, user = await self.join("owner-two@example.test", "owner", paid_fixture=True)
        path = f"/api/v1/organizations/{self.workspace_id}/members"
        replies = await asyncio.gather(self.client.patch(f"{path}/{self.user['id']}", json={"role": "viewer"}), other.patch(f"{path}/{user['user']['id']}", json={"role": "viewer"}))
        self.assertEqual(sorted(reply.status_code for reply in replies), [200, 409])
        async with self.app.state.session_factory() as db:
            owners = (await db.scalars(select(Membership).where(Membership.workspace_id == self.workspace_id, Membership.role == "owner"))).all()
        self.assertEqual(len(owners), 1)

    async def test_cookie_flags_digest_storage_and_cross_origin_rejection(self):
        from app.saas.models import AuditEvent, Session, User
        self.settings.cookie_secure = True
        secure_client = await self.new_client()
        secure_client.base_url = "https://testserver"
        response = await secure_client.post("/api/v1/auth/register", json={"email": "flags@example.test", "password": "ExampleStrong123!", "organization_name": "Flags"})
        self.assertEqual(response.status_code, 201)
        cookie = response.headers["set-cookie"].lower()
        for flag in ("httponly", "samesite=lax", "secure"):
            self.assertIn(flag, cookie)
        token = secure_client.cookies.get(self.settings.session_cookie_name)
        async with self.app.state.session_factory() as db:
            session = await db.scalar(select(Session))
            user = await db.scalar(select(User))
            events = (await db.scalars(select(AuditEvent))).all()
            self.assertNotEqual(session.token_digest, token)
            self.assertTrue(user.password_hash.startswith("pbkdf2_sha256$600000$"))
            self.assertNotIn(token, str([event.details for event in events]))
        secure_client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        self.assertEqual((await secure_client.post("/api/v1/auth/logout", headers={"Origin": "https://attacker.example"})).status_code, 403)

    async def test_duplicate_concurrent_registration_reports_conflict(self):
        from app.saas.dependencies import get_db
        # Use normal deferred transactions here to exercise the database unique
        # constraint independently of SQLite's production write serialization.
        async def deferred_db():
            async with self.app.state.session_factory() as db:
                yield db
        self.app.dependency_overrides[get_db] = deferred_db
        other = await self.new_client()
        payload = {"email": "race@example.test", "password": "ExampleStrong123!", "organization_name": "Race"}
        responses = await asyncio.gather(self.client.post("/api/v1/auth/register", json=payload), other.post("/api/v1/auth/register", json=payload), return_exceptions=True)
        for response in responses:
            self.assertIsInstance(response, httpx.Response, type(response).__name__)
        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
