from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace
from .auth import EmailInput, organizations_json, require_session
from .dependencies import AccessContext, access_for_workspace, get_access, get_db, get_user, lock_workspace, require_role, valid_id
from .models import APIKey, AuditEvent, Invitation, Membership, User, utcnow
from .security import aware, new_token, token_digest


Role = Literal["owner", "admin", "editor", "viewer"]
router = APIRouter(prefix="/api/v1", tags=["organizations"])
keys_router = APIRouter(prefix="/api/v1/api-keys", tags=["API keys"])


class OrganizationInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()


class OrganizationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Name is required")
        return value.strip() if value is not None else value


class RoleInput(BaseModel):
    role: Role


class InvitationInput(EmailInput):
    role: Role = "viewer"


class AcceptInput(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class KeyInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()


def audit(db, access, action, resource_id, **details):
    db.add(AuditEvent(workspace_id=access.workspace_id, user_id=access.user_id, action=action, resource_id=resource_id, details=details))


def organization_json(workspace, role):
    return {"id": workspace.id, "name": workspace.name, "description": workspace.description, "role": role}


async def path_access(workspace_id: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    # The path identifies the organization being managed; headers cannot confer
    # access to it. Authentication still uses the current server membership.
    return await access_for_workspace(request, db, user, workspace_id)


@router.get("/organizations")
async def list_organizations(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    workspace = request.state.credential.workspace_id if request.state.credential_kind == "api_key" else None
    return {"organizations": await organizations_json(db, user.id, workspace)}


@router.post("/organizations", status_code=201)
async def create_organization(payload: OrganizationInput, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    require_session(request)
    workspace = Workspace(name=payload.name, description=payload.description, owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db.add(AuditEvent(workspace_id=workspace.id, user_id=user.id, action="organization.created", resource_id=workspace.id))
    await db.commit()
    return organization_json(workspace, "owner")


@router.get("/organizations/{workspace_id}")
async def get_organization(access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    return organization_json(await db.get(Workspace, access.workspace_id), access.role)


@router.patch("/organizations/{workspace_id}")
async def update_organization(payload: OrganizationPatch, access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    workspace = await db.get(Workspace, access.workspace_id)
    if payload.name is not None:
        workspace.name = payload.name
    if "description" in payload.model_fields_set:
        workspace.description = payload.description
    audit(db, access, "organization.updated", workspace.id)
    await db.commit()
    return organization_json(workspace, access.role)


def member_json(membership, user):
    return {"id": membership.id, "user_id": user.id, "email": user.email, "name": user.name, "role": membership.role, "created_at": membership.created_at}


@router.get("/organizations/{workspace_id}/members")
async def list_members(access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Membership, User).join(User, Membership.user_id == User.id).where(Membership.workspace_id == access.workspace_id).order_by(Membership.created_at))).all()
    return {"members": [member_json(member, user) for member, user in rows]}


async def editable_member(db, access, user_id, new_role=None):
    require_role(access, "owner", "admin")
    await lock_workspace(db, access.workspace_id)
    member = await db.scalar(select(Membership).where(Membership.workspace_id == access.workspace_id, Membership.user_id == valid_id(user_id)).with_for_update())
    if member is None:
        raise HTTPException(404, "Resource not found")
    if access.role != "owner" and (member.role == "owner" or new_role == "owner"):
        raise HTTPException(403, "Owner role required")
    if member.role == "owner" and new_role != "owner":
        owners = await db.scalar(select(func.count(Membership.id)).where(Membership.workspace_id == access.workspace_id, Membership.role == "owner"))
        if owners <= 1:
            raise HTTPException(409, "The last owner cannot be removed or demoted")
    return member


@router.patch("/organizations/{workspace_id}/members/{user_id}")
async def change_role(user_id: str, payload: RoleInput, access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    member = await editable_member(db, access, user_id, payload.role)
    member.role = payload.role
    audit(db, access, "membership.role_changed", member.user_id, role=payload.role)
    await db.commit()
    return member_json(member, await db.get(User, member.user_id))


@router.delete("/organizations/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(user_id: str, access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    member = await editable_member(db, access, user_id)
    # Removing a membership permanently revokes that user's tenant keys; a
    # future invitation must not resurrect an old integration credential.
    await db.execute(update(APIKey).where(APIKey.workspace_id == access.workspace_id, APIKey.user_id == member.user_id, APIKey.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, access, "membership.removed", member.user_id)
    await db.delete(member)
    await db.commit()


def invitation_json(invite):
    return {"id": invite.id, "email": invite.email, "role": invite.role, "expires_at": invite.expires_at, "accepted_at": invite.accepted_at, "revoked_at": invite.revoked_at, "created_at": invite.created_at}


@router.get("/organizations/{workspace_id}/invitations")
async def list_invitations(access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    rows = (await db.scalars(select(Invitation).where(Invitation.workspace_id == access.workspace_id).order_by(Invitation.created_at.desc()))).all()
    return {"invitations": [invitation_json(row) for row in rows]}


@router.post("/organizations/{workspace_id}/invitations", status_code=201)
async def invite_member(payload: InvitationInput, request: Request, access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    if payload.role == "owner":
        require_role(access, "owner")
    await lock_workspace(db, access.workspace_id)
    if await db.scalar(select(Membership.id).join(User, User.id == Membership.user_id).where(Membership.workspace_id == access.workspace_id, User.email == payload.email)):
        raise HTTPException(409, "User is already a member")
    if await db.scalar(select(Invitation.id).where(Invitation.workspace_id == access.workspace_id, Invitation.email == payload.email, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None), Invitation.expires_at > utcnow())):
        raise HTTPException(409, "An active invitation already exists")
    token = new_token()
    row = Invitation(workspace_id=access.workspace_id, invited_by=access.user_id, email=payload.email, role=payload.role, token_digest=token_digest(token), expires_at=utcnow() + timedelta(seconds=request.app.state.settings.invite_ttl_seconds))
    db.add(row)
    await db.flush()
    audit(db, access, "invitation.created", row.id, role=payload.role)
    await db.commit()
    return {**invitation_json(row), "token": token}


@router.delete("/organizations/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invite(invitation_id: str, access: AccessContext = Depends(path_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    row = await db.scalar(select(Invitation).where(Invitation.id == valid_id(invitation_id), Invitation.workspace_id == access.workspace_id))
    if row is None:
        raise HTTPException(404, "Resource not found")
    if row.role == "owner":
        require_role(access, "owner")
    row.revoked_at = utcnow()
    audit(db, access, "invitation.revoked", row.id)
    await db.commit()


@router.post("/invitations/accept")
async def accept_invite(payload: AcceptInput, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_user)):
    require_session(request)
    row = await db.scalar(select(Invitation).where(Invitation.token_digest == token_digest(payload.token)))
    if row is None:
        raise HTTPException(404, "Invitation not found")
    await lock_workspace(db, row.workspace_id)
    # Refresh after locking to observe a concurrent acceptance on PostgreSQL.
    await db.refresh(row)
    workspace = await db.get(Workspace, row.workspace_id)
    if row.email != user.email or row.accepted_at is not None or row.revoked_at is not None or aware(row.expires_at) <= utcnow() or workspace is None or workspace.is_deleted:
        raise HTTPException(404, "Invitation not found")
    if await db.scalar(select(Membership.id).where(Membership.workspace_id == row.workspace_id, Membership.user_id == user.id)):
        raise HTTPException(409, "User is already a member")
    db.add(Membership(workspace_id=row.workspace_id, user_id=user.id, role=row.role))
    row.accepted_at = utcnow()
    db.add(AuditEvent(workspace_id=row.workspace_id, user_id=user.id, action="invitation.accepted", resource_id=row.id))
    await db.commit()
    return {"organization": organization_json(workspace, row.role)}


def key_json(row):
    return {"id": row.id, "name": row.name, "prefix": row.prefix, "user_id": row.user_id, "expires_at": row.expires_at, "revoked_at": row.revoked_at, "created_at": row.created_at}


@keys_router.get("")
async def list_keys(access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    rows = (await db.scalars(select(APIKey).where(APIKey.workspace_id == access.workspace_id).order_by(APIKey.created_at.desc()))).all()
    return {"api_keys": [key_json(row) for row in rows]}


@keys_router.post("", status_code=201)
async def create_key(payload: KeyInput, request: Request, access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    token = "kb_" + new_token()
    row = APIKey(workspace_id=access.workspace_id, user_id=access.user_id, name=payload.name, prefix=token[:12], token_digest=token_digest(token), expires_at=utcnow() + timedelta(seconds=request.app.state.settings.api_key_ttl_seconds))
    db.add(row)
    await db.flush()
    audit(db, access, "api_key.created", row.id)
    await db.commit()
    return {**key_json(row), "key": token}


@keys_router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: str, access: AccessContext = Depends(get_access), db: AsyncSession = Depends(get_db)):
    require_role(access, "owner", "admin")
    row = await db.scalar(select(APIKey).where(APIKey.id == valid_id(key_id), APIKey.workspace_id == access.workspace_id))
    if row is None:
        raise HTTPException(404, "Resource not found")
    row.revoked_at = utcnow()
    audit(db, access, "api_key.revoked", row.id)
    await db.commit()
