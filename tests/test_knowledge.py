"""Tenant boundaries and durable ingestion, entirely offline."""
import asyncio
import importlib
import sys
import types
import tempfile
import tracemalloc
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import select
from tests.support import ApiTestCase


class KnowledgeTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register('knowledge@example.test')

    async def kb(self, client=None, **extra):
        response = await (client or self.client).post('/api/v1/knowledge-bases', json={'name': 'Policies', **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    async def upload(self, kb, content=b'policy text', filename='policy.txt', client=None):
        return await (client or self.client).post(f'/api/v1/kb/{kb}/documents', files={'files': (filename, content, 'text/plain')})

    async def doc(self, kb):
        response = await self.upload(kb)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['documents'][0]['id']

    async def complete(self, kb):
        self.retriever.chunks = [{'content': 'Authoritative policy'}]
        doc = await self.doc(kb)
        await self.app.state.ingestion_worker.drain()
        chunks = (await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}/chunks')).json()
        return doc, chunks[0]['id']

    async def test_search_rejects_mixed_tenant_kb_ids_before_retrieval(self):
        own = await self.kb()
        other = await self.new_client()
        await self.register_with(other, 'foreign@example.test')
        foreign = await self.kb(other)
        response = await self.client.post('/api/v1/agent/search', json={'query': 'policy', 'kb_ids': [own, foreign]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.retriever.search_calls, [])
        for path in [f'/api/v1/knowledge-bases/{foreign}', f'/api/v1/kb/{foreign}/documents']:
            self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_documents_chunks_and_untrusted_index_metadata_are_scoped(self):
        kb = await self.kb()
        doc, chunk = await self.complete(kb)
        other = await self.new_client()
        await self.register_with(other, 'foreign@example.test')
        foreign = await self.kb(other)
        self.assertEqual((await other.get(f'/api/v1/kb/{kb}/documents/{doc}')).status_code, 404)
        self.assertEqual((await self.client.get(f'/api/v1/kb/{foreign}/documents/{doc}/chunks')).status_code, 404)
        self.retriever.results = [
            {'chunk_id': chunk, 'doc_id': doc, 'content': 'untrusted text', 'filename': 'bad', 'score': .8, 'metadata': {'kb_id': foreign, 'secret': 'leak'}},
            {'chunk_id': str(uuid4()), 'doc_id': doc, 'content': 'ghost', 'score': .9, 'metadata': {'kb_id': kb}},
            {'chunk_id': chunk, 'doc_id': str(uuid4()), 'content': 'mismatch', 'score': .9, 'metadata': {'kb_id': kb}},
            {'chunk_id': chunk, 'doc_id': doc, 'content': 'index stale', 'score': .8, 'metadata': {'kb_id': kb, 'secret': 'leak'}},
        ]
        response = await self.client.post('/api/v1/agent/search', json={'query': 'policy', 'kb_ids': [kb]})
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['content'], 'Authoritative policy')
        self.assertNotIn('secret', results[0]['metadata'])

    async def test_viewer_cannot_write(self):
        kb = await self.kb()
        viewer, _ = await self.join('viewer@example.test')
        for method, path, kwargs in [
            ('post', '/api/v1/knowledge-bases', {'json': {'name': 'No'}}),
            ('patch', f'/api/v1/knowledge-bases/{kb}', {'json': {'name': 'No'}}),
            ('delete', f'/api/v1/knowledge-bases/{kb}', {}),
        ]:
            self.assertEqual((await getattr(viewer, method)(path, **kwargs)).status_code, 403)
        self.assertEqual((await self.upload(kb, client=viewer)).status_code, 403)

    async def test_config_upload_validation_duplicate_and_limits(self):
        self.assertEqual((await self.client.post('/api/v1/knowledge-bases', json={'name': 'Bad', 'chunk_size': 100, 'chunk_overlap': 100})).status_code, 422)
        kb = await self.kb(chunk_size=120, chunk_overlap=10, use_unstructured=False)
        detail = (await self.client.get(f'/api/v1/knowledge-bases/{kb}')).json()
        self.assertEqual((detail['chunk_size'], detail['chunk_overlap'], detail['use_unstructured']), (120, 10, False))
        self.assertEqual((await self.upload(kb, b'')).status_code, 400)
        self.assertEqual((await self.upload(kb, filename='payload.exe')).status_code, 422)
        self.settings.max_upload_size_mb = 0
        self.assertEqual((await self.upload(kb)).status_code, 413)
        self.settings.max_upload_size_mb = 50
        doc = await self.doc(kb)
        duplicate = await self.upload(kb)
        self.assertEqual(duplicate.json()['skipped'], 1)
        self.assertEqual(duplicate.json()['accepted'], 0)
        calls = []
        async def parse(**kwargs):
            calls.append(kwargs)
            return [{'content': 'policy'}]
        self.retriever.parse = parse
        await self.app.state.ingestion_worker.drain()
        self.assertEqual((calls[0]['chunk_size'], calls[0]['chunk_overlap'], calls[0]['use_unstructured']), (120, 10, False))
        await self.kb()
        await self.kb()
        response = await self.client.post('/api/v1/knowledge-bases', json={'name': 'Excess'})
        self.assertEqual(response.status_code, 201)
        await self.kb()
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents')).json()['total'], 1)

    async def test_index_failure_retry_retains_source_and_deterministic_ids(self):
        kb = await self.kb()
        doc = await self.doc(kb)
        self.retriever.chunks = [{'content': 'policy'}]
        seen, cleanup = [], []
        async def fail(chunks):
            seen.append(chunks[0]['chunk_id'])
            raise RuntimeError('private credential')
        async def delete(doc_id):
            cleanup.append(doc_id)
            return 1
        self.retriever.upsert = fail
        self.retriever.delete_document = delete
        await self.app.state.ingestion_worker.drain()
        jobs = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs']
        job = jobs[0]
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('private', job['error'])
        from app.saas.ingestion_models import IngestionJob
        async with self.app.state.session_factory() as db:
            row = await db.get(IngestionJob, job['id'])
            self.assertTrue(Path(row.source_path).exists())
        async def succeed(chunks):
            seen.append(chunks[0]['chunk_id'])
            return len(chunks)
        self.retriever.upsert = succeed
        retry = await self.client.post(f"/api/v1/ingestion-jobs/{job['id']}/retry")
        self.assertEqual(retry.status_code, 200, retry.text)
        await self.app.state.ingestion_worker.drain()
        self.assertEqual(seen[0], seen[1])
        self.assertIn(doc, cleanup)
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}')).json()['status'], 'completed')

    async def test_expired_lease_recovers_and_bounded_attempts(self):
        kb = await self.kb()
        await self.doc(kb)
        from app.saas.ingestion_models import IngestionJob
        from app.saas.models import utcnow
        async with self.app.state.session_factory() as db:
            row = await db.scalar(select(IngestionJob))
            row.status, row.lease_token = 'running', str(uuid4())
            row.lease_expires_at = utcnow() - timedelta(seconds=1)
            row.attempts = 1
            await db.commit()
        self.retriever.chunks = [{'content': 'recovered'}]
        await self.app.state.ingestion_worker.drain()
        job = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs'][0]
        self.assertEqual((job['status'], job['attempts']), ('completed', 2))
        async with self.app.state.session_factory() as db:
            row = await db.get(IngestionJob, job['id'])
            row.status, row.attempts = 'failed', 3
            await db.commit()
        self.assertEqual((await self.client.post(f"/api/v1/ingestion-jobs/{job['id']}/retry")).status_code, 409)

    async def test_deletion_cleanup_is_durable_and_hides_results_immediately(self):
        kb = await self.kb()
        doc, chunk = await self.complete(kb)
        cleanup = []
        async def fail(doc_id):
            cleanup.append(doc_id)
            raise RuntimeError('offline')
        self.retriever.delete_document = fail
        response = await self.client.delete(f'/api/v1/kb/{kb}/documents/{doc}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}')).status_code, 404)
        self.retriever.results = [{'chunk_id': chunk, 'doc_id': doc, 'content': 'ghost', 'score': 1, 'metadata': {'kb_id': kb}}]
        self.assertEqual((await self.client.post('/api/v1/agent/search', json={'query': 'policy', 'kb_ids': [kb]})).json()['results'], [])
        await self.app.state.ingestion_worker.drain()
        job = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs'][0]
        self.assertEqual(job['status'], 'failed')
        async def succeed(doc_id):
            return 1
        self.retriever.delete_document = succeed
        self.assertEqual((await self.client.post(f"/api/v1/ingestion-jobs/{job['id']}/retry")).status_code, 200)
        await self.app.state.ingestion_worker.drain()
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents')).json()['total'], 0)

    async def test_default_adapter_is_lazy(self):
        from app.saas.application import create_app
        from app.saas.retrieval import RAGRetriever
        app = create_app(self.settings)
        self.assertIsInstance(app.state.retriever, RAGRetriever)
        self.assertIsNone(app.state.retriever._engine)

    async def test_concurrent_kb_inserts_are_unlimited(self):
        await self.kb()
        await self.kb()
        responses = await asyncio.gather(*[
            self.client.post('/api/v1/knowledge-bases', json={'name': name})
            for name in ('First contender', 'Second contender')])
        self.assertEqual(sorted(response.status_code for response in responses), [201, 201])
        self.assertEqual(len((await self.client.get('/api/v1/knowledge-bases')).json()), 4)

    async def test_viewer_document_deletion_retry_and_foreign_job_are_denied(self):
        kb = await self.kb()
        doc = await self.doc(kb)
        async def fail(chunks):
            raise RuntimeError('synthetic failure')
        self.retriever.chunks, self.retriever.upsert = [{'content': 'policy'}], fail
        await self.app.state.ingestion_worker.drain()
        job = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs'][0]
        viewer, _ = await self.join('job-viewer@example.test')
        self.assertEqual((await viewer.delete(f'/api/v1/kb/{kb}/documents/{doc}')).status_code, 403)
        self.assertEqual((await viewer.post(f"/api/v1/ingestion-jobs/{job['id']}/retry")).status_code, 403)
        foreign = await self.new_client()
        await self.register_with(foreign, 'foreign-job@example.test')
        self.assertEqual((await foreign.post(f"/api/v1/ingestion-jobs/{job['id']}/retry")).status_code, 404)
        self.assertEqual((await foreign.get('/api/v1/ingestion-jobs')).json()['jobs'], [])

    async def test_unlimited_documents_and_invalid_mixed_upload_are_atomic(self):
        from app.models import Document
        kb = await self.kb()
        async with self.app.state.session_factory() as db:
            for i in range(49):
                db.add(Document(kb_id=kb, filename=f'{i}.txt', file_hash=str(i), file_type='txt'))
            await db.commit()
        response = await self.client.post(f'/api/v1/kb/{kb}/documents', files=[
            ('files', ('one.txt', b'one', 'text/plain')),
            ('files', ('two.txt', b'two', 'text/plain'))])
        self.assertEqual(response.status_code, 202)
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents')).json()['total'], 51)
        retained = set(Path(self.settings.upload_temp_dir).rglob('*'))
        response = await self.client.post(f'/api/v1/kb/{kb}/documents', files=[
            ('files', ('three.txt', b'three', 'text/plain')),
            ('files', ('bad.exe', b'two', 'text/plain'))])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(set(Path(self.settings.upload_temp_dir).rglob('*')), retained)
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents')).json()['total'], 51)
        self.assertEqual((await self.upload(kb)).status_code, 202)

    async def test_deletion_during_index_write_is_fenced_then_cleaned(self):
        kb, cleanup = await self.kb(), []
        doc = await self.doc(kb)
        self.retriever.chunks = [{'content': 'late write'}]
        async def upsert(chunks):
            response = await self.client.delete(f'/api/v1/kb/{kb}/documents/{doc}')
            self.assertEqual(response.status_code, 200)
            return len(chunks)
        async def delete(doc_id):
            cleanup.append(doc_id)
        self.retriever.upsert, self.retriever.delete_document = upsert, delete
        await self.app.state.ingestion_worker.drain()
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents')).json()['total'], 0)
        job = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs'][0]
        self.assertEqual((job['status'], job['action']), ('completed', 'delete'))
        self.assertGreaterEqual(cleanup.count(doc), 2)

    async def test_cancelled_worker_recovers_from_retained_source(self):
        from app.saas.ingestion_models import IngestionJob
        from app.saas.models import utcnow
        kb = await self.kb()
        await self.doc(kb)
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.retriever.parse = blocked
        task = asyncio.create_task(self.app.state.ingestion_worker.drain())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.app.state.session_factory() as db:
            row = await db.scalar(select(IngestionJob))
            self.assertEqual(row.status, 'running')
            self.assertTrue(Path(row.source_path).is_file())
            row.lease_expires_at = utcnow() - timedelta(seconds=1)
            await db.commit()
        async def recovered(**kwargs):
            return [{'content': 'after restart'}]
        self.retriever.parse = recovered
        # New worker instance represents a process restart with the same SQL queue.
        from app.saas.ingestion import IngestionWorker
        await IngestionWorker(self.app.state.session_factory, self.settings, self.retriever).drain()
        job = (await self.client.get('/api/v1/ingestion-jobs')).json()['jobs'][0]
        self.assertEqual((job['status'], job['attempts']), ('completed', 2))

    async def test_heartbeat_prevents_other_worker_from_claiming_live_job(self):
        from app.saas.ingestion import IngestionWorker
        from app.saas.ingestion_models import IngestionJob
        from app.saas.models import utcnow
        from app.saas.security import aware
        kb = await self.kb()
        await self.doc(kb)
        self.settings.ingestion_lease_seconds = 3
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await release.wait()
            return [{'content': 'heartbeat'}]
        self.retriever.parse = blocked
        task = asyncio.create_task(self.app.state.ingestion_worker.drain())
        self.addAsyncCleanup(self._cancel, task)
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(3.2)
        async with self.app.state.session_factory() as db:
            row = await db.scalar(select(IngestionJob))
            self.assertGreater(aware(row.lease_expires_at), utcnow())
            self.assertEqual(row.attempts, 1)
        other = IngestionWorker(self.app.state.session_factory, self.settings, self.retriever)
        self.assertIsNone(await other._claim())
        release.set()
        await task

    async def _cancel(self, task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def processor(self):
        import app.schemas  # Keep ORM imports outside the optional-module patch.
        fake = types.ModuleType('langchain_text_splitters')
        class Splitter:
            def __init__(self, **kwargs):
                pass
            def split_text(self, text):
                return [text]
        fake.RecursiveCharacterTextSplitter = Splitter
        with patch.dict(sys.modules, {'langchain_text_splitters': fake}):
            module = importlib.import_module('app.rag_engine')
        self.rag_module = module
        from app.saas.config import AppSettings
        settings = AppSettings(_env_file=None, _env_prefix='KNOWLEDGE_TEST_ISOLATED_', use_unstructured=False)
        with patch.object(module, 'RecursiveCharacterTextSplitter', Splitter):
            return module.DocumentProcessor(settings=settings)

    async def test_office_archives_extract_text_without_extra_libraries(self):
        processor = self.processor()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'office.zip'
            examples = [
                ('sheet.xlsx', {'xl/sharedStrings.xml': '<sst><si><t>中文政策</t></si></sst>',
                    'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c><c t="inlineStr"><is><t>inline</t></is></c></row></sheetData></worksheet>'}, ['中文政策', '42', 'inline']),
                ('slides.pptx', {'ppt/slides/slide1.xml': '<slide><p><t>第一张政策</t></p></slide>',
                    'ppt/slides/slide2.xml': '<slide><p><t>Second slide</t></p></slide>'}, ['第一张政策', 'Second slide']),
                ('document.docx', {'word/document.xml': '<document><body><p><r><t>Office policy</t></r></p><tbl><tr><tc><p><r><t>Table cell</t></r></p></tc></tr></tbl></body></document>'}, ['Office policy', 'Table cell']),
            ]
            for filename, entries, expected in examples:
                with zipfile.ZipFile(path, 'w') as archive:
                    for name, data in entries.items():
                        archive.writestr(name, data)
                chunks = processor.parse_and_chunk(str(path), filename, str(uuid4()), str(uuid4()))
                self.assertTrue(chunks)
                for text in expected:
                    self.assertIn(text, chunks[0]['content'])

    async def test_shared_string_fanout_is_bounded_before_row_join(self):
        processor = self.processor()
        # Reduced budget keeps even the vulnerable regression below 2 MiB.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fanout.xlsx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('xl/sharedStrings.xml', '<sst><si><t>' + 'x' * 10000 + '</t></si></sst>')
                archive.writestr('xl/worksheets/sheet1.xml', '<worksheet><row>' + '<c t="s"><v>0</v></c>' * 200 + '</row></worksheet>')
            with patch.object(processor, 'OFFICE_MAX_TEXT_CHARS', 100000, create=True):
                tracemalloc.start()
                try:
                    with self.assertRaisesRegex(ValueError, 'Office text exceeds processing limit'):
                        processor._parse_file(str(path), path.name)
                    _, peak = tracemalloc.get_traced_memory()
                    self.assertLess(peak, 1000000, f'Fan-out allocated {peak} bytes before rejection')
                finally:
                    tracemalloc.stop()

    async def test_actual_adapter_parser_and_search_logs_exclude_sensitive_data(self):
        from app.saas.retrieval import RAGRetriever
        from fastapi import HTTPException
        processor = self.processor()
        module = self.rag_module
        canary = 'SYNTHETIC_CREDENTIAL_CANARY'
        adapter = RAGRetriever(types.SimpleNamespace(use_reranker=False))
        engine = module.RAGEngine(settings=types.SimpleNamespace(
            chroma_collection_name=canary, chroma_persist_dir=canary))
        engine._ready = True
        async def embed(query):
            return [0.1]
        def fail(*args, **kwargs):
            raise RuntimeError(canary)
        engine.embedder = types.SimpleNamespace(embed_single=embed)
        engine.chroma_collection = types.SimpleNamespace(query=fail)
        engine.bm25 = types.SimpleNamespace(search=fail)
        adapter._engine = engine
        processor.use_unstructured = True
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.txt'
            path.write_text('Visible text', encoding='utf-8')
            with patch.dict(sys.modules, {'app.rag_engine': module}), \
                    patch.object(module, 'DocumentProcessor', return_value=processor), \
                    patch.object(processor, '_parse_unstructured', side_effect=RuntimeError(canary)), \
                    self.assertLogs('rag_engine', level='INFO') as logs:
                chunks = await adapter.parse(str(path), canary + '.txt', str(uuid4()), str(uuid4()), 100, 10, True)
                with self.assertRaises(HTTPException) as failure:
                    await adapter.search(canary, [str(uuid4())], 5, .5, .4, False)
            self.assertEqual(chunks[0]['content'], 'Visible text')
            self.assertEqual(failure.exception.status_code, 503)
            self.assertNotIn(canary, failure.exception.detail)
            self.assertNotIn(canary, '\n'.join(logs.output))
            self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_adapter_unhandled_failure_is_sanitized_without_raw_cause(self):
        from app.saas.retrieval import RAGRetriever
        from fastapi import HTTPException
        canary = 'SYNTHETIC_CREDENTIAL_CANARY'
        adapter = RAGRetriever(types.SimpleNamespace(use_reranker=True))
        async def fail(*args, **kwargs):
            raise RuntimeError(canary)
        adapter._engine = types.SimpleNamespace(ingest_document=fail, delete_document_chunks=fail,
            delete_kb_chunks=fail, hybrid_search=fail)
        adapter._reranker = types.SimpleNamespace(rerank=fail)
        for operation in [lambda: adapter.upsert([]), lambda: adapter.delete_document('id'), lambda: adapter.delete_kb('id'),
                          lambda: adapter.search('query', [], 1, .5, .4, True), lambda: adapter.rerank('query', [], 1)]:
            with self.assertRaises(HTTPException) as raised:
                await operation()
            self.assertEqual(raised.exception.status_code, 503)
            self.assertNotIn(canary, str(raised.exception.detail))
            self.assertTrue(raised.exception.__suppress_context__)
            self.assertIsNone(raised.exception.__cause__)

    async def test_embedding_cpu_fallback_logs_exclude_exception_and_model_secrets(self):
        self.processor()
        module = self.rag_module
        canary = 'SYNTHETIC_CREDENTIAL_CANARY'
        fake_torch = types.ModuleType('torch')
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
        fake_transformers = types.ModuleType('sentence_transformers')
        class Model:
            def get_sentence_embedding_dimension(self):
                return 3
        calls = []
        def load(name, device):
            calls.append(device)
            if device == 'cuda':
                raise RuntimeError(canary)
            return Model()
        fake_transformers.SentenceTransformer = load
        settings = types.SimpleNamespace(embedding_model_name=canary, embedding_device='cuda', embedding_batch_size=1)
        with patch.dict(sys.modules, {'torch': fake_torch, 'sentence_transformers': fake_transformers}), \
                self.assertLogs('rag_engine', level='INFO') as logs:
            embedder = module.LocalEmbedding(settings=settings)
            await embedder._ensure_loaded()
        self.assertEqual(calls, ['cuda', 'cpu'])
        self.assertEqual(embedder.dim, 3)
        self.assertNotIn(canary, '\n'.join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_office_parser_rejects_archive_and_xml_bombs_and_path_escape(self):
        processor = self.processor()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'office.zip'
            for name, xml in [('../escape.xml', '<a/>'),
                ('ppt/slides/slide1.xml', '<!DOCTYPE a [<!ENTITY x "boom">]><slide>&x;</slide>'),
                ('ppt/slides/slide1.xml', '<slide>' + 'x' * (8 * 1024 * 1024) + '</slide>')]:
                with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(name, xml)
                with self.assertRaises(ValueError):
                    processor._parse_file(str(path), 'slides.pptx')

    async def test_plain_formats_and_html_extract_visible_content_locally(self):
        processor = self.processor()
        with tempfile.TemporaryDirectory() as folder:
            for filename, content in [('policy.txt', '中文政策'), ('policy.md', '# 中文政策'),
                ('policy.csv', 'name,value\n政策,42'), ('policy.json', '{"policy":"中文政策"}')]:
                path = Path(folder) / filename
                path.write_text(content, encoding='utf-8')
                expected = content.replace(',', ' | ') if filename.endswith('.csv') else content
                self.assertEqual(processor._parse_file(str(path), filename), expected)
            path = Path(folder) / 'policy.html'
            path.write_text('<html><script>hidden script</script><style>hidden style</style><p>中文 &amp; policy</p></html>', encoding='utf-8')
            with patch.dict(sys.modules, {'bs4': None}):
                result = processor._parse_file(str(path), path.name)
            self.assertIn('中文 & policy', result)
            self.assertNotIn('hidden', result)

    async def test_disabled_reranker_is_reported_before_initializing_engine(self):
        from app.saas.config import AppSettings
        from app.saas.retrieval import RAGRetriever
        from fastapi import HTTPException
        settings = AppSettings(_env_file=None, _env_prefix='KNOWLEDGE_TEST_ISOLATED_', use_reranker=False)
        adapter = RAGRetriever(settings)
        for operation in [adapter.rerank('q', [], 1), adapter.search('q', [], 1, .5, .4, True)]:
            with self.assertRaises(HTTPException) as raised:
                await operation
            self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNone(adapter._engine)

    async def test_settings_injected_into_actual_local_constructors_and_rerank(self):
        from app.saas.config import AppSettings
        from app.saas.retrieval import RAGRetriever
        fake = types.ModuleType('langchain_text_splitters')
        class Splitter:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        fake.RecursiveCharacterTextSplitter = Splitter
        # Only stand in for an optional splitter import; actual production local
        # constructors and LocalReranker.rerank execute without downloading models.
        with patch.dict(sys.modules, {'langchain_text_splitters': fake}):
            module = importlib.import_module('app.rag_engine')
            settings = AppSettings(_env_file=None, _env_prefix='KNOWLEDGE_TEST_ISOLATED_',
                embedding_model_name='injected', use_reranker=True)
            with patch.object(module, 'get_settings', side_effect=AssertionError('Legacy settings read')):
                self.assertEqual(module.LocalEmbedding(settings=settings).model_name, 'injected')
                self.assertIs(module.RAGEngine(settings=settings).cfg, settings)
                processor = module.DocumentProcessor(120, 10, settings=settings, use_unstructured=False)
                self.assertFalse(processor.use_unstructured)
                class Model:
                    def compute_score(self, pairs, normalize=True):
                        return [.1, .9]
                adapter = RAGRetriever(settings)
                adapter._reranker = module.LocalReranker(settings=settings)
                adapter._reranker._model = Model()
                original = [{'content': 'first'}, {'content': 'second'}]
                result = await adapter.rerank('q', original, 1)
                self.assertEqual(result[0]['content'], 'second')
                self.assertNotIn('score', original[0])
                self.assertIsNone(adapter._engine)
