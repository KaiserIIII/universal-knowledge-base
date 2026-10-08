"""Organization-scoped conversation restoration, export and idempotent feedback."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .conversation_models import Conversation, Feedback, Message
from .dependencies import get_access, get_db, valid_id
from .models import utcnow

router = APIRouter(prefix='/api/v1')


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='New conversation', min_length=1, max_length=200)


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rating: Literal['positive', 'negative']
    reason: str = Field(default='', max_length=1000)


async def scoped_conversation(db, access, conversation_id):
    row = await db.scalar(select(Conversation).where(Conversation.id == valid_id(conversation_id),
        Conversation.workspace_id == access.workspace_id, Conversation.is_deleted.is_(False)))
    if row is None:
        raise HTTPException(404, 'Resource not found')
    return row


def conversation_dict(row):
    return {'id': row.id, 'title': row.title, 'created_at': row.created_at, 'updated_at': row.updated_at}


def message_dict(row):
    return {'id': row.id, 'role': row.role, 'content': row.content, 'status': row.status,
            'sources': row.sources, 'latency_ms': row.latency_ms, 'error': row.error,
            'invalid_citations': row.invalid_citations, 'usage': row.usage, 'created_at': row.created_at}


@router.get('/conversations')
async def list_conversations(limit: int = 100, offset: int = 0, db=Depends(get_db), access=Depends(get_access)):
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise HTTPException(422, 'Invalid pagination')
    rows = (await db.scalars(select(Conversation).where(Conversation.workspace_id == access.workspace_id,
        Conversation.is_deleted.is_(False)).order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit).offset(offset))).all()
    return {'conversations': [conversation_dict(row) for row in rows]}


@router.post('/conversations', status_code=201)
async def create_conversation(body: ConversationInput, db=Depends(get_db), access=Depends(get_access)):
    row = Conversation(workspace_id=access.workspace_id, created_by=access.user_id, title=body.title.strip() or 'New conversation')
    db.add(row)
    await db.commit()
    return conversation_dict(row)


@router.get('/conversations/{conversation_id}')
async def get_conversation(conversation_id: str, before: int | None = None, limit: int = 200,
                           db=Depends(get_db), access=Depends(get_access)):
    row = await scoped_conversation(db, access, conversation_id)
    if not 1 <= limit <= 500 or (before is not None and before < 1):
        raise HTTPException(422, 'Invalid pagination')
    query = select(Message).where(Message.conversation_id == row.id)
    if before is not None:
        query = query.where(Message.position < before)
    messages = list((await db.scalars(query.order_by(Message.position.desc()).limit(limit + 1))).all())
    more = len(messages) > limit
    messages = list(reversed(messages[:limit]))
    return {**conversation_dict(row), 'messages': [message_dict(m) for m in messages],
            'next_before': messages[0].position if more else None}


@router.patch('/conversations/{conversation_id}')
async def update_conversation(conversation_id: str, body: ConversationInput, db=Depends(get_db), access=Depends(get_access)):
    row = await scoped_conversation(db, access, conversation_id)
    row.title, row.updated_at = body.title.strip() or 'New conversation', utcnow()
    await db.commit()
    return conversation_dict(row)


@router.delete('/conversations/{conversation_id}', status_code=204)
async def delete_conversation(conversation_id: str, db=Depends(get_db), access=Depends(get_access)):
    row = await scoped_conversation(db, access, conversation_id)
    if await db.scalar(select(Message.id).where(Message.conversation_id == row.id, Message.status == 'running').limit(1)):
        raise HTTPException(409, 'Conversation has a running answer')
    row.is_deleted = True
    await db.commit()
    return Response(status_code=204)


@router.get('/conversations/{conversation_id}/export')
async def export_conversation(conversation_id: str, db=Depends(get_db), access=Depends(get_access)):
    row = await scoped_conversation(db, access, conversation_id)
    # Bounded export; request explicitly fails rather than silently truncating.
    messages = (await db.scalars(select(Message).where(Message.conversation_id == row.id).order_by(Message.position).limit(501))).all()
    if len(messages) > 500:
        raise HTTPException(413, 'Conversation export exceeds 500 messages')
    parts = ['# ' + row.title + '\n']
    for message in messages:
        parts.append(f'## {message.role} ({message.status})\n\n{message.content}\n')
        for index, source in enumerate(message.sources, 1):
            parts.append(f"- [{index}] {source['filename']} — source:{source['chunk_id']}\n")
    text = '\n'.join(parts)
    if len(text) > 2000000:
        raise HTTPException(413, 'Conversation export is too large')
    return PlainTextResponse(text, media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="conversation-{row.id}.md"'})


@router.post('/messages/{message_id}/feedback')
async def feedback(message_id: str, body: FeedbackInput, db=Depends(get_db), access=Depends(get_access)):
    message = await db.scalar(select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(
        Message.id == valid_id(message_id), Conversation.workspace_id == access.workspace_id, Conversation.is_deleted.is_(False), Message.role == 'assistant'))
    if message is None:
        raise HTTPException(404, 'Resource not found')
    if message.status not in {'completed', 'insufficient_evidence'}:
        raise HTTPException(409, 'Feedback requires a final answer')
    row = await db.scalar(select(Feedback).where(Feedback.message_id == message.id, Feedback.user_id == access.user_id))
    if row is None:
        row = Feedback(message_id=message.id, user_id=access.user_id)
        db.add(row)
    row.rating, row.reason, row.updated_at = body.rating, body.reason, utcnow()
    await db.commit()
    return {'id': row.id, 'message_id': row.message_id, 'rating': row.rating, 'reason': row.reason}
