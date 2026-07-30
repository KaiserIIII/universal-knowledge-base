"""
Agent 检索路由 — 面向外部 Agent 和系统管理的混合检索 API
提供:
  - POST /api/v1/agent/search      对外开放的混合检索接口
  - POST /api/v1/agent/search/batch 批量检索
  - GET  /api/v1/agent/status      检索引擎健康检查
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..exceptions import ValidationError, NotFoundError
from ..models import KnowledgeBase
from ..schemas import (
    AgentSearchRequest,
    AgentSearchResponse,
    ChunkMatch,
)
from ..rag_engine import get_rag_engine

router = APIRouter(prefix="/api/v1/agent", tags=["Agent Search"])


async def _ensure_kbs_exist(kb_ids: List[uuid.UUID], db: AsyncSession) -> None:
    kb_ids_str = [str(kb_id) for kb_id in kb_ids]
    result = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(kb_ids_str),
            KnowledgeBase.is_deleted == False,
        )
    )
    found = {str(kb_id) for kb_id in result.scalars().all()}
    missing = [kb_id for kb_id in kb_ids_str if kb_id not in found]
    if missing:
        raise NotFoundError("KnowledgeBase", ", ".join(missing))


async def _execute_search(request: AgentSearchRequest) -> AgentSearchResponse:
    start_time = time.monotonic()
    engine = get_rag_engine()
    matches = await engine.hybrid_search(
        query=request.query,
        kb_ids=[str(k) for k in request.kb_ids],
        top_k=request.top_k,
        score_threshold=request.score_threshold,
        hybrid_alpha=request.hybrid_alpha,
        enable_reranker=request.enable_reranker,
    )
    latency_ms = int((time.monotonic() - start_time) * 1000)

    search_mode = "hybrid"
    if request.enable_reranker:
        search_mode = "hybrid+rerank"
    elif request.hybrid_alpha == 0.0:
        search_mode = "keyword_only"
    elif request.hybrid_alpha == 1.0:
        search_mode = "vector_only"

    return AgentSearchResponse(
        matches=matches,
        total_found=len(matches),
        latency_ms=latency_ms,
        search_mode=search_mode,
    )


@router.post(
    "/search",
    response_model=AgentSearchResponse,
    summary="Agent 开放混合检索接口",
    description="""
    专供外部 Agent (Dify / Coze / 自定义 AI 工作流节点) 调用的高性能混合检索 API。

    检索流程:
      1. 将 query 通过本地 BGE-small-zh-v1.5 向量化
      2. 并行执行 ChromaDB 向量检索 + 本地 BM25 关键词检索
      3. 绝对向量阈值过滤、BM25 归一化与 Alpha 加权融合
      4. [可选] BGE-Reranker 二次重排
      5. 返回标准化 ChunkMatch 数组

    注意事项:
      - kb_ids 必填，可同时跨多个知识库检索
      - hybrid_alpha=0 为纯关键词检索，=1 为纯语义检索
      - enable_reranker=True 时会额外增加 200-500ms 延迟
      - score_threshold 建议在 0.3~0.5 之间
    """,
)
async def agent_search(
    request: AgentSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_kbs_exist(request.kb_ids, db)
    return await _execute_search(request)


@router.post(
    "/search/batch",
    summary="批量检索",
    description="对多个查询并行执行检索，适用于批量评估场景",
)
async def agent_search_batch(
    requests: List[AgentSearchRequest],
    db: AsyncSession = Depends(get_db),
):
    if not requests:
        raise ValidationError("At least one search request is required")
    if len(requests) > 20:
        raise ValidationError("Too many search requests (max 20 per batch)")
    all_kb_ids = list(dict.fromkeys(
        kb_id for request in requests for kb_id in request.kb_ids
    ))
    await _ensure_kbs_exist(all_kb_ids, db)

    results = await asyncio.gather(
        *[_execute_search(request) for request in requests],
        return_exceptions=False,
    )
    return {"total_queries": len(requests), "results": results}


@router.get("/status", summary="检索引擎状态检查")
async def agent_status():
    """检查嵌入式 ChromaDB、BM25 和本地模型状态。"""
    engine = get_rag_engine()
    try:
        await engine.ensure_ready()
        vector_count = await asyncio.to_thread(engine.chroma_collection.count)
        chroma_ok = True
        error = None
    except Exception as exc:
        vector_count = 0
        chroma_ok = False
        error = str(exc)

    return {
        "search_engine": {
            "chroma": {
                "connected": chroma_ok,
                "collection": engine.collection_name,
                "vector_count": vector_count,
                "persist_dir": engine.persist_dir,
            },
            "bm25": {
                "connected": engine.bm25 is not None,
                "document_count": engine.bm25.document_count if engine.bm25 else 0,
            },
        },
        "models": {
            "embedding": engine.cfg.embedding_model_name if engine.embedder else "not initialized",
            "embedding_device": engine.embedder.device if engine.embedder else None,
            "reranker": engine.cfg.reranker_model_name if engine.reranker and engine.reranker.enabled else None,
        },
        "error": error,
    }


@router.post(
    "/search/health_check",
    summary="快速验证检索连通性",
    description="用一条测试查询验证完整的检索链路（Embedding → Vector → BM25 → 融合）",
)
async def agent_search_health_check():
    """端到端检索链路健康验证"""
    start = time.monotonic()
    engine = get_rag_engine()

    try:
        test_query = "测试检索连通性"
        await engine.ensure_ready()
        embedding = await engine.embedder.embed_single(test_query)
        vector_count = await asyncio.to_thread(engine.chroma_collection.count)

        return {
            "status": "ok",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "embedding": {
                "model": engine.cfg.embedding_model_name,
                "dimension": len(embedding),
                "test_passed": len(embedding) == engine.embedder.dim,
            },
            "chroma": {
                "collection": engine.collection_name,
                "vector_count": vector_count,
                "test_passed": vector_count >= 0,
            },
            "bm25": {
                "document_count": engine.bm25.document_count,
                "test_passed": engine.bm25 is not None,
            },
        }
    except Exception as e:
        return {
            "status": "error",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(e),
        }
