"""Durable lease queue. SQL publication is fenced; index writes are idempotent.

There is deliberately no claim of distributed exactly-once index execution.
Expired workers may repeat writes; relational visibility remains authoritative.
"""
import asyncio
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid4, uuid5

from sqlalchemy import delete, func, or_, select, text
from app.models import Document, DocumentChunk, DocStatus, KnowledgeBase
from .dependencies import lock_workspace
from .ingestion_models import IngestionJob, KnowledgeConfig
from .models import utcnow
from .security import aware


class IngestionWorker:
    def __init__(self, session_factory, settings, retriever):
        self.sessions, self.settings, self.retriever = session_factory, settings, retriever
        self._event, self._lock = asyncio.Event(), asyncio.Lock()
        self._task = None

    def start(self):
        if self.settings.ingestion_worker_enabled:
            self._task = asyncio.create_task(self._loop())

    def wake(self):
        self._event.set()

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self):
        while True:
            self._event.clear()
            try:
                await self.drain()
            except Exception:
                # Transient DB failure must not permanently kill queue polling.
                pass
            try:
                await asyncio.wait_for(self._event.wait(), self.settings.ingestion_poll_seconds)
            except TimeoutError:
                pass

    async def _begin(self, db):
        if db.bind.dialect.name == 'sqlite':
            await db.execute(text('BEGIN IMMEDIATE'))

    def _source(self, path):
        source = Path(path).resolve()
        root = Path(self.settings.upload_temp_dir).resolve()
        if not source.is_relative_to(root) or source == root:
            raise ValueError('Invalid retained source')
        return source

    async def _claim(self):
        async with self.sessions() as db:
            await self._begin(db)
            now = utcnow()
            eligible = or_(
                IngestionJob.status == 'queued',
                (IngestionJob.status == 'running') & or_(IngestionJob.lease_expires_at <= now,
                                                       IngestionJob.lease_expires_at.is_(None)))
            candidate = (await db.execute(select(IngestionJob.id, IngestionJob.workspace_id)
                .where(eligible).order_by(IngestionJob.created_at).limit(1))).first()
            if candidate is None:
                return None
            # Use the same lock order as HTTP mutations: workspace, then job.
            await lock_workspace(db, candidate.workspace_id)
            row = await db.scalar(select(IngestionJob).where(IngestionJob.id == candidate.id, eligible)
                .with_for_update(skip_locked=True))
            if row is None:
                return None
            if row.attempts >= self.settings.ingestion_max_attempts:
                row.status, row.error = 'failed', 'Attempt limit reached'
                row.lease_token = row.lease_expires_at = None
                doc = await db.get(Document, row.doc_id)
                if doc is not None:
                    doc.status, doc.error_msg = DocStatus.FAILED, row.error
                await db.commit()
                return {'exhausted': True}
            row.attempts += 1
            row.status, row.error, row.lease_token = 'running', None, str(uuid4())
            row.lease_expires_at = now + timedelta(seconds=self.settings.ingestion_lease_seconds)
            doc, kb = await db.get(Document, row.doc_id), await db.get(KnowledgeBase, row.kb_id)
            config = await db.get(KnowledgeConfig, row.kb_id)
            if doc is not None and row.action == 'ingest':
                doc.status, doc.error_msg = DocStatus.PARSING, None
            job = {key: getattr(row, key) for key in ('id', 'workspace_id', 'kb_id', 'doc_id', 'filename', 'action', 'source_path', 'lease_token')}
            job.update(chunk_size=config.chunk_size if config else self.settings.chunk_default_size,
                       chunk_overlap=config.chunk_overlap if config else self.settings.chunk_default_overlap,
                       use_unstructured=config.use_unstructured if config else self.settings.use_unstructured,
                       valid=doc is not None and kb is not None and not kb.is_deleted)
            await db.commit()
            return job

    async def _owned(self, db, job):
        await lock_workspace(db, job['workspace_id'])
        row = await db.scalar(select(IngestionJob).where(IngestionJob.id == job['id']).with_for_update())
        if (row is None or row.status != 'running' or row.lease_token != job['lease_token']
                or row.action != job['action'] or row.lease_expires_at is None
                or aware(row.lease_expires_at) <= utcnow()):
            return None
        return row

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.ingestion_lease_seconds / 3)
            async with self.sessions() as db:
                await self._begin(db)
                row = await self._owned(db, job)
                if row is None:
                    return
                row.lease_expires_at = utcnow() + timedelta(seconds=self.settings.ingestion_lease_seconds)
                await db.commit()

    async def _counts(self, db, kb_id):
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            return
        kb.doc_count = await db.scalar(select(func.count(Document.id)).where(Document.kb_id == kb_id))
        kb.chunk_count, kb.total_chars = (await db.execute(select(func.count(DocumentChunk.id),
            func.coalesce(func.sum(func.length(DocumentChunk.content)), 0)).join(Document)
            .where(Document.kb_id == kb_id))).one()

    async def _run(self, job):
        chunks = []
        if job['action'] == 'ingest':
            if not job['valid']:
                raise ValueError('Document unavailable')
            source = self._source(job['source_path'])
            parsed = await self.retriever.parse(file_path=str(source), filename=job['filename'],
                kb_id=job['kb_id'], doc_id=job['doc_id'], chunk_size=job['chunk_size'],
                chunk_overlap=job['chunk_overlap'], use_unstructured=job['use_unstructured'])
            for item in parsed:
                content = item.get('content', '').strip()
                if not content:
                    continue
                index = len(chunks)
                chunks.append({'chunk_id': str(uuid5(NAMESPACE_DNS, f"{job['doc_id']}:{index}")),
                    'doc_id': job['doc_id'], 'kb_id': job['kb_id'], 'filename': job['filename'],
                    'chunk_index': index, 'content': content, 'token_count': len(content) // 2,
                    'metadata': {'kb_id': job['kb_id'], 'chunk_index': index}})
            if not chunks:
                raise ValueError('No extractable content')
            async with self.sessions() as db:
                await self._begin(db)
                if await self._owned(db, job) is None:
                    return
                doc = await db.get(Document, job['doc_id'])
                doc.status = DocStatus.EMBEDDING
                await db.commit()
            # Clear any incomplete old attempt before idempotent replacement.
            await self.retriever.delete_document(job['doc_id'])
            await self.retriever.upsert(chunks)
        else:
            await self.retriever.delete_document(job['doc_id'])
        async with self.sessions() as db:
            await self._begin(db)
            row = await self._owned(db, job)
            if row is None:
                return
            doc = await db.get(Document, job['doc_id'])
            await db.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == job['doc_id']))
            if job['action'] == 'ingest':
                if doc is None:
                    raise ValueError('Document unavailable')
                for chunk in chunks:
                    db.add(DocumentChunk(id=chunk['chunk_id'], doc_id=doc.id,
                        chunk_index=chunk['chunk_index'], content=chunk['content'],
                        token_count=chunk['token_count'], metadata_=chunk['metadata']))
                doc.status, doc.error_msg, doc.chunk_count = DocStatus.COMPLETED, None, len(chunks)
                doc.word_count = sum(len(chunk['content'].split()) for chunk in chunks)
            elif doc is not None:
                await db.delete(doc)
            row.status, row.error = 'completed', None
            row.lease_token = row.lease_expires_at = None
            await db.flush()
            await self._counts(db, job['kb_id'])
            await db.commit()
        if job['source_path']:
            with suppress(OSError):
                self._source(job['source_path']).unlink(missing_ok=True)

    async def _failure(self, job):
        # Index cleanup is best effort. Failed SQL documents remain invisible;
        # the next explicit retry clears any residue before inserting again.
        async with self.sessions() as db:
            await self._begin(db)
            row = await self._owned(db, job)
            if row is None:
                return
            # Keep the organization/job fence while cleaning: a stale failed
            # worker must never delete a newer attempt's successful index write.
            if job['action'] == 'ingest':
                with suppress(Exception):
                    await asyncio.wait_for(self.retriever.delete_document(job['doc_id']),
                                           self.settings.ingestion_job_timeout_seconds)
            row.status, row.error = 'failed', 'Processing failed; retry or contact the administrator'
            row.lease_token = row.lease_expires_at = None
            doc = await db.get(Document, row.doc_id)
            if doc is not None:
                doc.status, doc.error_msg = DocStatus.FAILED, row.error
            await db.commit()

    async def drain(self):
        async with self._lock:
            while job := await self._claim():
                if job.get('exhausted'):
                    continue
                heartbeat = asyncio.create_task(self._heartbeat(job))
                try:
                    await asyncio.wait_for(self._run(job), self.settings.ingestion_job_timeout_seconds)
                except asyncio.CancelledError:
                    # Leave committed running lease for restart recovery.
                    raise
                except Exception:
                    await self._failure(job)
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
