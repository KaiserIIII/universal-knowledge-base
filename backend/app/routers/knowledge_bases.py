"""
知识库路由 — CRUD + 统计
"""
from __future__ import annotations

import asyncio
import uuid
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import get_settings
from ..exceptions import NotFoundError
from ..models import KnowledgeBase, Document, Workspace
from ..schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeBaseListResponse,
)

router = APIRouter(prefix="/api/v1/kb", tags=["Knowledge Bases"])

async def _get_system_workspace(db: AsyncSession) -> Workspace:
    result = await db.execute(
        select(Workspace).where(Workspace.is_deleted == False)
        .order_by(Workspace.created_at.asc()).limit(1)
    )
    workspace = result.scalar_one_or_none()
    if workspace:
        return workspace
    workspace = Workspace(name="系统数据", description="内部兼容字段")
    db.add(workspace)
    await db.flush()
    return workspace


@router.post("", response_model=KnowledgeBaseResponse, status_code=201, summary="创建知识库")
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
):
    # 知识库必须归属一个真实且未删除的工作空间。
    workspace = await _get_system_workspace(db)

    kb = KnowledgeBase(
        workspace_id=workspace.id,
        name=payload.name,
        description=payload.description,
        kb_type=payload.kb_type,
        category_tags=payload.category_tags,
        avatar=payload.avatar,
    )
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    return kb


@router.get("", response_model=List[KnowledgeBaseListResponse], summary="列出知识库")
async def list_knowledge_bases(
    kb_type: str|None = Query(default=None),
    include_deleted: bool = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(KnowledgeBase)
    if not include_deleted:
        stmt = stmt.where(KnowledgeBase.is_deleted == False)
    if kb_type:
        stmt = stmt.where(KnowledgeBase.kb_type == kb_type)
    stmt = stmt.offset(skip).limit(limit).order_by(KnowledgeBase.created_at.desc())

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse, summary="获取知识库详情")
async def get_knowledge_base(
    kb_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise NotFoundError("KnowledgeBase", str(kb_id))
    return kb


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse, summary="更新知识库")
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(kb, field, value)

    await db.flush()
    await db.refresh(kb)
    return kb


@router.delete("/{kb_id}", status_code=204, summary="删除知识库")
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    hard: bool = Query(default=False, description="硬删除（同时清理 Chroma 与 BM25 索引）"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    if hard:
        # 硬删除：先清理向量库
        from ..rag_engine import get_rag_engine
        engine = get_rag_engine()
        await engine.delete_kb_chunks(str(kb_id))
        await db.delete(kb)
        upload_root = Path(get_settings().upload_temp_dir).resolve()
        upload_dir = (upload_root / str(kb_id)).resolve()
        if upload_root == upload_dir.parent and upload_dir.exists():
            await asyncio.to_thread(shutil.rmtree, upload_dir, True)
    else:
        kb.is_deleted = True

    await db.flush()


@router.get("/{kb_id}/stats", summary="知识库统计")
async def get_kb_stats(
    kb_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """返回知识库的文档数量、切片数量、总字符数、状态分布"""
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    # 按状态统计文档
    from ..models import DocStatus

    doc_stats = {}
    for status_val in DocStatus:
        count_result = await db.execute(
            select(func.count(Document.id)).where(
                Document.kb_id == kb_id,
                Document.status == status_val,
            )
        )
        doc_stats[status_val.value] = count_result.scalar() or 0

    return {
        "kb_id": str(kb_id),
        "kb_name": kb.name,
        "doc_count": kb.doc_count,
        "chunk_count": kb.chunk_count,
        "total_chars": kb.total_chars,
        "doc_by_status": doc_stats,
    }
