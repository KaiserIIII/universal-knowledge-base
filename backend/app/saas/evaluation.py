"""Bounded, tenant-scoped document retrieval comparisons (no answer judging)."""
from time import perf_counter
from math import log2
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.models import Document, DocStatus
from .dependencies import AccessContext, get_access, get_db, valid_id
from .ingestion_models import IngestionJob
from .retrieval import scope_results, validate_kb_ids

router = APIRouter(prefix='/api/v1/evaluation', tags=['evaluation'])
ResourceId = Annotated[str, Field(min_length=1, max_length=64)]


def retrieval_metrics(retrieved_doc_ids, relevant_doc_ids):
    """Binary relevance metrics over the unique-document ranking/cutoff."""
    retrieved = list(dict.fromkeys(retrieved_doc_ids))
    relevant = set(relevant_doc_ids)
    matches = sum(doc_id in relevant for doc_id in retrieved)
    first_rank = next((rank for rank, doc_id in enumerate(retrieved, 1) if doc_id in relevant), None)
    dcg = sum(1 / log2(rank + 1) for rank, doc_id in enumerate(retrieved, 1) if doc_id in relevant)
    ideal = sum(1 / log2(rank + 1) for rank in range(1, min(len(relevant), len(retrieved)) + 1))
    return {'context_precision': matches / len(retrieved) if retrieved else 0.0,
            'context_recall': matches / len(relevant) if relevant else 0.0,
            'mrr': 1 / first_rank if first_rank else 0.0,
            'hit_rate': 1.0 if matches else 0.0, 'ndcg': dcg / ideal if ideal else 0.0}


class EvaluationQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    relevant_doc_ids: list[ResourceId] | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.query.strip():
            raise ValueError('query must not be blank')
        return self


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    top_k: int = Field(default=5, ge=1, le=20)
    hybrid_alpha: float = Field(default=.5, ge=0, le=1, allow_inf_nan=False)
    score_threshold: float = Field(default=.4, ge=0, le=1, allow_inf_nan=False)
    enable_reranker: bool = False

    @model_validator(mode='after')
    def nonblank(self):
        if not self.name.strip():
            raise ValueError('config name must not be blank')
        return self


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kb_ids: list[ResourceId] = Field(min_length=1, max_length=20)
    queries: list[EvaluationQuery] = Field(min_length=1, max_length=50)
    configs: list[RetrievalConfig] = Field(min_length=1, max_length=3)

    @model_validator(mode='after')
    def unique_names(self):
        names = [config.name for config in self.configs]
        if len(names) != len(set(names)):
            raise ValueError('config names must be unique')
        return self


async def validate_labels(db, kb_ids, queries):
    """Check every supplied label against selected live, completed documents."""
    labels = {valid_id(doc_id) for query in queries for doc_id in (query.relevant_doc_ids or [])}
    if not labels:
        return
    rows = (await db.scalars(select(Document.id).outerjoin(IngestionJob, IngestionJob.doc_id == Document.id)
        .where(Document.id.in_(labels), Document.kb_id.in_(kb_ids), Document.status == DocStatus.COMPLETED,
               (IngestionJob.id.is_(None) | (IngestionJob.action == 'ingest'))))).all()
    if set(rows) != labels:
        raise HTTPException(404, 'Resource not found')


@router.post('/retrieval')
async def compare_retrieval(body: EvaluationInput, request: Request,
                            access: AccessContext = Depends(get_access), db=Depends(get_db)):
    kb_ids = await validate_kb_ids(db, access, body.kb_ids)
    await validate_labels(db, kb_ids, body.queries)
    await db.rollback()  # label validation must not hold write locks over retrieval
    comparisons = []
    for config in body.configs:
        for query in body.queries:
            started = perf_counter()
            # Each comparison releases its short authorization read before the
            # adapter and rechecks current access/resources after it returns.
            try:
                access = await get_access(request, db)
                await validate_kb_ids(db, access, kb_ids)
                await validate_labels(db, kb_ids, body.queries)
            finally:
                await db.rollback()
            raw = await request.app.state.retriever.search(query=query.query, kb_ids=kb_ids,
                top_k=config.top_k, hybrid_alpha=config.hybrid_alpha,
                score_threshold=config.score_threshold, enable_reranker=config.enable_reranker)
            try:
                access = await get_access(request, db)
                await validate_kb_ids(db, access, kb_ids)
                await validate_labels(db, kb_ids, body.queries)
                results = (await scope_results(db, access, kb_ids, raw))[:config.top_k]
            finally:
                await db.rollback()
            duration_ms = round((perf_counter() - started) * 1000, 3)
            metrics = ({'context_precision': None, 'context_recall': None, 'mrr': None, 'hit_rate': None, 'ndcg': None}
                       if query.relevant_doc_ids is None else
                       retrieval_metrics((row['doc_id'] for row in results), query.relevant_doc_ids))
            # Bound response content even if a stored chunk exceeds ingestion defaults.
            public_results = [{**row, 'content': row['content'][:1000]} for row in results]
            comparisons.append({'config': config.model_dump(), 'query': query.query,
                                'results': public_results, 'metrics': metrics,
                                'duration_ms': duration_ms})
    return {'comparisons': comparisons}
