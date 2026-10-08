from dataclasses import dataclass
import hmac
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, Workspace
from .models import APIKey, Membership, Session, User, utcnow
from .security import aware, session_csrf, token_digest


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    workspace_id: str
    role: str
    credential_id: str


def valid_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404, "Resource not found")


async def get_db(request: Request):
    async with request.app.state.session_factory() as db:
        # SQLite has no row locks; serialize writes before any access reads.
        # PostgreSQL mutation services additionally lock their workspace row.
        if db.bind.dialect.name == "sqlite" and request.method not in {"GET", "HEAD", "OPTIONS"}:
            await db.execute(text("BEGIN IMMEDIATE"))
        yield db


async def get_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    authorization = request.headers.get("authorization")
    credential = None
    csrf = None
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise HTTPException(401, "Authentication required")
        credential = await db.scalar(select(APIKey).where(APIKey.token_digest == token_digest(token)))
        kind = "api_key"
    else:
        token = request.cookies.get(request.app.state.settings.session_cookie_name, "")
        if not token or len(token) > 256:
            raise HTTPException(401, "Authentication required")
        credential = await db.scalar(select(Session).where(Session.token_digest == token_digest(token)))
        kind = "session"
        csrf = session_csrf(token)
    if credential is None or credential.revoked_at is not None or aware(credential.expires_at) <= utcnow():
        raise HTTPException(401, "Authentication required")
    user = await db.get(User, credential.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "Authentication required")
    if kind == "api_key":
        membership = await db.scalar(select(Membership).join(Workspace, Workspace.id == Membership.workspace_id).where(
            Membership.user_id == user.id, Membership.workspace_id == credential.workspace_id, Workspace.is_deleted.is_(False),
        ))
        if membership is None:
            raise HTTPException(401, "Authentication required")
    elif request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(token_digest(supplied), credential.csrf_digest):
            raise HTTPException(403, "CSRF token required")
    request.state.user = user
    request.state.credential = credential
    request.state.credential_id = credential.id
    request.state.credential_kind = kind
    request.state.csrf_token = csrf
    return user


async def access_for_workspace(request: Request, db: AsyncSession, user: User, workspace_id: str) -> AccessContext:
    workspace_id = valid_id(workspace_id)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        await lock_workspace(db, workspace_id)
    if request.state.credential_kind == "api_key" and request.state.credential.workspace_id != workspace_id:
        raise HTTPException(404, "Resource not found")
    membership = await db.scalar(select(Membership).join(Workspace, Workspace.id == Membership.workspace_id).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user.id, Workspace.is_deleted.is_(False),
    ))
    if membership is None:
        raise HTTPException(404, "Resource not found")
    return AccessContext(user.id, workspace_id, membership.role, request.state.credential_id)


async def get_access(request: Request, db: AsyncSession = Depends(get_db)) -> AccessContext:
    user = await get_user(request, db)
    workspace_id = request.headers.get("X-Workspace-ID")
    if not workspace_id:
        raise HTTPException(400, "X-Workspace-ID is required")
    return await access_for_workspace(request, db, user, workspace_id)


def require_role(access: AccessContext, *roles: str):
    if access.role not in roles:
        raise HTTPException(403, "Insufficient organization role")


async def get_scoped_kb(db: AsyncSession, access: AccessContext, kb_id: str) -> KnowledgeBase:
    kb = await db.scalar(select(KnowledgeBase).where(
        KnowledgeBase.id == valid_id(kb_id), KnowledgeBase.workspace_id == access.workspace_id, KnowledgeBase.is_deleted.is_(False),
    ))
    if kb is None:
        raise HTTPException(404, "Resource not found")
    return kb


async def lock_workspace(db: AsyncSession, workspace_id: str):
    await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
