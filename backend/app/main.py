"""
通用知识库 — FastAPI 应用入口 (零依赖嵌入式版本)

启动方式（在 backend 目录）:
    python run.py
或:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
import time
import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import get_settings
from .database import engine, check_db_connection
from .exceptions import (
    AppException,
    app_exception_handler,
    validation_exception_handler,
    generic_exception_handler,
    RequestIDMiddleware,
)
from .rag_engine import get_rag_engine
from .schemas import HealthResponse

# ── 日志配置 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# 降低第三方库日志噪音
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)

# ── 全局变量 ─────────────────────────────────────────────────
_settings = get_settings()
_start_time = time.time()


async def migrate_existing_schema() -> None:
    """Apply small idempotent migrations that ``create_all`` cannot add."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            legacy_workspace = (await conn.execute(
                text("SELECT 1 FROM workspaces WHERE id = :legacy_id LIMIT 1"),
                {"legacy_id": "default"},
            )).scalar_one_or_none()
            if legacy_workspace is not None:
                migrated_id = str(uuid.uuid4())
                await conn.execute(
                    text(
                        """
                        INSERT INTO workspaces (
                            id, name, description, department, owner_id,
                            is_deleted, created_at, updated_at
                        )
                        SELECT
                            :migrated_id, name, description, department, owner_id,
                            is_deleted, created_at, updated_at
                        FROM workspaces
                        WHERE id = :legacy_id
                        """
                    ),
                    {"migrated_id": migrated_id, "legacy_id": "default"},
                )
                await conn.execute(
                    text(
                        "UPDATE knowledge_bases "
                        "SET workspace_id = :migrated_id WHERE workspace_id = :legacy_id"
                    ),
                    {"migrated_id": migrated_id, "legacy_id": "default"},
                )
                await conn.execute(
                    text("DELETE FROM workspaces WHERE id = :legacy_id"),
                    {"legacy_id": "default"},
                )
                logger.info(
                    "Migrated legacy workspace id 'default' to UUID %s",
                    migrated_id,
                )

        # Unique constraints declared after the first deployment are not added by
        # SQLAlchemy create_all(). Unique indexes enforce the same invariant on
        # both existing SQLite databases and PostgreSQL deployments.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_kb_file_hash "
            "ON documents (kb_id, file_hash)"
        ))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_doc_index "
            "ON document_chunks (doc_id, chunk_index)"
        ))


async def reconcile_persisted_state(rag) -> None:
    """Repair interrupted pipelines, derived chunks, vectors, and KB counters."""
    from sqlalchemy import delete, func, select, update
    from .database import AsyncSessionLocal
    from .models import Document, DocumentChunk, DocStatus, KnowledgeBase

    interrupted_statuses = [
        DocStatus.UPLOADING,
        DocStatus.PARSING,
        DocStatus.EMBEDDING,
    ]
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Document)
            .where(Document.status.in_(interrupted_statuses))
            .values(
                status=DocStatus.FAILED,
                error_msg="Processing interrupted by service restart",
                word_count=0,
                chunk_count=0,
            )
        )

        failed_ids = list((await session.execute(
            select(Document.id).where(Document.status == DocStatus.FAILED)
        )).scalars().all())
        if failed_ids:
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.doc_id.in_(failed_ids))
            )
            await session.execute(
                update(Document)
                .where(Document.id.in_(failed_ids))
                .values(word_count=0, chunk_count=0)
            )

        completed_docs = list((await session.execute(
            select(Document).where(Document.status == DocStatus.COMPLETED)
        )).scalars().all())
        completed_ids = {str(document.id) for document in completed_docs}
        chunk_counts = dict((await session.execute(
            select(DocumentChunk.doc_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.doc_id.in_(completed_ids))
            .group_by(DocumentChunk.doc_id)
        )).all()) if completed_ids else {}
        for document in completed_docs:
            document.chunk_count = int(chunk_counts.get(document.id, 0))

        aggregate_rows = (await session.execute(
            select(
                Document.kb_id,
                func.count(func.distinct(Document.id)),
                func.count(DocumentChunk.id),
                func.coalesce(func.sum(func.length(DocumentChunk.content)), 0),
            )
            .outerjoin(DocumentChunk, DocumentChunk.doc_id == Document.id)
            .where(Document.status == DocStatus.COMPLETED)
            .group_by(Document.kb_id)
        )).all()
        aggregate_by_kb = {
            row[0]: (int(row[1]), int(row[2]), int(row[3]))
            for row in aggregate_rows
        }
        knowledge_bases = list((await session.execute(
            select(KnowledgeBase)
        )).scalars().all())
        for knowledge_base in knowledge_bases:
            doc_count, chunk_count, total_chars = aggregate_by_kb.get(
                knowledge_base.id,
                (0, 0, 0),
            )
            knowledge_base.doc_count = doc_count
            knowledge_base.chunk_count = chunk_count
            knowledge_base.total_chars = total_chars
        await session.commit()

    removed_vectors = await rag.reconcile_index(completed_ids)
    logger.info(
        "Persistence reconciliation complete: %s failed docs, %s stale vectors removed",
        len(failed_ids),
        removed_vectors,
    )


# ══════════════════════════════════════════════════════════════════
# 生命周期管理
# ══════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期:
      startup:  连接数据库 → 创建表 → 初始化 RAG 引擎 → 启动嵌入服务
      shutdown: 优雅关闭所有连接
    """
    # ── Startup ──────────────────────────────────────────
    logger.info(f"├─ {_settings.app_name} v{_settings.app_version} starting...")

    # 1. 创建数据库表 (若不存在)
    try:
        from .models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await migrate_existing_schema()
        logger.info("├─ Database tables verified")
    except Exception as e:
        logger.error(f"├─ [FAIL] Database initialization: {e}")
        raise

    # 2. 初始化 RAG 引擎 (ChromaDB + 本地 Embedding + BM25)
    try:
        rag = get_rag_engine()
        await rag.init_stores()
        await reconcile_persisted_state(rag)
        logger.info(f"├─ RAG Engine initialized (ChromaDB + local BM25 + local Embedding)")
    except Exception as e:
        logger.error(f"├─ [FAIL] RAG Engine initialization: {e}")
        logger.warning("├─ Some search features may be unavailable")

    # 3. 确保上传目录存在
    upload_dir = str(Path(_settings.upload_temp_dir).resolve())
    os.makedirs(upload_dir, exist_ok=True)
    os.environ.setdefault("UPLOAD_DIR", upload_dir)
    logger.info(f"├─ Upload directory: {upload_dir}")

    # 4. 检查 LLM API Key
    if not _settings.llm_api_key or _settings.llm_api_key.startswith("sk-your-"):
        logger.warning("├─ [WARN] LLM_API_KEY not set — chat endpoints will fail")
    else:
        logger.info(f"├─ LLM Model: {_settings.llm_model}")

    logger.info(f"└─ {_settings.app_name} ready ({time.time() - _start_time:.2f}s)")

    yield  # 应用运行中

    # ── Shutdown ─────────────────────────────────────────
    logger.info("├─ Shutting down...")
    try:
        rag = get_rag_engine()
        await rag.close()
    except Exception as error:
        logger.warning("RAG shutdown failed: %s", error)

    try:
        await engine.dispose()
    except Exception as error:
        logger.warning("Database shutdown failed: %s", error)

    logger.info("└─ Shutdown complete")


# ══════════════════════════════════════════════════════════════════
# FastAPI 应用工厂
# ══════════════════════════════════════════════════════════════════
app = FastAPI(
    title=_settings.app_name,
    version=_settings.app_version,
    description="""
## 通用知识库 (嵌入式版本)

一个基于 RAG (Retrieval-Augmented Generation) 的本地优先通用知识库系统。
默认使用 SQLite，也支持 PostgreSQL；检索无需部署独立向量或搜索服务。

### 核心能力
- **📄 智能文档解析** — 支持 PDF/Word/HTML/Markdown/TXT，语义切分
- **🔍 混合检索** — ChromaDB 向量 + 本地 BM25 关键词，Alpha 加权融合
- **🧠 本地 Embedding** — bge-small-zh-v1.5，无需外部 API
- **🤖 RAG 对话** — 自动检索知识库上下文，流式 SSE 输出，引用溯源
- **🔌 Agent API** — 开放的检索接口，可被 Dify/Coze/自定义 Agent 调用

### API 分类
| 前缀 | 说明 |
|------|------|
| `/api/v1/kb` | 知识库管理、文档上传、状态查询 |
| `/api/v1/agent` | Agent 开放检索接口 |
| `/api/v1/chat` | 流式/同步对话接口 |
| `/health` | 系统健康检查 |
""",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ══════════════════════════════════════════════════════════════════
# 中间件注册
# ══════════════════════════════════════════════════════════════════
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制为具体域名
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RAG-Citations", "X-RAG-Search-Time-Ms"],
)

app.add_middleware(RequestIDMiddleware)


# ══════════════════════════════════════════════════════════════════
# 全局异常处理器注册
# ══════════════════════════════════════════════════════════════════
from fastapi.exceptions import RequestValidationError

app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)


# ══════════════════════════════════════════════════════════════════
# 路由注册
# ══════════════════════════════════════════════════════════════════
from .routers.knowledge_bases import router as kb_router
from .routers.documents import router as documents_router
from .routers.agent import router as agent_router
from .routers.chat import router as chat_router
from .routers.settings import router as settings_router

app.include_router(kb_router)
app.include_router(documents_router)
app.include_router(agent_router)
app.include_router(chat_router)
app.include_router(settings_router)


# ══════════════════════════════════════════════════════════════════
# 健康检查 & 系统端点
# ══════════════════════════════════════════════════════════════════
@app.get("/", include_in_schema=False)
async def root():
    """返回无构建步骤的单页知识库界面。"""
    frontend_path = Path(__file__).resolve().parents[2] / "index.html"
    return FileResponse(frontend_path, media_type="text/html; charset=utf-8")


@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """系统健康检查 — 嵌入式版本：检查 SQLite + ChromaDB + 本地模型状态"""
    db_ok = await check_db_connection()

    chroma_ok = False
    bm25_ok = False
    embedding_ok = False

    rag = get_rag_engine()
    try:
        if rag.chroma_collection is not None:
            n = await asyncio.to_thread(rag.chroma_collection.count)
            chroma_ok = n >= 0
    except Exception as error:
        logger.debug("Chroma health check failed: %s", error)

    try:
        bm25_ok = rag.bm25 is not None
    except Exception as error:
        logger.debug("BM25 health check failed: %s", error)

    try:
        if rag.embedder and rag.embedder._model is not None:
            embedding_ok = rag.embedder.dim > 0
    except Exception as error:
        logger.debug("Embedding health check failed: %s", error)

    uptime = time.time() - _start_time

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=_settings.app_version,
        timestamp=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        db_connected=db_ok,
        chroma_connected=chroma_ok,
        bm25_connected=bm25_ok,
        embedding_loaded=embedding_ok,
        uptime_seconds=round(uptime, 2),
    )


@app.get("/api/v1/stats", tags=["System"], summary="系统统计信息")
async def system_stats():
    """返回全系统级别的统计数据"""
    from .database import AsyncSessionLocal
    from sqlalchemy import select, func
    from .models import KnowledgeBase, Document, DocumentChunk, DocStatus

    async with AsyncSessionLocal() as session:
        kb_count = (await session.execute(
            select(func.count(KnowledgeBase.id)).where(KnowledgeBase.is_deleted == False)
        )).scalar() or 0

        doc_count = (await session.execute(
            select(func.count(Document.id))
        )).scalar() or 0

        chunk_count = (await session.execute(
            select(func.count(DocumentChunk.id))
        )).scalar() or 0

        total_chars = (await session.execute(
            select(func.sum(KnowledgeBase.total_chars)).where(
                KnowledgeBase.is_deleted == False
            )
        )).scalar() or 0

        doc_by_status = {}
        for status_val in DocStatus:
            cnt = (await session.execute(
                select(func.count(Document.id)).where(Document.status == status_val)
            )).scalar() or 0
            doc_by_status[status_val.value] = cnt

    return {
        "kb_count": kb_count,
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "total_chars": total_chars or 0,
        "doc_by_status": doc_by_status,
        "uptime_seconds": round(time.time() - _start_time, 2),
    }


# ══════════════════════════════════════════════════════════════════
# 开发模式直接运行
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info",
    )
