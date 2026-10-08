"""Document-level retrieval comparison with isolated SQL and an injected adapter."""
import unittest
from uuid import uuid4

from tests.support import ApiTestCase

try:
    from app.saas.evaluation import retrieval_metrics
except ImportError:
    retrieval_metrics = None


class MetricTests(unittest.TestCase):
    def test_known_rank_duplicate_chunks_and_empty_truth(self):
        self.assertIsNotNone(retrieval_metrics)
        self.assertEqual(retrieval_metrics(['wrong', 'right'], ['right']), {
            'context_precision': .5, 'context_recall': 1.0, 'mrr': .5, 'hit_rate': 1.0, 'ndcg': 0.6309297535714575})
        self.assertEqual(retrieval_metrics(['wrong', 'wrong', 'right', 'right'], ['right']), {
            'context_precision': .5, 'context_recall': 1.0, 'mrr': .5, 'hit_rate': 1.0, 'ndcg': 0.6309297535714575})
        self.assertEqual(retrieval_metrics(['right', 'right'], ['right', 'right']), {
            'context_precision': 1.0, 'context_recall': 1.0, 'mrr': 1.0, 'hit_rate': 1.0, 'ndcg': 1.0})
        self.assertEqual(retrieval_metrics([], []), {
            'context_precision': 0.0, 'context_recall': 0.0, 'mrr': 0.0, 'hit_rate': 0.0, 'ndcg': 0.0})
        self.assertEqual(retrieval_metrics(['wrong'], []), {
            'context_precision': 0.0, 'context_recall': 0.0, 'mrr': 0.0, 'hit_rate': 0.0, 'ndcg': 0.0})
        self.assertEqual(retrieval_metrics(['wrong'], ['right']), {
            'context_precision': 0.0, 'context_recall': 0.0, 'mrr': 0.0, 'hit_rate': 0.0, 'ndcg': 0.0})

    def test_ndcg_multiple_relevant_documents_and_returned_cutoff(self):
        metrics = retrieval_metrics(['wrong', 'a', 'a', 'b'], ['a', 'b', 'c'])
        self.assertAlmostEqual(metrics['ndcg'], 0.5307212739772434)
        self.assertEqual(metrics['hit_rate'], 1.0)
        self.assertEqual(retrieval_metrics(['a'], ['a', 'b', 'c'])['ndcg'], 1.0)


class EvaluationTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register('evaluation@example.test')
        self.kb = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Warranty'})).json()['id']

    async def add_doc(self, kb, name, content, client=None):
        client = client or self.client
        self.retriever.chunks = [{'content': content}]
        response = await client.post(f'/api/v1/kb/{kb}/documents', files={
            'files': (name, content.encode(), 'text/plain')})
        self.assertEqual(response.status_code, 202, response.text)
        doc_id = response.json()['documents'][0]['id']
        await self.app.state.ingestion_worker.drain()
        chunks = (await client.get(f'/api/v1/kb/{kb}/documents/{doc_id}/chunks')).json()
        return doc_id, chunks[0]['id']

    def hit(self, doc, chunk, kb):
        return {'doc_id': doc, 'chunk_id': chunk, 'score': .9, 'metadata': {'kb_id': kb}}

    def body(self, **kwargs):
        return {'kb_ids': [self.kb], 'queries': [{'query': '保修多久？'}],
                'configs': [{'name': 'baseline', 'top_k': 5, 'hybrid_alpha': .3,
                             'score_threshold': .2, 'enable_reranker': False}], **kwargs}

    async def test_configs_forwarded_and_metrics_use_scoped_documents(self):
        doc, chunk = await self.add_doc(self.kb, 'warranty.txt', '保修两年。')
        async def configured_search(**kwargs):
            self.retriever.search_calls.append(kwargs)
            return [] if kwargs['top_k'] == 2 else [self.hit(doc, chunk, self.kb)]
        self.retriever.search = configured_search
        payload = self.body(queries=[{'query': '保修多久？', 'relevant_doc_ids': [doc]}], configs=[
            {'name': 'a', 'top_k': 2, 'hybrid_alpha': .2, 'score_threshold': .1, 'enable_reranker': False},
            {'name': 'b', 'top_k': 3, 'hybrid_alpha': .8, 'score_threshold': .6, 'enable_reranker': True}])
        response = await self.client.post('/api/v1/evaluation/retrieval', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        comparisons = response.json()['comparisons']
        self.assertEqual(len(comparisons), 2)
        self.assertEqual([row['metrics']['mrr'] for row in comparisons], [0, 1])
        self.assertEqual([row['results'] for row in comparisons][0], [])
        self.assertEqual(comparisons[1]['results'][0]['doc_id'], doc)
        self.assertEqual([call['top_k'] for call in self.retriever.search_calls], [2, 3])
        self.assertEqual([call['hybrid_alpha'] for call in self.retriever.search_calls], [.2, .8])
        self.assertEqual([call['score_threshold'] for call in self.retriever.search_calls], [.1, .6])
        self.assertEqual([call['enable_reranker'] for call in self.retriever.search_calls], [False, True])

    async def test_missing_truth_is_null_and_explicit_empty_truth_is_zero(self):
        response = await self.client.post('/api/v1/evaluation/retrieval', json=self.body(queries=[
            {'query': 'unlabeled'}, {'query': 'known empty', 'relevant_doc_ids': []}]))
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()['comparisons']
        self.assertEqual(rows[0]['metrics'], {'context_precision': None, 'context_recall': None, 'mrr': None, 'hit_rate': None, 'ndcg': None})
        self.assertEqual(rows[1]['metrics'], {'context_precision': 0.0, 'context_recall': 0.0, 'mrr': 0.0, 'hit_rate': 0.0, 'ndcg': 0.0})

    async def test_all_labels_must_be_live_completed_selected_and_tenant_scoped(self):
        selected, _ = await self.add_doc(self.kb, 'selected.txt', 'Selected')
        other_kb = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Other'})).json()['id']
        same_tenant, _ = await self.add_doc(other_kb, 'other.txt', 'Other')
        other_client = await self.new_client()
        await self.register_with(other_client, 'foreign@example.test')
        foreign_kb = (await other_client.post('/api/v1/knowledge-bases', json={'name': 'Foreign'})).json()['id']
        foreign, _ = await self.add_doc(foreign_kb, 'foreign.txt', 'Foreign', other_client)
        deleted, _ = await self.add_doc(self.kb, 'deleted.txt', 'Delete pending')
        response = await self.client.delete(f'/api/v1/kb/{self.kb}/documents/{deleted}')
        self.assertEqual(response.status_code, 200, response.text)
        pending = (await self.client.post(f'/api/v1/kb/{self.kb}/documents', files={
            'files': ('pending.txt', b'Pending', 'text/plain')})).json()['documents'][0]['id']
        for bad in [same_tenant, pending, deleted, foreign, str(uuid4())]:
            response = await self.client.post('/api/v1/evaluation/retrieval', json=self.body(
                queries=[{'query': 'good', 'relevant_doc_ids': [selected]},
                         {'query': 'bad', 'relevant_doc_ids': [bad]}]))
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(self.retriever.search_calls, [])
        response = await self.client.post('/api/v1/evaluation/retrieval', json=self.body(kb_ids=[self.kb, foreign_kb]))
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.retriever.search_calls, [])

    async def test_bounds_and_unknown_config_reject_before_adapter(self):
        cases = [
            self.body(configs=[{'name': f'c{i}'} for i in range(4)]),
            self.body(queries=[{'query': f'q{i}'} for i in range(51)]),
            self.body(configs=[{'name': 'too many', 'top_k': 21}]),
            self.body(configs=[{'name': 'unknown', 'extra': 'x'}]),
            self.body(configs=[{'name': 'nonfinite', 'hybrid_alpha': 'NaN'}]),
            self.body(queries=[{'query': 'x' * 1001}]),
            self.body(queries=[{'query': 'valid', 'relevant_doc_ids': ['x' * 65]}]),
            self.body(configs=[{'name': 'a'}, {'name': 'a'}]),
        ]
        for payload in cases:
            response = await self.client.post('/api/v1/evaluation/retrieval', json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.retriever.search_calls, [])

    async def test_unavailable_reranker_is_explicit_failure(self):
        from app.saas.retrieval import RAGRetriever
        self.app.state.retriever = RAGRetriever(self.settings)
        response = await self.client.post('/api/v1/evaluation/retrieval', json=self.body(
            configs=[{'name': 'rerank', 'enable_reranker': True}]))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn('reranking capability', response.json()['detail'])

    async def test_long_stored_chunk_is_bounded_in_response(self):
        doc, chunk = await self.add_doc(self.kb, 'long.txt', 'A' * 1200)
        self.retriever.results = [self.hit(doc, chunk, self.kb)]
        response = await self.client.post('/api/v1/evaluation/retrieval', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['comparisons'][0]['results'][0]
        self.assertEqual(len(result['content']), 1000)
