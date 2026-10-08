"""Self-hosted operation preserves historical data without commercial modules."""
from pathlib import Path
from sqlalchemy import text, inspect
from tests.support import ApiTestCase


class OpenSourceTests(ApiTestCase):
    async def test_counts_are_unlimited_and_commercial_routes_are_absent(self):
        await self.register('open@example.test')
        for index in range(5):
            response = await self.client.post('/api/v1/knowledge-bases', json={'name': str(index)})
            self.assertEqual(response.status_code, 201)
        for path in ('/api/v1/billing', '/api/v1/billing/checkout', '/api/v1/billing/webhook'):
            self.assertEqual((await self.client.post(path, json={})).status_code, 404)
        async with self.app.state.engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
            self.assertNotIn('saas_subscriptions', names)

    async def test_old_required_workflow_reference_is_migrated_without_data_loss(self):
        from app.saas.schema_updates import upgrade_schema
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import event
        engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.temporary.name) / 'old.db').as_posix())
        self.addAsyncCleanup(engine.dispose)
        @event.listens_for(engine.sync_engine, 'connect')
        def enable_fk(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')
        async with engine.begin() as conn:
            await conn.execute(text('CREATE TABLE saas_answer_reservations (id VARCHAR(36) PRIMARY KEY, note TEXT)'))
            await conn.execute(text("INSERT INTO saas_answer_reservations VALUES ('old-id','historical')"))
            await conn.exec_driver_sql("CREATE TABLE saas_workflow_runs (id VARCHAR(36) PRIMARY KEY, reservation_id VARCHAR(36) NOT NULL UNIQUE REFERENCES saas_answer_reservations(id), custom_note TEXT COLLATE NOCASE UNIQUE DEFAULT ':unassigned', custom_length INT GENERATED ALWAYS AS (length(custom_note)) STORED)")
            await conn.execute(text("INSERT INTO saas_workflow_runs (id,reservation_id,custom_note) VALUES ('run-id','old-id','original answer')"))
            await conn.execute(text('CREATE INDEX retained_note ON saas_workflow_runs(custom_note)'))
            await conn.exec_driver_sql("CREATE TABLE audit_notes (note TEXT)")
            await conn.exec_driver_sql("CREATE TRIGGER retained_trigger AFTER INSERT ON saas_workflow_runs BEGIN INSERT INTO audit_notes VALUES (':created'); END")
        await upgrade_schema(engine)
        await upgrade_schema(engine)
        async with engine.begin() as conn:
            self.assertEqual((await conn.execute(text('SELECT * FROM saas_workflow_runs'))).one(), ('run-id','old-id','original answer',15))
            await conn.execute(text("INSERT INTO saas_workflow_runs (id,reservation_id,custom_note) VALUES ('new-id',NULL,'new answer')"))
            self.assertEqual(await conn.scalar(text("SELECT count(*) FROM saas_workflow_runs WHERE custom_note='ORIGINAL ANSWER'")), 1)
            self.assertEqual(await conn.scalar(text('SELECT note FROM saas_answer_reservations')), 'historical')
            self.assertEqual(await conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='retained_note'")), 1)
            self.assertEqual(await conn.scalar(text('PRAGMA foreign_keys')), 1)
            self.assertEqual(await conn.scalar(text('SELECT note FROM audit_notes')), ':created')
            await conn.exec_driver_sql("INSERT INTO saas_workflow_runs (id) VALUES ('default-id')")
            self.assertEqual(await conn.scalar(text("SELECT custom_note FROM saas_workflow_runs WHERE id='default-id'")), ':unassigned')
