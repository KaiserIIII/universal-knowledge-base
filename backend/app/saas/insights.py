"""SQL-authoritative aggregate operations; transport failures are never knowledge gaps."""
from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from app.models import Document, DocStatus, KnowledgeBase
from .conversation_models import Conversation, Feedback, Message
from .dependencies import get_access, get_db

router = APIRouter(prefix='/api/v1')


@router.get('/insights')
async def insights(db=Depends(get_db), access=Depends(get_access)):
    documents = (await db.execute(select(Document.status, func.count(Document.id)).join(KnowledgeBase,
        KnowledgeBase.id == Document.kb_id).where(KnowledgeBase.workspace_id == access.workspace_id,
        KnowledgeBase.is_deleted.is_(False)).group_by(Document.status))).all()
    answers = (await db.execute(select(Message.status, func.count(Message.id)).join(Conversation,
        Conversation.id == Message.conversation_id).where(Conversation.workspace_id == access.workspace_id,
        Conversation.is_deleted.is_(False), Message.role == 'assistant').group_by(Message.status))).all()
    latency = await db.scalar(select(func.avg(Message.latency_ms)).join(Conversation,
        Conversation.id == Message.conversation_id).where(Conversation.workspace_id == access.workspace_id,
        Conversation.is_deleted.is_(False), Message.role == 'assistant', Message.status == 'completed'))
    negative = select(Feedback.message_id).where(Feedback.message_id == Message.id, Feedback.rating == 'negative').exists()
    negative = negative.correlate(Message)
    reason = case((Message.status == 'insufficient_evidence', 'insufficient_evidence'), else_='negative_feedback')
    rows = (await db.execute(select(Message.query, reason.label('reason'), func.count(Message.id), func.max(Message.finished_at)).join(
        Conversation, Conversation.id == Message.conversation_id).where(Conversation.workspace_id == access.workspace_id,
        Conversation.is_deleted.is_(False), Message.role == 'assistant',
        (Message.status == 'insufficient_evidence') | ((Message.status == 'completed') & negative)).group_by(Message.query, reason)
        .order_by(func.max(Message.finished_at).desc()).limit(100))).all()
    doc_counts = {'completed': 0, 'failed': 0, **{status.value: count for status, count in documents}}
    answer_counts = {'completed': 0, 'insufficient_evidence': 0, 'error': 0, 'canceled': 0, 'running': 0, **dict(answers)}
    return {'documents': doc_counts, 'answers': answer_counts, 'latency_ms': {'average': round(latency, 3) if latency is not None else None},
            'gaps': [{'query': query, 'reason': why, 'count': count, 'last_seen': seen} for query, why, count, seen in rows]}
