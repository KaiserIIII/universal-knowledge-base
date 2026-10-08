"""Lazy local adapter and SQL-authoritative retrieval boundary."""
import asyncio
import math
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Document, DocumentChunk, DocStatus, KnowledgeBase
from .dependencies import get_scoped_kb, valid_id
from .ingestion_models import IngestionJob


class RAGRetriever:
    def __init__(self, settings):
        self.settings = settings
        self._engine = None
        self._reranker = None

    def _get_engine(self):
        if self._engine is None:
            try:
                from app.rag_engine import RAGEngine
                self._engine = RAGEngine(settings=self.settings)
            except Exception:
                raise HTTPException(503, 'Local retrieval dependencies unavailable') from None
        return self._engine

    async def search(self, query, kb_ids, top_k, hybrid_alpha, score_threshold, enable_reranker):
        if enable_reranker and not self.settings.use_reranker:
            raise HTTPException(503, 'Local reranking capability is disabled')
        try:
            engine = self._get_engine()
            matches = await engine.hybrid_search(query=query, kb_ids=kb_ids, top_k=top_k,
                hybrid_alpha=hybrid_alpha, score_threshold=score_threshold, enable_reranker=enable_reranker)
            return [match.model_dump(mode='json') for match in matches]
        except Exception:
            raise HTTPException(503, 'Local retrieval capability unavailable') from None

    async def rerank(self, query, results, top_k):
        if not self.settings.use_reranker:
            raise HTTPException(503, 'Local reranking capability is disabled')
        try:
            # Standalone reranking does not initialize embeddings/index stores.
            from app.rag_engine import LocalReranker
            if self._reranker is None:
                self._reranker = LocalReranker(settings=self.settings)
            return await self._reranker.rerank(query, [dict(row) for row in results], top_k)
        except Exception:
            raise HTTPException(503, 'Local reranking capability unavailable') from None

    async def parse(self, file_path, filename, kb_id, doc_id, chunk_size, chunk_overlap, use_unstructured=False):
        try:
            from app.rag_engine import DocumentProcessor
            processor = DocumentProcessor(chunk_size, chunk_overlap, settings=self.settings,
                use_unstructured=use_unstructured)
            return await asyncio.to_thread(processor.parse_and_chunk, file_path, filename, kb_id, doc_id)
        except Exception:
            raise HTTPException(503, 'Local parsing capability unavailable') from None

    async def upsert(self, chunks):
        try:
            return await self._get_engine().ingest_document(chunks)
        except Exception:
            raise HTTPException(503, 'Local indexing capability unavailable') from None

    async def delete_document(self, doc_id):
        try:
            return await self._get_engine().delete_document_chunks(doc_id)
        except Exception:
            raise HTTPException(503, 'Local index cleanup unavailable') from None

    async def delete_kb(self, kb_id):
        try:
            return await self._get_engine().delete_kb_chunks(kb_id)
        except Exception:
            raise HTTPException(503, 'Local index cleanup unavailable') from None

    async def close(self):
        if self._engine is not None:
            await self._engine.close()


async def validate_kb_ids(db, access, kb_ids):
    """Validate every selected ID before invoking any retrieval capability."""
    if not kb_ids:
        raise HTTPException(422, 'Select at least one knowledge base')
    result = []
    for kb_id in dict.fromkeys(kb_ids):
        result.append((await get_scoped_kb(db, access, kb_id)).id)
    return result


async def scope_results(db, access, kb_ids, results):
    """Reconstruct public content from SQL; never trust index content or metadata."""
    safe, seen = [], set()
    for hit in results:
        if not isinstance(hit, dict):
            continue
        try:
            chunk_id, doc_id = valid_id(hit.get('chunk_id')), valid_id(hit.get('doc_id'))
            claimed_kb = valid_id((hit.get('metadata') or {}).get('kb_id'))
            score = float(hit.get('score', 0))
        except (HTTPException, ValueError, TypeError, AttributeError):
            continue
        if claimed_kb not in kb_ids or chunk_id in seen or not math.isfinite(score):
            continue
        row = (await db.execute(select(DocumentChunk, Document).join(Document, Document.id == DocumentChunk.doc_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .outerjoin(IngestionJob, IngestionJob.doc_id == Document.id)
            .where(DocumentChunk.id == chunk_id, Document.id == doc_id, Document.kb_id == claimed_kb,
                KnowledgeBase.workspace_id == access.workspace_id, KnowledgeBase.is_deleted.is_(False),
                Document.status == DocStatus.COMPLETED,
                (IngestionJob.id.is_(None) | (IngestionJob.action == 'ingest'))))).first()
        if row is None:
            continue
        chunk, doc = row
        safe.append({'chunk_id': chunk.id, 'doc_id': doc.id, 'content': chunk.content,
            'filename': doc.filename, 'score': min(max(score, 0), 1),
            'metadata': {'kb_id': doc.kb_id, 'chunk_index': chunk.chunk_index}})
        seen.add(chunk_id)
    return safe


async def scoped_search(db, access, retriever, *, query, kb_ids, top_k=5, hybrid_alpha=.5,
                        score_threshold=.4, enable_reranker=False):
    kb_ids = await validate_kb_ids(db, access, kb_ids)
    results = await retriever.search(query=query, kb_ids=kb_ids, top_k=top_k,
        hybrid_alpha=hybrid_alpha, score_threshold=score_threshold, enable_reranker=enable_reranker)
    return (await scope_results(db, access, kb_ids, results))[:top_k]
