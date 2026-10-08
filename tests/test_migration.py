"""Operator-only claims never run on registration or alter vector KB IDs."""
from uuid import uuid4
from pathlib import Path
from sqlalchemy import select,text,inspect
from sqlalchemy.ext.asyncio import create_async_engine
import httpx
from tests.support import ApiTestCase
from app.models import Base,Document,DocumentChunk,DocStatus,KnowledgeBase,Workspace


class MigrationTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register('operator@example.test')

    async def test_dry_run_is_read_only_and_claim_is_explicit(self):
        from app.saas.migrate_legacy import claim_workspace
        legacy=str(uuid4())
        async with self.app.state.session_factory() as db:
            db.add(Workspace(id=legacy,name='Legacy'))
            await db.commit()
            result=await claim_workspace(db,legacy,self.user['email'])
            self.assertFalse(result['applied'])
        self.assertEqual((await self.client.get(f'/api/v1/organizations/{legacy}')).status_code,404)
        async with self.app.state.session_factory() as db:
            await db.execute(text('BEGIN IMMEDIATE'))
            result=await claim_workspace(db,legacy,self.user['email'],apply=True)
        self.assertEqual(result['target_workspace_id'],legacy)
        self.assertEqual((await self.client.get(f'/api/v1/organizations/{legacy}')).status_code,200)

    async def test_claim_refuses_organization_with_existing_members(self):
        from app.saas.migrate_legacy import claim_workspace
        async with self.app.state.session_factory() as db:
            await db.execute(text('BEGIN IMMEDIATE'))
            with self.assertRaisesRegex(ValueError,'already claimed'):
                await claim_workspace(db,self.workspace_id,self.user['email'],apply=True)

    async def test_default_id_conversion_preserves_knowledge_ids(self):
        from app.saas.migrate_legacy import claim_workspace
        kb_id=str(uuid4())
        async with self.app.state.session_factory() as db:
            await db.execute(text("INSERT INTO workspaces (id,name,is_deleted,created_at,updated_at) VALUES ('default','Legacy',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            await db.execute(text("INSERT INTO knowledge_bases (id,workspace_id,name,kb_type,category_tags,doc_count,chunk_count,total_chars,is_deleted,created_at,updated_at) VALUES (:id,'default','Existing','GENERAL','[]',0,0,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),{'id':kb_id})
            await db.commit()
            await db.execute(text('BEGIN IMMEDIATE'))
            result=await claim_workspace(db,'default',self.user['email'],apply=True)
        async with self.app.state.session_factory() as db:
            kb=await db.get(KnowledgeBase,kb_id)
            self.assertEqual(kb.workspace_id,result['target_workspace_id'])
            self.assertEqual(kb.id,kb_id)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM workspaces WHERE id='default'")),0)

    async def test_startup_adds_saas_tables_without_replacing_legacy_records(self):
        from app.saas.application import create_app
        from app.saas.migrate_legacy import claim_workspace
        from app.saas.config import AppSettings
        folder=Path(self.temporary.name)
        database_url=f"sqlite+aiosqlite:///{(folder/'legacy-only.db').as_posix()}"
        engine=create_async_engine(database_url)
        kb_id,doc_id,chunk_id=(str(uuid4()) for _ in range(3))
        legacy_tables=[Workspace.__table__,KnowledgeBase.__table__,Document.__table__,DocumentChunk.__table__]
        try:
            async with engine.begin() as connection:
                await connection.run_sync(lambda sync:Base.metadata.create_all(sync,tables=legacy_tables))
                self.assertEqual(set(await connection.run_sync(lambda sync:inspect(sync).get_table_names())),{table.name for table in legacy_tables})
                await connection.execute(text("INSERT INTO workspaces (id,name,is_deleted,created_at,updated_at) VALUES ('default','Synthetic legacy',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                await connection.execute(text("INSERT INTO knowledge_bases (id,workspace_id,name,kb_type,category_tags,doc_count,chunk_count,total_chars,is_deleted,created_at,updated_at) VALUES (:id,'default','Synthetic existing','GENERAL','[]',1,1,21,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),{'id':kb_id})
                await connection.execute(Document.__table__.insert().values(id=doc_id,kb_id=kb_id,file_hash='synthetic-legacy-hash',filename='synthetic.txt',file_type='txt',status=DocStatus.COMPLETED,chunk_count=1))
                await connection.execute(DocumentChunk.__table__.insert().values(id=chunk_id,doc_id=doc_id,chunk_index=0,content='Synthetic legacy text'))
        finally:
            await engine.dispose()
        settings=AppSettings(_env_file=None,_env_prefix='KNOWLEDGE_TEST_LEGACY_ISOLATED_',database_url=database_url,upload_temp_dir=str(folder/'legacy-uploads'),cookie_secure=False,ingestion_worker_enabled=False)
        app=create_app(settings,self.retriever,self.llm,self.payment)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
                identity=await self.register_with(client,'legacy-operator@example.test')
                self.assertEqual((await client.get(f'/api/v1/knowledge-bases/{kb_id}')).status_code,404)
                async with app.state.session_factory() as db:
                    await db.execute(text('BEGIN IMMEDIATE'))
                    result=await claim_workspace(db,'default',identity['user']['email'],apply=True)
                client.headers['X-Workspace-ID']=result['target_workspace_id']
                self.assertEqual((await client.get(f'/api/v1/knowledge-bases/{kb_id}')).status_code,200)
                async with app.state.session_factory() as db:
                    self.assertEqual((await db.get(Document,doc_id)).kb_id,kb_id)
                    self.assertEqual((await db.get(DocumentChunk,chunk_id)).content,'Synthetic legacy text')
                    table_names=await db.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('saas_users','saas_conversations','saas_messages','saas_feedback')"))
                    self.assertEqual(table_names,4)
