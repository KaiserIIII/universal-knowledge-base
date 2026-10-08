"""Use parsed local files as evidence; SQL controls scope and readiness."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import DocumentChunk, DocStatus
from .knowledge import get_scoped_document
from .retrieval import validate_kb_ids


async def validate_files(db, access, kb_ids, doc_ids, *, complete=True):
    if kb_ids:
        await validate_kb_ids(db, access, kb_ids)
    if complete and (not kb_ids or not doc_ids):
        raise HTTPException(422, 'Select knowledge bases and ready documents')
    documents = []
    for doc_id in dict.fromkeys(doc_ids):
        # Resolve against all selected libraries without revealing foreign IDs.
        found = None
        for kb_id in kb_ids:
            try:
                found = await get_scoped_document(db, access, kb_id, doc_id)
                break
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
        if found is None:
            raise HTTPException(404, 'Resource not found')
        if found.status != DocStatus.COMPLETED:
            raise HTTPException(409, 'Document parsing is not complete')
        documents.append(found)
    return documents


async def file_sources(db, access, config):
    documents = await validate_files(db, access, config['kb_ids'], config['doc_ids'])
    sources = []
    for document in documents:
        remaining = config['top_k'] - len(sources)
        if remaining <= 0:
            break
        chunks = (await db.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == document.id)
            .order_by(DocumentChunk.chunk_index).limit(remaining))).all()
        for chunk in chunks:
            sources.append({'chunk_id': chunk.id, 'doc_id': document.id, 'filename': document.filename,
                'content': chunk.content, 'score': 1.0,
                'metadata': {'kb_id': document.kb_id, 'chunk_index': chunk.chunk_index,
                             'source_sha256': document.file_hash}})
    return sources
