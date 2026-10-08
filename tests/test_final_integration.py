"""Final review regressions, using temporary SQL and synthetic local channels."""
import asyncio
import json
import types
from unittest.mock import patch

from tests.support import ApiTestCase


def failing_rag(settings, channel, hit=None):
    # Import optional parser through the existing isolated dependency fixture.
    from tests.test_knowledge import AdapterTests
    fixture = AdapterTests()
    fixture.processor()
    module = fixture.rag_module
    from app.saas.retrieval import RAGRetriever
    engine = module.RAGEngine(settings=settings)
    engine._ready = True
    async def embed(query):
        if channel == 'embedding':
            raise RuntimeError('PRIVATE_RETRIEVAL_CANARY')
        return [0.1]
    def vector(**kwargs):
        if channel in {'vector', 'both'}:
            raise RuntimeError('PRIVATE_RETRIEVAL_CANARY')
        if hit:
            return {'ids': [[hit['chunk_id']]], 'distances': [[.1]], 'documents': [['Index text']],
                    'metadatas': [[{'doc_id': hit['doc_id'], 'kb_id': hit['metadata']['kb_id']}]]}
        return {'ids': [[]]}
    def keywords(*args):
        if channel in {'bm25', 'both'}:
            raise RuntimeError('PRIVATE_RETRIEVAL_CANARY')
        return [{'chunk_id': hit['chunk_id'], 'doc_id': hit['doc_id'], 'kb_id': hit['metadata']['kb_id'],
                 'score': .9, 'content': 'Index text'}] if hit else []
    engine.embedder = types.SimpleNamespace(embed_single=embed)
    engine.chroma_collection = types.SimpleNamespace(query=vector)
    engine.bm25 = types.SimpleNamespace(search=keywords)
    adapter = RAGRetriever(settings)
    adapter._engine = engine
    return adapter


class FinalIntegrationTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register('final-owner@example.test')
        self.kb = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Policies'})).json()['id']

    def evaluation(self, labels=None):
        query = {'query': 'Policy?'}
        if labels is not None:
            query['relevant_doc_ids'] = labels
        return {'kb_ids': [self.kb], 'queries': [query], 'configs': [{'name': 'test'}]}

    async def evidence(self, content='Policy'):
        self.retriever.chunks = [{'content': 'SQL authoritative policy'}]
        response = await self.client.post(f'/api/v1/kb/{self.kb}/documents', files={
            'files': ('policy.txt', content.encode(), 'text/plain')})
        doc = response.json()['documents'][0]['id']
        await self.app.state.ingestion_worker.drain()
        chunk = (await self.client.get(f'/api/v1/kb/{self.kb}/documents/{doc}/chunks')).json()[0]['id']
        return {'doc_id': doc, 'chunk_id': chunk, 'score': .9, 'metadata': {'kb_id': self.kb}}

    async def test_actual_selected_channel_outages_fail_closed_for_search_and_evaluation(self):
        hit = await self.evidence()
        for channel, alpha in [('bm25', 0), ('vector', 1), ('embedding', 1),
                               ('vector', .5), ('bm25', .5), ('both', .5)]:
            with self.subTest(channel=channel, alpha=alpha):
                self.app.state.retriever = failing_rag(self.settings, channel, hit)
                response = await self.client.post('/api/v1/agent/search', json={
                    'query': 'Policy?', 'kb_ids': [self.kb], 'hybrid_alpha': alpha})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn('PRIVATE_RETRIEVAL_CANARY', response.text)
                body = self.evaluation()
                body['configs'][0]['hybrid_alpha'] = alpha
                response = await self.client.post('/api/v1/evaluation/retrieval', json=body)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn('PRIVATE_RETRIEVAL_CANARY', response.text)

    async def test_actual_outage_chat_sync_sse_saved_state_and_insights(self):
        adapter = failing_rag(self.settings, 'both')
        self.app.state.chat_service.retriever = adapter
        for path in ['/api/v1/chat/completions/sync', '/api/v1/chat/completions']:
            response = await self.client.post(path, json={'query': 'Outage?', 'kb_ids': [self.kb]})
            if path.endswith('/sync'):
                result = response.json()
            else:
                frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
                result = frames[-1]['metadata']
            self.assertEqual(result['status'], 'error', result)
            self.assertEqual(result['error'], 'retrieval_unavailable')
            detail = (await self.client.get('/api/v1/conversations/' + result['conversation_id'])).json()
            self.assertEqual(detail['messages'][-1]['status'], 'error')
        self.assertEqual(self.llm.calls, [])
        insights = (await self.client.get('/api/v1/insights')).json()
        self.assertEqual(insights['answers']['insufficient_evidence'], 0)
        self.assertEqual(insights['gaps'], [])

    async def test_unselected_failed_channel_preserves_successful_empty_retrieval(self):
        for channel, alpha in [('vector', 0), ('embedding', 0), ('bm25', 1)]:
            self.app.state.retriever = failing_rag(self.settings, channel)
            response = await self.client.post('/api/v1/agent/search', json={
                'query': 'Policy?', 'kb_ids': [self.kb], 'hybrid_alpha': alpha})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['results'], [])

    async def test_search_and_evaluation_release_lock_and_recheck_membership(self):
        viewer, user = await self.join('final-viewer@example.test')
        for path in ['/api/v1/agent/search', '/api/v1/evaluation/retrieval']:
            with self.subTest(path=path):
                entered, release = asyncio.Event(), asyncio.Event()
                async def blocked(**kwargs):
                    entered.set()
                    await release.wait()
                    return []
                self.app.state.retriever.search = blocked
                body = self.evaluation() if 'evaluation' in path else {'query': 'Policy?', 'kb_ids': [self.kb]}
                task = asyncio.create_task(viewer.post(path, json=body))
                mutation_task = None
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    mutation_task = asyncio.create_task(self.client.post('/api/v1/knowledge-bases', json={'name': 'Concurrent'}))
                    done, _ = await asyncio.wait([mutation_task], timeout=1)
                    progressed = bool(done)
                    if progressed:
                        mutation = await mutation_task
                        self.assertEqual(mutation.status_code, 201, mutation.text)
                        removed = await self.client.delete(
                            f"/api/v1/organizations/{self.workspace_id}/members/{user['user']['id']}")
                        self.assertEqual(removed.status_code, 204, removed.text)
                finally:
                    release.set()
                    response = await task
                    if mutation_task:
                        await mutation_task
                self.assertTrue(progressed, 'Mutation was blocked by read-only retrieval')
                self.assertEqual(response.status_code, 404, response.text)
                if path.endswith('/search'):
                    invitation = await self.invite(user['user']['email'])
                    accepted = await viewer.post('/api/v1/invitations/accept', json={'token': invitation['token']})
                    self.assertEqual(accepted.status_code, 200, accepted.text)

    async def test_search_and_evaluation_rehydrate_after_concurrent_document_deletion(self):
        for path in ['/api/v1/agent/search', '/api/v1/evaluation/retrieval']:
            hit = await self.evidence(path)
            entered, release = asyncio.Event(), asyncio.Event()
            async def blocked(**kwargs):
                entered.set()
                await release.wait()
                return [hit]
            self.app.state.retriever.search = blocked
            body = self.evaluation() if 'evaluation' in path else {'query': 'Policy?', 'kb_ids': [self.kb]}
            task = asyncio.create_task(self.client.post(path, json=body))
            deletion_task = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                deletion_task = asyncio.create_task(self.client.delete(f"/api/v1/kb/{self.kb}/documents/{hit['doc_id']}"))
                done, _ = await asyncio.wait([deletion_task], timeout=1)
                progressed = bool(done)
            finally:
                release.set()
                response = await task
                if deletion_task:
                    deleted = await deletion_task
            self.assertTrue(progressed, 'Deletion was blocked by read-only retrieval')
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()['comparisons'][0]['results'] if 'evaluation' in path else response.json()['results']
            self.assertEqual(rows, [])

    async def test_evaluation_rechecks_labels_after_retrieval(self):
        hit = await self.evidence()
        async def delete_during_search(**kwargs):
            response = await self.client.delete(f"/api/v1/kb/{self.kb}/documents/{hit['doc_id']}")
            self.assertEqual(response.status_code, 200, response.text)
            return [hit]
        self.app.state.retriever.search = delete_during_search
        response = await self.client.post('/api/v1/evaluation/retrieval', json=self.evaluation([hit['doc_id']]))
        self.assertEqual(response.status_code, 404, response.text)

    async def test_search_and_evaluation_still_require_cookie_csrf(self):
        for path, body in [('/api/v1/agent/search', {'query': 'Policy?', 'kb_ids': [self.kb]}),
                           ('/api/v1/evaluation/retrieval', self.evaluation())]:
            response = await self.client.post(path, json=body, headers={'X-CSRF-Token': ''})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.retriever.search_calls, [])

    async def test_readiness_checks_mandatory_database_and_sanitizes_failure(self):
        response = await self.client.get('/api/v1/ready')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'status': 'ready'})
        with patch.object(self.app.state, 'session_factory', side_effect=RuntimeError('PRIVATE_DATABASE_CANARY')):
            response = await self.client.get('/api/v1/ready')
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json(), {'status': 'unavailable'})
            self.assertNotIn('PRIVATE_DATABASE_CANARY', response.text)
            self.assertEqual((await self.client.get('/api/v1/health')).status_code, 200)
        self.assertEqual(self.retriever.search_calls, [])
        self.assertEqual(self.llm.calls, [])

    async def test_readiness_database_wait_has_a_deadline(self):
        class BlockedSession:
            async def __aenter__(self):
                await asyncio.Event().wait()
            async def __aexit__(self, *args):
                pass
        with patch.object(self.app.state, 'session_factory', return_value=BlockedSession()):
            response = await asyncio.wait_for(self.client.get('/api/v1/ready'), 3)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {'status': 'unavailable'})
