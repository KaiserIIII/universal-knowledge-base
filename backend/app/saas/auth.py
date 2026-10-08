from datetime import timedelta
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace
from .dependencies import get_db, get_user, valid_id
from .models import AuditEvent, Membership, Session, User, utcnow
from .security import hash_password, new_token, session_csrf, token_digest, verify_password


router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class EmailInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Valid email is required")
        return value


class LoginInput(EmailInput):
    password: str = Field(min_length=1, max_length=1024)


class RegisterInput(EmailInput):
    password: str = Field(min_length=12, max_length=1024)
    organization_name: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=128)

    @field_validator("organization_name")
    @classmethod
    def organization_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Organization name is required")
        return value


def user_json(user):
    return {"id": user.id, "email": user.email, "name": user.name, "created_at": user.created_at}


async def organizations_json(db, user_id, workspace_id=None):
    query = select(Workspace, Membership.role).join(Membership, Membership.workspace_id == Workspace.id).where(Membership.user_id == user_id, Workspace.is_deleted.is_(False)).order_by(Workspace.created_at)
    if workspace_id:
        query = query.where(Workspace.id == workspace_id)
    return [{"id": workspace.id, "name": workspace.name, "description": workspace.description, "role": role} for workspace, role in (await db.execute(query)).all()]


async def auth_json(request, db, user, csrf):
    workspace = request.state.credential.workspace_id if getattr(request.state, "credential_kind", None) == "api_key" else None
    return {"user": user_json(user), "organizations": await organizations_json(db, user.id, workspace), "csrf_token": csrf}


async def create_session(request, response, db, user):
    token = new_token()
    csrf = session_csrf(token)
    settings = request.app.state.settings
    session = Session(user_id=user.id, token_digest=token_digest(token), csrf_digest=token_digest(csrf), expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds))
    db.add(session)
    await db.commit()
    response.set_cookie(settings.session_cookie_name, token, max_age=settings.session_ttl_seconds, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return await auth_json(request, db, user, csrf)


@router.post("/register", status_code=201)
async def register(payload: RegisterInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if not request.app.state.settings.registration_enabled:
        raise HTTPException(403, "Registration is disabled")
    request.app.state.auth_throttle.check(request, "register")
    if await db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(409, "Email is already registered")
    user = User(email=payload.email, password_hash=await hash_password(payload.password), name=payload.name.strip())
    try:
        db.add(user)
        await db.flush()
        organization = Workspace(name=payload.organization_name, owner_id=user.id)
        db.add(organization)
        await db.flush()
        db.add(Membership(workspace_id=organization.id, user_id=user.id, role="owner"))
        db.add(AuditEvent(workspace_id=organization.id, user_id=user.id, action="account.registered", resource_id=user.id))
        return await create_session(request, response, db, user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Email is already registered")


@router.post("/login")
async def login(payload: LoginInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    request.app.state.auth_throttle.check(request, "login")
    user = await db.scalar(select(User).where(User.email == payload.email))
    # Unknown users still perform the same KDF work to limit account timing leaks.
    stored = user.password_hash if user else request.app.state.dummy_password_hash
    valid = await verify_password(payload.password, stored)
    if not user or not user.is_active or not valid:
        raise HTTPException(401, "Invalid email or password")
    db.add(AuditEvent(user_id=user.id, action="account.login", resource_id=user.id))
    return await create_session(request, response, db, user)


@router.get("/me")
async def me(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    response.headers["Cache-Control"] = "no-store"
    return await auth_json(request, db, user, request.state.csrf_token)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    if request.state.credential_kind != "session":
        raise HTTPException(403, "Browser session required")
    request.state.credential.revoked_at = utcnow()
    db.add(AuditEvent(user_id=user.id, action="session.revoked", resource_id=request.state.credential_id))
    await db.commit()
    response.delete_cookie(request.app.state.settings.session_cookie_name, path="/", secure=request.app.state.settings.cookie_secure, httponly=True, samesite="lax")


def require_session(request):
    if request.state.credential_kind != "session":
        raise HTTPException(403, "Browser session required")


@router.get("/sessions")
async def sessions(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    require_session(request)
    rows = (await db.scalars(select(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None), Session.expires_at > utcnow()).order_by(Session.created_at.desc()))).all()
    return {"sessions": [{"id": row.id, "created_at": row.created_at, "expires_at": row.expires_at, "current": row.id == request.state.credential_id} for row in rows]}


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    require_session(request)
    row = await db.scalar(select(Session).where(Session.id == valid_id(session_id), Session.user_id == user.id))
    if row is None:
        raise HTTPException(404, "Resource not found")
    row.revoked_at = utcnow()
    db.add(AuditEvent(user_id=user.id, action="session.revoked", resource_id=row.id))
    await db.commit()
