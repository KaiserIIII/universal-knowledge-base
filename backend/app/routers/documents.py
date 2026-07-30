"""
文档管理路由 — 上传、列表、状态查询、删除 + 后台异步解析管道
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy import case, delete, select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import get_settings
from ..exceptions import (
    NotFoundError,
    ConflictError,
    ServiceUnavailableError,
    ValidationError,
)
from ..models import (
    Document,
    DocumentChunk,
    DocStatus,
    KnowledgeBase,
)
from ..rag_engine import DocumentProcessor
from ..schemas import (
    DocumentResponse,
    DocumentUploadResponse,
    DocumentDeleteResponse,
    ChunkResponse,
)

logger = logging.getLogger("documents")

router = APIRouter(prefix="/api/v1/kb", tags=["Documents"])

# 支持的文件类型
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".html", ".htm",
    ".pdf", ".docx", ".doc",
    ".json", ".xml", ".csv", ".log", ".yaml", ".yml",
}
UPLOAD_READ_SIZE = 1024 * 1024


async def _ensure_kb_available(kb_id: uuid.UUID, db: AsyncSession) -> None:
    result = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        )
    )
    if result.scalar_one_or_none() is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))


async def _persist_upload(file: UploadFile, target_path: str) -> tuple[str, int] | None:
    """Stream an UploadFile to disk while enforcing the size limit and hashing it."""
    max_file_size_bytes = get_settings().max_upload_size_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    try:
        async with aiofiles.open(target_path, "wb") as output:
            while chunk := await file.read(UPLOAD_READ_SIZE):
                size += len(chunk)
                if size > max_file_size_bytes:
                    return None
                digest.update(chunk)
                await output.write(chunk)
        return digest.hexdigest(), size
    finally:
        await file.close()


# ══════════════════════════════════════════════════════════════════
# 后台异步文档解析管道
# ══════════════════════════════════════════════════════════════════
async def process_document_pipeline(
    doc_id: str,
    file_path: str,
    filename: str,
    kb_id: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
):
    """
    后台异步任务流水线: 解析 → 切分 → PG写入 → 双写向量库
    由 BackgroundTasks 调度，不阻塞上传接口响应。
    """
    from ..database import AsyncSessionLocal
    from ..rag_engine import get_rag_engine

    engine = get_rag_engine()

    async with AsyncSessionLocal() as session:
        doc = None
        vectors_ingested = False
        try:
            await engine.ensure_ready()
            processor = DocumentProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )

            # 1. 查询文档记录
            result = await session.execute(
                select(Document).where(Document.id == doc_id)
            )
            doc = result.scalar_one_or_none()
            if not doc:
                logger.error(f"Document {doc_id} not found in pipeline")
                return

            # 2. 更新状态 → PARSING
            doc.status = DocStatus.PARSING
            await session.commit()

            # 3. 每个任务使用独立 processor，保证分块参数并发隔离。
            chunks = await asyncio.to_thread(
                processor.parse_and_chunk,
                file_path,
                filename,
                kb_id,
                doc_id,
            )

            if not chunks:
                doc.status = DocStatus.FAILED
                doc.error_msg = "Document produced no text after parsing (possibly empty or image-only PDF)"
                await session.commit()
                return

            # 4. 更新统计字段
            total_chars = sum(len(c["content"]) for c in chunks)
            total_words = sum(c["token_count"] for c in chunks)
            doc.word_count = total_words
            doc.chunk_count = len(chunks)
            doc.status = DocStatus.EMBEDDING
            await session.commit()

            # 5. 写入 PostgreSQL chunk 备份
            for c in chunks:
                db_chunk = DocumentChunk(
                    id=c["chunk_id"],
                    doc_id=doc_id,
                    chunk_index=c["chunk_index"],
                    content=c["content"],
                    token_count=c["token_count"],
                    metadata_={
                        "kb_id": kb_id,
                        "doc_id": doc_id,
                        "filename": filename,
                        "chunk_index": c["chunk_index"],
                        "total_chunks": len(chunks),
                    },
                )
                session.add(db_chunk)
            await session.commit()

            # 6. 写入 ChromaDB + BM25。失败必须让文档进入 FAILED，不能产生假完成状态。
            ingested = await engine.ingest_document(chunks)
            if ingested != len(chunks):
                raise RuntimeError(
                    f"Vector index accepted {ingested} of {len(chunks)} chunks"
                )
            vectors_ingested = True

            # 7. 更新知识库统计
            await session.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(
                    doc_count=KnowledgeBase.doc_count + 1,
                    chunk_count=KnowledgeBase.chunk_count + len(chunks),
                    total_chars=KnowledgeBase.total_chars + total_chars,
                )
            )

            # 8. 标记完成
            doc.status = DocStatus.COMPLETED
            doc.error_msg = None
            await session.commit()

            logger.info(
                f"Document '{filename}' ({doc_id}) processed successfully: "
                f"{len(chunks)} chunks, {total_chars} chars"
            )

        except Exception as e:
            logger.exception(f"Document pipeline failed for {doc_id}: {e}")
            await session.rollback()
            if vectors_ingested:
                try:
                    await engine.delete_document_chunks(doc_id)
                except Exception as cleanup_error:
                    logger.error("Failed to roll back vector data for %s: %s", doc_id, cleanup_error)
            try:
                # 重新查询 doc（因为事务可能已回滚）
                result = await session.execute(
                    select(Document).where(Document.id == doc_id)
                )
                doc = result.scalar_one_or_none()
                if doc:
                    await session.execute(
                        delete(DocumentChunk).where(DocumentChunk.doc_id == doc_id)
                    )
                    doc.status = DocStatus.FAILED
                    doc.error_msg = str(e)[:1000]
                    doc.word_count = 0
                    doc.chunk_count = 0
                    await session.commit()
            except Exception as inner_e:
                logger.error(f"Failed to update document status: {inner_e}")

        finally:
            # 清理临时文件
            try:
                if await asyncio.to_thread(os.path.exists, file_path):
                    await asyncio.to_thread(os.remove, file_path)
            except OSError as cleanup_error:
                logger.warning("Failed to remove temp file %s: %s", file_path, cleanup_error)


# ══════════════════════════════════════════════════════════════════
# API 端点
# ══════════════════════════════════════════════════════════════════

@router.post(
    "/{kb_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
    summary="批量上传文档（异步解析管道）",
)
async def upload_documents(
    kb_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="文档文件，支持 PDF/Word/Markdown/TXT"),
    chunk_size: Optional[int] = Form(default=None, ge=200, le=4000, description="分块大小"),
    chunk_overlap: Optional[int] = Form(default=None, ge=0, le=500, description="分块重叠"),
    db: AsyncSession = Depends(get_db),
):
    """
    接收文档上传，立即返回 202 Accepted。
    解析、切分、向量化在后台异步执行，通过 GET /documents/{doc_id} 查询状态。
    """
    settings = get_settings()
    chunk_size = settings.chunk_default_size if chunk_size is None else chunk_size
    chunk_overlap = settings.chunk_default_overlap if chunk_overlap is None else chunk_overlap
    if chunk_overlap >= chunk_size:
        raise ValidationError("chunk_overlap must be smaller than chunk_size")

    # 校验知识库存在
    await _ensure_kb_available(kb_id, db)

    # 校验文件数量
    if len(files) > settings.max_files_per_batch:
        raise ValidationError(
            f"Too many files: {len(files)} (max {settings.max_files_per_batch} per batch)"
        )

    upload_dir = os.path.join(
        os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "uploads")),
        str(kb_id),
    )
    await asyncio.to_thread(os.makedirs, upload_dir, exist_ok=True)

    accepted_docs: List[Document] = []
    skipped = 0

    for file in files:
        # 校验扩展名
        if not file.filename:
            skipped += 1
            continue
        original_filename = Path(file.filename).name
        ext = Path(original_filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            logger.warning(f"Skipping unsupported file: {file.filename} (.{ext})")
            skipped += 1
            continue

        safe_filename = f"{secrets.token_hex(16)}_{original_filename}"
        tmp_path = os.path.join(upload_dir, safe_filename)
        persisted = await _persist_upload(file, tmp_path)
        if persisted is None:
            logger.warning(
                "Skipping oversized file: %s (> %sMB)",
                original_filename,
                settings.max_upload_size_mb,
            )
            if await asyncio.to_thread(os.path.exists, tmp_path):
                await asyncio.to_thread(os.remove, tmp_path)
            skipped += 1
            continue
        file_hash, file_size_bytes = persisted
        if file_size_bytes == 0:
            if await asyncio.to_thread(os.path.exists, tmp_path):
                await asyncio.to_thread(os.remove, tmp_path)
            skipped += 1
            continue

        # 校验重复
        existing = await db.execute(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.file_hash == file_hash,
            )
        )
        if existing.scalar_one_or_none():
            await asyncio.to_thread(os.remove, tmp_path)
            skipped += 1
            continue

        # 创建文档记录
        doc = Document(
            kb_id=kb_id,
            file_hash=file_hash,
            filename=original_filename,
            file_type=ext.lstrip("."),
            file_size_bytes=file_size_bytes,
            status=DocStatus.UPLOADING,
        )
        db.add(doc)
        await db.flush()
        await db.refresh(doc)

        accepted_docs.append(doc)

        # 注册后台解析任务
        background_tasks.add_task(
            process_document_pipeline,
            str(doc.id),
            tmp_path,
            doc.filename,
            str(kb_id),
            chunk_size,
            chunk_overlap,
        )

    await db.commit()

    return DocumentUploadResponse(
        accepted=len(accepted_docs),
        skipped=skipped,
        documents=[DocumentResponse.model_validate(d) for d in accepted_docs],
    )


@router.get(
    "/{kb_id}/documents",
    response_model=List[DocumentResponse],
    summary="列出知识库下的文档",
)
async def list_documents(
    kb_id: uuid.UUID,
    status_filter: str = Query(default=None, alias="status", description="按状态过滤"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Document).where(Document.kb_id == kb_id)

    await _ensure_kb_available(kb_id, db)

    if status_filter:
        try:
            doc_status = DocStatus(status_filter)
            stmt = stmt.where(Document.status == doc_status)
        except ValueError:
            raise ValidationError(
                f"Invalid status: '{status_filter}'. "
                f"Valid values: {[s.value for s in DocStatus]}"
            )

    stmt = stmt.offset(skip).limit(limit).order_by(Document.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get(
    "/{kb_id}/documents/{doc_id}",
    response_model=DocumentResponse,
    summary="获取文档详情与处理状态",
)
async def get_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_kb_available(kb_id, db)
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.kb_id == kb_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise NotFoundError("Document", str(doc_id))
    return doc


@router.get(
    "/{kb_id}/documents/{doc_id}/chunks",
    response_model=List[ChunkResponse],
    summary="获取文档的所有切片",
)
async def get_document_chunks(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_kb_available(kb_id, db)
    # 确认文档存在
    doc_result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.kb_id == kb_id)
    )
    if not doc_result.scalar_one_or_none():
        raise NotFoundError("Document", str(doc_id))

    chunks_result = await db.execute(
        select(DocumentChunk)
        .where(DocumentChunk.doc_id == doc_id)
        .order_by(DocumentChunk.chunk_index)
    )
    return chunks_result.scalars().all()


@router.delete(
    "/{kb_id}/documents/{doc_id}",
    response_model=DocumentDeleteResponse,
    summary="删除文档（含向量清理）",
)
async def delete_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_kb_available(kb_id, db)
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.kb_id == kb_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise NotFoundError("Document", str(doc_id))

    completed = doc.status == DocStatus.COMPLETED
    chunk_count = doc.chunk_count if completed else 0
    removed_chars = 0
    if completed:
        removed_chars = int((await db.execute(
            select(func.coalesce(func.sum(func.length(DocumentChunk.content)), 0))
            .where(DocumentChunk.doc_id == doc_id)
        )).scalar_one())

    # 1. 先删除派生索引。失败时保留关系数据，允许调用方安全重试，
    # 避免 SQL 已删除但 Chroma/BM25 中仍能检索到孤儿内容。
    try:
        from ..rag_engine import get_rag_engine
        engine = get_rag_engine()
        deleted = await engine.delete_document_chunks(str(doc_id))
        logger.info(f"Deleted {deleted} vectors for document {doc_id}")
    except Exception as e:
        logger.exception("Index cleanup failed for document %s", doc_id)
        raise ServiceUnavailableError(
            "retrieval index",
            f"document cleanup failed; relational data was preserved: {e}",
        ) from e

    # 2. 更新知识库统计
    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .values(
            doc_count=case(
                (KnowledgeBase.doc_count > 0, KnowledgeBase.doc_count - (1 if completed else 0)),
                else_=0,
            ),
            chunk_count=case(
                (KnowledgeBase.chunk_count >= chunk_count, KnowledgeBase.chunk_count - chunk_count),
                else_=0,
            ),
            total_chars=case(
                (KnowledgeBase.total_chars >= removed_chars, KnowledgeBase.total_chars - removed_chars),
                else_=0,
            ),
        )
    )

    # 3. 级联删除文档及其切片（models 层已设置 cascade）
    await db.delete(doc)
    await db.commit()

    return DocumentDeleteResponse(
        deleted=True,
        message=f"Document '{doc.filename}' and {chunk_count} chunks deleted",
    )
