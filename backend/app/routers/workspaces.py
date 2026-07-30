"""
工作空间路由 — CRUD
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..exceptions import NotFoundError
from ..models import Workspace
from ..schemas import (
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceUpdate,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["Workspaces"])


@router.post("", response_model=WorkspaceResponse, status_code=201, summary="创建工作空间")
async def create_workspace(
    payload: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
):
    ws = Workspace(
        name=payload.name,
        description=payload.description,
        department=payload.department,
        owner_id=payload.owner_id,
    )
    db.add(ws)
    await db.flush()
    await db.refresh(ws)
    return ws


@router.get("", response_model=List[WorkspaceResponse], summary="列出工作空间")
async def list_workspaces(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=200),
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Workspace)
    if not include_deleted:
        stmt = stmt.where(Workspace.is_deleted == False)
    stmt = stmt.offset(skip).limit(limit).order_by(Workspace.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{workspace_id}", response_model=WorkspaceResponse, summary="获取工作空间详情")
async def get_workspace(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.is_deleted == False,
        )
    )
    ws = result.scalar_one_or_none()
    if not ws:
        raise NotFoundError("Workspace", str(workspace_id))
    return ws


@router.patch("/{workspace_id}", response_model=WorkspaceResponse, summary="更新工作空间")
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.is_deleted == False,
        )
    )
    ws = result.scalar_one_or_none()
    if not ws:
        raise NotFoundError("Workspace", str(workspace_id))

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(ws, field, value)

    await db.flush()
    await db.refresh(ws)
    return ws


@router.delete("/{workspace_id}", status_code=204, summary="软删除工作空间")
async def delete_workspace(
    workspace_id: uuid.UUID,
    hard: bool = Query(default=False, description="硬删除（物理删除）"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ws = result.scalar_one_or_none()
    if not ws:
        raise NotFoundError("Workspace", str(workspace_id))

    if hard:
        await db.delete(ws)
    else:
        ws.is_deleted = True
    await db.flush()
