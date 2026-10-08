"""Explicit operator command; registration never claims legacy resources."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID,uuid4

from sqlalchemy import event,select,text
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine

from .config import AppSettings
from .models import AuditEvent,Membership,User


async def claim_workspace(db,workspace_id,owner_email,*,apply=False):
    """Caller begins a write transaction (BEGIN IMMEDIATE on SQLite) for apply."""
    if workspace_id != 'default':
        workspace_id=str(UUID(workspace_id))
    elif db.bind.dialect.name != 'sqlite':
        raise ValueError('The legacy default ID is supported only on SQLite')
    owner=await db.scalar(select(User).where(User.email==owner_email.strip().lower(),User.is_active.is_(True)))
    if owner is None:
        raise ValueError('Register an active SaaS owner account in this database first')
    query='SELECT * FROM workspaces WHERE id=:id'
    if apply and db.bind.dialect.name!='sqlite':
        query+=' FOR UPDATE'
    workspace=(await db.execute(text(query),{'id':workspace_id})).mappings().first()
    if workspace is None or workspace['is_deleted']:
        raise ValueError('Legacy workspace not found')
    if await db.scalar(text('SELECT count(*) FROM saas_memberships WHERE workspace_id=:id'),{'id':workspace_id}):
        raise ValueError('Workspace is already claimed')
    target=str(uuid4()) if workspace_id=='default' else workspace_id
    knowledge_bases=await db.scalar(text('SELECT count(*) FROM knowledge_bases WHERE workspace_id=:id'),{'id':workspace_id})
    documents=await db.scalar(text('SELECT count(*) FROM documents d JOIN knowledge_bases k ON d.kb_id=k.id WHERE k.workspace_id=:id'),{'id':workspace_id})
    result={'source_workspace_id':workspace_id,'target_workspace_id':target,
            'knowledge_bases':knowledge_bases,'documents':documents,'applied':apply}
    if not apply:
        return result
    # Add constraints without deleting or deduplicating operator data.
    await db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_kb_file_hash ON documents (kb_id,file_hash)'))
    await db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_doc_index ON document_chunks (doc_id,chunk_index)'))
    if workspace_id=='default':
        await db.execute(text('''INSERT INTO workspaces
            (id,name,description,department,owner_id,is_deleted,created_at,updated_at)
            SELECT :target,name,description,department,owner_id,is_deleted,created_at,updated_at
            FROM workspaces WHERE id=:source'''),{'target':target,'source':workspace_id})
        await db.execute(text('UPDATE knowledge_bases SET workspace_id=:target WHERE workspace_id=:source'),{'target':target,'source':workspace_id})
        await db.execute(text('DELETE FROM workspaces WHERE id=:source'),{'source':workspace_id})
    await db.execute(text('UPDATE workspaces SET owner_id=:owner WHERE id=:target'),{'owner':owner.id,'target':target})
    db.add(Membership(workspace_id=target,user_id=owner.id,role='owner'))
    db.add(AuditEvent(workspace_id=target,user_id=owner.id,action='migration.legacy_claimed',
                      details={'legacy_default_id':workspace_id=='default','knowledge_bases':knowledge_bases,'documents':documents}))
    await db.commit()
    return result


async def run(args):
    settings=AppSettings(_env_file=None,_env_prefix='KNOWLEDGE_MIGRATION_ISOLATED_',database_url=args.database_url)
    prefix='sqlite+aiosqlite:///'
    if settings.database_url.startswith(prefix):
        filename=settings.database_url[len(prefix):]
        if filename==':memory:' or not Path(filename).is_file():
            raise ValueError('Migration requires an existing database file')
    engine=create_async_engine(settings.database_url)
    if engine.dialect.name=='sqlite':
        @event.listens_for(engine.sync_engine,'connect')
        def sqlite_constraints(connection,record):
            cursor=connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.execute('PRAGMA busy_timeout=10000')
            cursor.close()
    try:
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            if args.apply and engine.dialect.name=='sqlite':
                await db.execute(text('BEGIN IMMEDIATE'))
            result=await claim_workspace(db,args.workspace_id,args.owner_email,apply=args.apply)
            print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        await engine.dispose()


def main():
    parser=argparse.ArgumentParser(description='Claim one unowned legacy workspace for an existing SaaS account; dry run by default.')
    parser.add_argument('--database-url',default=os.environ.get('DATABASE_URL'))
    parser.add_argument('--workspace-id',required=True)
    parser.add_argument('--owner-email',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.database_url:
        parser.error('Provide DATABASE_URL or --database-url explicitly')
    try:
        asyncio.run(run(args))
    except ValueError as error:
        parser.exit(1,f'Migration refused: {error}\n')
    except Exception as error:
        parser.exit(1,f'Migration failed ({type(error).__name__}); verify schema and reconcile duplicate rows without deleting source data.\n')


if __name__=='__main__':
    main()
