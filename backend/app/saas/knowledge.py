"""Tenant-scoped knowledge APIs; SQL is the authority for all visible data."""
import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from app.models import Document, DocumentChunk, DocStatus, KnowledgeBase, KBType
from app.parsing import NATIVE_FORMATS, ParserOptions, parser_catalog, validate_selection
from .dependencies import AccessContext, get_access, get_db, get_scoped_kb, require_role, valid_id
from .ingestion_models import IngestionJob, KnowledgeConfig
from .retrieval import scope_results, validate_kb_ids

router = APIRouter(prefix='/api/v1')
WRITERS = ('owner', 'admin', 'editor')
SUPPORTED = NATIVE_FORMATS


class KBCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    kb_type: KBType = KBType.GENERAL
    category_tags: list[str] = Field(default_factory=list, max_length=30)
    chunk_size: int = Field(default=1000, ge=50, le=10000)
    chunk_overlap: int = Field(default=200, ge=0, le=9999)
    use_unstructured: bool = False
    parser_config: ParserOptions = Field(default_factory=ParserOptions)

    @model_validator(mode='after')
    def overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError('chunk_overlap must be smaller than chunk_size')
        if not self.name.strip():
            raise ValueError('name must not be blank')
        return self


class KBUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    kb_type: KBType | None = None
    category_tags: list[str] | None = Field(default=None, max_length=30)
    chunk_size: int | None = Field(default=None, ge=50, le=10000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=9999)
    use_unstructured: bool | None = None
    parser_config: ParserOptions | None = None


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=10000)
    kb_ids: list[str] = Field(min_length=1, max_length=100)
    top_k: int = Field(default=5, ge=1, le=50)
    hybrid_alpha: float = Field(default=.5, ge=0, le=1)
    score_threshold: float = Field(default=.4, ge=0, le=1)
    enable_reranker: bool = False


async def kb_json(db, kb, settings):
    config = await db.get(KnowledgeConfig, kb.id)
    return {'id': kb.id, 'workspace_id': kb.workspace_id, 'name': kb.name,
        'description': kb.description, 'kb_type': kb.kb_type.value, 'category_tags': kb.category_tags,
        'doc_count': kb.doc_count, 'chunk_count': kb.chunk_count, 'total_chars': kb.total_chars,
        'created_at': kb.created_at, 'updated_at': kb.updated_at,
        'chunk_size': config.chunk_size if config else settings.chunk_default_size,
        'chunk_overlap': config.chunk_overlap if config else settings.chunk_default_overlap,
        'use_unstructured': config.use_unstructured if config else settings.use_unstructured,
        'parser_config': ParserOptions.model_validate(config.parser_config or {} if config else {}).model_dump(),
        'embedding_model': settings.embedding_model_name}


def document_json(doc):
    return {'id': doc.id, 'kb_id': doc.kb_id, 'filename': doc.filename, 'file_type': doc.file_type,
        'file_size_bytes': doc.file_size_bytes, 'status': doc.status.value,
        'error_msg': doc.error_msg, 'word_count': doc.word_count, 'page_count': doc.page_count,
        'chunk_count': doc.chunk_count, 'created_at': doc.created_at, 'updated_at': doc.updated_at}


def job_json(job):
    return {key: getattr(job, key) for key in ('id', 'doc_id', 'kb_id', 'filename', 'action', 'status', 'attempts', 'error', 'created_at', 'updated_at')}


def live_documents(kb_id):
    return select(Document).outerjoin(IngestionJob, IngestionJob.doc_id == Document.id).where(
        Document.kb_id == kb_id, IngestionJob.id.is_(None) | (IngestionJob.action == 'ingest'))


@router.get('/parsers')
async def list_parsers(access: AccessContext = Depends(get_access)):
    return {'parsers': parser_catalog(), 'ocr_available': False, 'vision_available': False}


async def get_scoped_document(db, access, kb_id, doc_id):
    await get_scoped_kb(db, access, kb_id)
    doc = await db.scalar(live_documents(kb_id).where(Document.id == valid_id(doc_id)))
    if doc is None:
        raise HTTPException(404, 'Resource not found')
    return doc


@router.get('/knowledge-bases')
async def list_kbs(request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    rows = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == access.workspace_id,
        KnowledgeBase.is_deleted.is_(False)).order_by(KnowledgeBase.created_at))).all()
    return [await kb_json(db, row, request.app.state.settings) for row in rows]


@router.post('/knowledge-bases', status_code=201)
async def create_kb(body: KBCreate, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    require_role(access, *WRITERS)
    kb = KnowledgeBase(workspace_id=access.workspace_id, name=body.name.strip(), description=body.description,
        kb_type=body.kb_type, category_tags=body.category_tags)
    db.add(kb)
    await db.flush()
    db.add(KnowledgeConfig(kb_id=kb.id, chunk_size=body.chunk_size, chunk_overlap=body.chunk_overlap,
        use_unstructured=body.use_unstructured, parser_config=body.parser_config.model_dump()))
    await db.commit()
    return await kb_json(db, kb, request.app.state.settings)


@router.get('/knowledge-bases/{kb_id}')
async def detail_kb(kb_id: str, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    return await kb_json(db, await get_scoped_kb(db, access, kb_id), request.app.state.settings)


@router.patch('/knowledge-bases/{kb_id}')
@router.put('/knowledge-bases/{kb_id}')
async def update_kb(kb_id: str, body: KBUpdate, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    kb = await get_scoped_kb(db, access, kb_id)
    require_role(access, *WRITERS)
    old = await kb_json(db, kb, request.app.state.settings)
    merged = {key: old[key] for key in KBCreate.model_fields}
    changes = body.model_dump(exclude_unset=True)
    merged.update(changes)
    try:
        validated = KBCreate.model_validate(merged)
    except ValueError:
        raise HTTPException(422, 'Invalid knowledge configuration')
    for key in ('name', 'description', 'kb_type', 'category_tags'):
        setattr(kb, key, getattr(validated, key))
    config = await db.get(KnowledgeConfig, kb.id)
    if config is None:
        config = KnowledgeConfig(kb_id=kb.id)
        db.add(config)
    for key in ('chunk_size', 'chunk_overlap', 'use_unstructured'):
        setattr(config, key, getattr(validated, key))
    config.parser_config = validated.parser_config.model_dump()
    await db.commit()
    return await kb_json(db, kb, request.app.state.settings)


async def mark_delete(db, access, doc):
    job = await db.scalar(select(IngestionJob).where(IngestionJob.doc_id == doc.id))
    if job is None:
        job = IngestionJob(workspace_id=access.workspace_id, kb_id=doc.kb_id, doc_id=doc.id,
            filename=doc.filename, source_path='')
        db.add(job)
    job.action, job.status, job.attempts, job.error = 'delete', 'queued', 0, None
    job.lease_token = job.lease_expires_at = None
    doc.status, doc.error_msg = DocStatus.FAILED, 'Deletion pending'


@router.delete('/knowledge-bases/{kb_id}')
async def delete_kb(kb_id: str, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    kb = await get_scoped_kb(db, access, kb_id)
    require_role(access, *WRITERS)
    docs = (await db.scalars(select(Document).where(Document.kb_id == kb.id))).all()
    for doc in docs:
        await mark_delete(db, access, doc)
    kb.is_deleted = True
    await db.commit()
    request.app.state.ingestion_worker.wake()
    return {'deleted': True}


@router.get('/kb/{kb_id}/documents')
async def list_documents(kb_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                         access: AccessContext = Depends(get_access), db=Depends(get_db)):
    await get_scoped_kb(db, access, kb_id)
    statement = live_documents(kb_id)
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    docs = (await db.scalars(statement.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {'items': [document_json(doc) for doc in docs], 'total': total, 'page': page, 'page_size': page_size}


@router.post('/kb/{kb_id}/documents', status_code=202)
async def upload_documents(kb_id: str, request: Request, files: list[UploadFile] = File(...),
                           access: AccessContext = Depends(get_access), db=Depends(get_db)):
    kb = await get_scoped_kb(db, access, kb_id)
    require_role(access, *WRITERS)
    settings = request.app.state.settings
    config = await db.get(KnowledgeConfig, kb.id)
    parser_options = ParserOptions.model_validate(config.parser_config or {} if config else {})
    if not files or len(files) > settings.max_files_per_batch:
        raise HTTPException(400, 'Invalid upload batch size')
    prepared, hashes, skipped = [], set(), 0
    # Validate the entire batch before any source write or database insert.
    for file in files:
        filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED or len(filename) > 512:
            raise HTTPException(422, 'Unsupported file type')
        try:
            validate_selection(filename, parser_options)
        except ValueError:
            raise HTTPException(422, 'Selected parser does not support this file type') from None
        data = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
        if len(data) > settings.max_upload_size_mb * 1024 * 1024:
            raise HTTPException(413, 'Upload exceeds size limit')
        if not data or not data.strip():
            raise HTTPException(400, 'Empty upload')
        digest = hashlib.sha256(data).hexdigest()
        existing = await db.scalar(select(Document.id).where(Document.kb_id == kb.id, Document.file_hash == digest))
        if digest in hashes or existing:
            skipped += 1
            continue
        hashes.add(digest)
        prepared.append((filename, suffix, data, digest))
    written, documents = [], []
    try:
        folder = Path(settings.upload_temp_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for filename, suffix, data, digest in prepared:
            doc_id = str(uuid4())
            path = folder / (doc_id + suffix)
            path.write_bytes(data)
            written.append(path)
            doc = Document(id=doc_id, kb_id=kb.id, file_hash=digest, filename=filename,
                file_type=suffix[1:], file_size_bytes=len(data), status=DocStatus.UPLOADING)
            db.add(doc)
            db.add(IngestionJob(workspace_id=access.workspace_id, kb_id=kb.id, doc_id=doc_id,
                filename=filename, source_path=str(path)))
            documents.append(doc)
        kb.doc_count += len(documents)
        await db.commit()
    except BaseException:
        await db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    request.app.state.ingestion_worker.wake()
    return {'accepted': len(documents), 'skipped': skipped, 'documents': [document_json(doc) for doc in documents]}


@router.get('/kb/{kb_id}/documents/{doc_id}')
async def detail_document(kb_id: str, doc_id: str, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    return document_json(await get_scoped_document(db, access, kb_id, doc_id))


@router.get('/kb/{kb_id}/documents/{doc_id}/chunks')
async def document_chunks(kb_id: str, doc_id: str, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    doc = await get_scoped_document(db, access, kb_id, doc_id)
    rows = (await db.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == doc.id).order_by(DocumentChunk.chunk_index))).all()
    return [{'id': row.id, 'doc_id': row.doc_id, 'chunk_index': row.chunk_index,
        'content': row.content, 'token_count': row.token_count,
        'metadata': {'kb_id': kb_id, 'chunk_index': row.chunk_index,
            **{key: row.metadata_[key] for key in ('source_sha256', 'parser_mode') if key in (row.metadata_ or {})}}} for row in rows]


@router.delete('/kb/{kb_id}/documents/{doc_id}')
async def delete_document(kb_id: str, doc_id: str, request: Request,
                          access: AccessContext = Depends(get_access), db=Depends(get_db)):
    doc = await get_scoped_document(db, access, kb_id, doc_id)
    require_role(access, *WRITERS)
    await mark_delete(db, access, doc)
    await db.commit()
    request.app.state.ingestion_worker.wake()
    return {'deleted': True}


@router.get('/ingestion-jobs')
async def list_jobs(access: AccessContext = Depends(get_access), db=Depends(get_db)):
    jobs = (await db.scalars(select(IngestionJob).where(IngestionJob.workspace_id == access.workspace_id)
        .order_by(IngestionJob.created_at.desc()).limit(200))).all()
    return {'jobs': [job_json(job) for job in jobs]}


@router.post('/ingestion-jobs/{job_id}/retry')
async def retry_job(job_id: str, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    job = await db.scalar(select(IngestionJob).where(IngestionJob.id == valid_id(job_id), IngestionJob.workspace_id == access.workspace_id))
    if job is None:
        raise HTTPException(404, 'Resource not found')
    require_role(access, *WRITERS)
    if job.status != 'failed' or job.attempts >= request.app.state.settings.ingestion_max_attempts:
        raise HTTPException(409, 'Job cannot be retried')
    if job.action == 'ingest':
        await get_scoped_kb(db, access, job.kb_id)
        if not Path(job.source_path).is_file():
            raise HTTPException(409, 'Source unavailable')
    job.status, job.error = 'queued', None
    job.lease_token = job.lease_expires_at = None
    await db.commit()
    request.app.state.ingestion_worker.wake()
    return job_json(job)


@router.post('/agent/search')
async def search(body: SearchInput, request: Request, access: AccessContext = Depends(get_access), db=Depends(get_db)):
    kb_ids = await validate_kb_ids(db, access, body.kb_ids)
    await db.rollback()  # release POST serialization before awaiting retrieval
    results = await request.app.state.retriever.search(**{**body.model_dump(), 'kb_ids': kb_ids})
    try:
        access = await get_access(request, db)  # current credentials and membership
        await validate_kb_ids(db, access, kb_ids)
        return {'results': (await scope_results(db, access, kb_ids, results))[:body.top_k]}
    finally:
        await db.rollback()
