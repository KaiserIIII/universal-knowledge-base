"""Tenant workflow execution against temporary SQL and synthetic upstreams only."""
import asyncio
import copy
import json
import os
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import select
from tests.support import ApiTestCase


def graph(kb, profile, *, dual=False):
    nodes = [
        {'id': 'input', 'type': 'input', 'config': {}},
        {'id': 'retrieve', 'type': 'retrieval', 'config': {'kb_ids': [kb], 'top_k': 3, 'context_chars': 1000}},
        {'id': 'gate', 'type': 'evidence', 'config': {'min_sources': 1}},
        {'id': 'model', 'type': 'model', 'config': {'profile_id': profile, 'temperature': .3, 'top_p': .7, 'max_tokens': 123, 'timeout_seconds': 5}},
        {'id': 'output', 'type': 'output', 'config': {}},
    ]
    pairs = [('input', 'retrieve'), ('retrieve', 'gate'), ('gate', 'model'), ('model', 'output')]
    if dual:
        nodes.extend([{'id': 'model2', 'type': 'model', 'config': {'profile_id': profile, 'temperature': .8, 'max_tokens': 234}},
                      {'id': 'merge', 'type': 'merge', 'config': {'mode': 'concatenate'}}])
        pairs = pairs[:-1] + [('gate', 'model2'), ('model', 'merge'), ('model2', 'merge'), ('merge', 'output')]
    for node in nodes:
        node.update(label=node['id'], position={'x': 100, 'y': 100})
    return {'nodes': nodes, 'edges': [{'id': str(i), 'source': a, 'target': b, 'source_port': 'out', 'target_port': 'in'} for i, (a, b) in enumerate(pairs)]}


class WorkflowTests(ApiTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register('flows@example.test')
        self.kb = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Policies'})).json()['id']

    async def profile(self, **overrides):
        self.settings.__dict__.update(model_allowed_hosts=['provider.example.test'], model_allowed_secret_refs=['WORKFLOW_TEST_KEY'])
        payload = {'name': 'Synthetic', 'provider': 'openai-compatible', 'base_url': 'https://provider.example.test/v1',
                   'model': 'model-a', 'api_key_env': None, **overrides}
        response = await self.client.post('/api/v1/model-profiles', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    async def evidence(self, kb=None, content='Policy document body'):
        kb = kb or self.kb
        self.retriever.chunks = [{'content': content}]
        response = await self.client.post(f'/api/v1/kb/{kb}/documents', files={'files': ('policy.txt', content.encode(), 'text/plain')})
        doc = response.json()['documents'][0]['id']
        await self.app.state.ingestion_worker.drain()
        chunk = (await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}/chunks')).json()[0]['id']
        hit = {'chunk_id': chunk, 'doc_id': doc, 'score': .9, 'metadata': {'kb_id': kb}}
        self.retriever.results.append(hit)
        return hit

    async def save(self, value):
        response = await self.client.post('/api/v1/workflows', json={'name': 'Workflow', 'graph': value})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    async def run_flow(self, wid, **kwargs):
        response = await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'What policy?', 'use_published': False, **kwargs})
        self.assertEqual(response.status_code, 202, response.text)
        return await self.poll(wid, response.json()['id'])

    async def poll(self, wid, rid):
        for _ in range(300):
            result = (await self.client.get(f'/api/v1/workflows/{wid}/runs/{rid}')).json()
            if result.get('status') not in {'queued', 'running', 'canceling'}:
                return result
            await asyncio.sleep(.01)
        self.fail('Workflow did not finish')

    async def usage(self):
        from app.saas.billing_models import MonthlyUsage
        async with self.app.state.session_factory() as db:
            row = await db.scalar(select(MonthlyUsage).where(MonthlyUsage.workspace_id == self.workspace_id))
            return (row.completed_answers, row.reserved_answers) if row else (0, 0)

    async def test_profiles_enforce_hosts_secrets_urls_and_roles(self):
        pid = await self.profile()
        for data in [{'base_url': 'https://evil.test/v1'}, {'api_key_env': 'PATH'}, {'base_url': 'https://user:pass@provider.example.test/v1'},
                     {'base_url': 'https://provider.example.test/v1?api_key=x'}, {'base_url': 'http://provider.example.test/v1'},
                     {'temperature': 5}, {'provider': 'arbitrary-code'}, {'api_key': 'plaintext'}]:
            response = await self.client.patch('/api/v1/model-profiles/' + pid, json=data)
            self.assertEqual(response.status_code, 422, response.text)
        viewer, _ = await self.join('viewer@example.test', paid_fixture=True)
        self.assertEqual((await viewer.post('/api/v1/model-profiles/' + pid + '/test')).status_code, 403)

    async def test_graph_validation_rejects_cycle_ports_dangling_and_budget(self):
        pid = await self.profile()
        original = graph(self.kb, pid)
        cases = []
        g = copy.deepcopy(original); g['edges'][0]['target'] = 'missing'; cases.append(g)
        g = copy.deepcopy(original); g['edges'][0]['source_port'] = 'wrong'; cases.append(g)
        g = copy.deepcopy(original); g['edges'].append(dict(g['edges'][0], id='duplicate')); cases.append(g)
        g = copy.deepcopy(original); g['nodes'][3]['config']['max_tokens'] = 99999; cases.append(g)
        g = copy.deepcopy(original); g['nodes'].append({'id': 'orphan', 'type': 'input', 'label': '', 'position': {'x': 0, 'y': 0}, 'config': {}}); cases.append(g)
        g = copy.deepcopy(original); g['edges'].append({'id': 'cycle', 'source': 'gate', 'target': 'gate', 'source_port': 'out', 'target_port': 'in'}); cases.append(g)
        for g in cases:
            response = await self.client.post('/api/v1/workflows/validate', json={'graph': g})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.retriever.search_calls, [])

    async def test_drafts_versions_rollback_and_foreign_resources(self):
        pid = await self.profile()
        g = graph(self.kb, pid)
        wid = await self.save(g)
        published = await self.client.post(f'/api/v1/workflows/{wid}/publish')
        self.assertEqual(published.status_code, 200, published.text)
        version = published.json()['published_version_id']
        draft = graph(self.kb, '')
        self.assertEqual((await self.client.patch(f'/api/v1/workflows/{wid}', json={'graph': draft})).status_code, 200)
        self.assertEqual((await self.client.post(f'/api/v1/workflows/{wid}/publish')).status_code, 422)
        self.assertEqual((await self.run_flow(wid, use_published=True))['status'], 'insufficient_evidence')
        rollback = await self.client.post(f'/api/v1/workflows/{wid}/rollback', json={'version_id': version})
        self.assertEqual(rollback.json()['graph']['nodes'][3]['config']['profile_id'], '')
        other = await self.new_client(); await self.register_with(other, 'foreign@example.test')
        self.assertEqual((await other.get(f'/api/v1/workflows/{wid}')).status_code, 404)
        self.assertEqual((await other.get('/api/v1/model-profiles/' + pid)).status_code, 404)
        foreign_kb = (await other.post('/api/v1/knowledge-bases', json={'name': 'Foreign'})).json()['id']
        for bad in [graph(foreign_kb, pid), graph(self.kb, str(uuid4()))]:
            self.assertEqual((await self.client.post('/api/v1/workflows/validate', json={'graph': bad})).status_code, 404)

    async def test_empty_gate_short_circuits_even_with_direct_prompt(self):
        pid = await self.profile()
        g = graph(self.kb, pid)
        g['nodes'].append({'id': 'prompt', 'type': 'prompt', 'label': 'prompt', 'position': {'x': 0, 'y': 0}, 'config': {}})
        g['edges'].extend([{'id': 'a', 'source': 'input', 'target': 'prompt', 'source_port': 'out', 'target_port': 'in'}, {'id': 'b', 'source': 'prompt', 'target': 'model', 'source_port': 'out', 'target_port': 'in'}])
        wid = await self.save(g)
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'insufficient_evidence', result)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(await self.usage(), (0, 0))

    async def test_actual_retrieval_outage_fails_workflow_without_knowledge_gap(self):
        from tests.test_final_integration import failing_rag
        pid = await self.profile()
        wid = await self.save(graph(self.kb, pid))
        self.app.state.workflow_service.retriever = failing_rag(self.settings, 'both')
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['error'], 'retrieval_unavailable')
        self.assertNotIn('PRIVATE_RETRIEVAL_CANARY', json.dumps(result))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(await self.usage(), (0, 0))
        self.assertEqual((await self.client.get('/api/v1/insights')).json()['gaps'], [])

    async def test_supported_metadata_filter_uses_authoritative_sql_fields(self):
        pid = await self.profile()
        hit = await self.evidence()
        hit['metadata'].update(chunk_index=999, category='forged')
        for metadata, status in [({'kb_id': self.kb, 'chunk_index': 0}, 'completed'),
                                 ({'chunk_index': 999}, 'insufficient_evidence'),
                                 ({'category': 'forged'}, 'insufficient_evidence')]:
            g = graph(self.kb, pid)
            g['nodes'].append({'id': 'filter', 'type': 'filter', 'label': 'filter',
                               'position': {'x': 0, 'y': 0}, 'config': {'metadata': metadata}})
            g['edges'][1]['target'] = 'filter'
            g['edges'].append({'id': 'filtered', 'source': 'filter', 'target': 'gate', 'source_port': 'out', 'target_port': 'in'})
            wid = await self.save(g)
            result = await self.run_flow(wid)
            self.assertEqual(result['status'], status, result)

    async def test_cancel_progresses_while_search_or_evaluation_waits_for_retrieval(self):
        pid = await self.profile()
        wid = await self.save(graph(self.kb, pid))
        for path in ['/api/v1/agent/search', '/api/v1/evaluation/retrieval']:
            workflow_entered, read_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def blocked(**kwargs):
                (read_entered if kwargs['query'] == 'slow-read' else workflow_entered).set()
                await release.wait()
                return []
            self.retriever.search = blocked
            response = await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'workflow', 'use_published': False})
            self.assertEqual(response.status_code, 202, response.text)
            rid = response.json()['id']
            await asyncio.wait_for(workflow_entered.wait(), 2)
            body = {'query': 'slow-read', 'kb_ids': [self.kb]}
            if 'evaluation' in path:
                body = {'kb_ids': [self.kb], 'queries': [{'query': 'slow-read'}], 'configs': [{'name': 'test'}]}
            read_task = asyncio.create_task(self.client.post(path, json=body))
            cancel_task = None
            try:
                await asyncio.wait_for(read_entered.wait(), 2)
                cancel_task = asyncio.create_task(self.client.post(f'/api/v1/workflows/{wid}/runs/{rid}/cancel'))
                done, _ = await asyncio.wait([cancel_task], timeout=1)
                progressed = bool(done)
            finally:
                release.set()
                await read_task
                if cancel_task:
                    canceled = await cancel_task
            self.assertTrue(progressed, 'Cancellation was blocked by read-only retrieval')
            self.assertEqual(canceled.status_code, 200, canceled.text)
            self.assertEqual((await self.poll(wid, rid))['status'], 'canceled')
            self.assertEqual(await self.usage(), (0, 0))

    async def test_parallel_models_dispatch_parameters_and_safe_traces(self):
        pid, pid2 = await self.profile(), await self.profile(model='model-b')
        await self.evidence()
        g = graph(self.kb, pid, dual=True); g['nodes'][5]['config']['profile_id'] = pid2
        wid = await self.save(g)
        arrived = asyncio.Event(); count = 0
        async def complete(**kwargs):
            nonlocal count
            self.llm.calls.append(kwargs); count += 1
            if count == 2: arrived.set()
            await asyncio.wait_for(arrived.wait(), 1)
            return {'content': 'Supported [1] [99]', 'usage': {}}
        self.llm.complete = complete
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual({call['profile_id'] for call in self.llm.calls}, {pid, pid2})
        first = next(call for call in self.llm.calls if call['profile_id'] == pid)
        self.assertEqual((first['temperature'], first['top_p'], first['max_tokens'], first['timeout_seconds']), (.3, .7, 123, 5))
        self.assertNotIn('[99]', result['answer'])
        self.assertTrue(result['invalid_citations'])
        self.assertEqual(len(result['sources']), 1)
        self.assertNotIn('Policy document body', json.dumps(result['trace']))
        self.assertEqual(await self.usage(), (1, 0))

    async def test_partial_failure_is_degraded_and_total_failure_is_error(self):
        pid = await self.profile(); await self.evidence()
        wid = await self.save(graph(self.kb, pid, dual=True))
        async def complete(**kwargs):
            if kwargs['temperature'] == .3: raise httpx.ConnectError('SECRET upstream payload')
            return {'content': 'Answer [1]'}
        self.llm.complete = complete
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'degraded', result)
        self.assertNotIn('SECRET', json.dumps(result))
        async def fail(**kwargs): raise httpx.ConnectError('SECRET')
        self.llm.complete = fail
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(await self.usage(), (1, 0))

    async def test_real_cancel_and_whole_run_timeout_release_quota(self):
        pid = await self.profile(); await self.evidence()
        wid = await self.save(graph(self.kb, pid, dual=True))
        started = asyncio.Event(); canceled = []
        async def slow(**kwargs):
            started.set()
            try: await asyncio.sleep(30)
            finally: canceled.append(True)
        self.llm.complete = slow
        response = await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})
        self.assertEqual(response.status_code, 202, response.text)
        rid = response.json()['id']; await asyncio.wait_for(started.wait(), 2)
        self.assertEqual((await self.client.post(f'/api/v1/workflows/{wid}/runs/{rid}/cancel')).status_code, 200)
        result = await self.poll(wid, rid)
        self.assertEqual(result['status'], 'canceled', result)
        self.assertTrue(canceled)
        self.assertEqual(await self.usage(), (0, 0))
        self.settings.workflow_timeout_seconds = .1
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'timed_out', result)
        self.assertEqual(await self.usage(), (0, 0))

    async def test_real_http_profile_parameters_and_safe_test_error(self):
        pid = await self.profile(api_key_env='WORKFLOW_TEST_KEY')
        await self.evidence()
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Answer [1]'}}]})
        from app.saas.llm import OpenAICompatibleLLM
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        self.app.state.model_dispatcher.llm = OpenAICompatibleLLM(self.settings, client=client)
        with patch.dict(os.environ, {'WORKFLOW_TEST_KEY': 'CANARY_secret_9283'}):
            wid = await self.save(graph(self.kb, pid))
            result = await self.run_flow(wid)
            self.assertEqual(result['status'], 'completed', result)
            body = json.loads(seen[0].content)
            self.assertEqual((str(seen[0].url), body['model'], body['temperature'], body['top_p'], body['max_tokens']),
                             ('https://provider.example.test/v1/chat/completions', 'model-a', .3, .7, 123))
            self.assertEqual(seen[0].headers['authorization'], 'Bearer CANARY_secret_9283')
            self.assertEqual(seen[0].extensions['timeout']['read'], 5)
            self.assertNotIn('CANARY_secret', json.dumps(result))
            detail = await self.client.get('/api/v1/model-profiles/' + pid)
            self.assertNotIn('CANARY_secret', detail.text)
        self.assertEqual((await self.client.post('/api/v1/model-profiles/' + pid + '/test')).json()['ok'], False)

    async def test_chat_profile_is_scoped_before_search_and_dispatches_same_binding(self):
        pid = await self.profile(top_p=.42, max_tokens=333, temperature=.6, system_prompt='Profile style instruction')
        await self.evidence()
        payload = {'query': 'Policy?', 'kb_ids': [self.kb], 'profile_id': pid}
        response = await self.client.post('/api/v1/chat/completions/sync', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'completed')
        self.assertEqual(self.llm.calls[-1]['profile_id'], pid)
        self.assertEqual((self.llm.calls[-1]['temperature'], self.llm.calls[-1]['max_tokens'], self.llm.calls[-1]['top_p']), (.6, 333, .42))
        self.assertIn('Profile style instruction', json.dumps(self.llm.calls[-1]['messages']))
        searches, calls = len(self.retriever.search_calls), len(self.llm.calls)
        response = await self.client.post('/api/v1/chat/completions/sync', json={**payload, 'profile_id': str(uuid4())})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual((len(self.retriever.search_calls), len(self.llm.calls)), (searches, calls))
        response = await self.client.post('/api/v1/chat/completions', json={**payload, 'temperature': .9, 'max_tokens': 55})
        self.assertIn('"status": "completed"', response.text)
        self.assertEqual((self.llm.calls[-1]['temperature'], self.llm.calls[-1]['max_tokens']), (.9, 55))

    async def test_missing_secret_and_revoked_host_fail_before_retrieval(self):
        pid = await self.profile(api_key_env='WORKFLOW_TEST_KEY')
        wid = await self.save(graph(self.kb, pid))
        response = await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.retriever.search_calls, [])
        self.settings.model_allowed_hosts = []
        self.assertEqual((await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})).status_code, 422)
        self.assertEqual(await self.usage(), (0, 0))

    async def test_dual_kb_transforms_prompt_rerank_and_synthesis_are_source_bound(self):
        pid = await self.profile()
        kb2 = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Second'})).json()['id']
        first = await self.evidence(content='FIRST allowed text')
        second = await self.evidence(kb2, 'SECOND must be filtered')
        self.settings.use_reranker = True
        g = graph(self.kb, pid, dual=True)
        extra = [('retrieve2', 'retrieval', {'kb_ids': [kb2], 'top_k': 2, 'hybrid_alpha': .8}),
                 ('filter', 'filter', {'metadata': {'kb_id': self.kb}}),
                 ('dedup', 'deduplicate', {'top_k': 1, 'context_chars': 1000}),
                 ('rerank', 'rerank', {'top_k': 1}),
                 ('prompt', 'prompt', {'system_prompt': 'Be brief and cite.'})]
        for id, kind, config in extra:
            g['nodes'].append({'id': id, 'type': kind, 'config': config, 'label': id, 'position': {'x': 0, 'y': 0}})
        g['nodes'][6]['config'] = {'mode': 'synthesize', 'profile_id': pid, 'temperature': .4, 'max_tokens': 345}
        pairs = [('input', 'retrieve'), ('input', 'retrieve2'), ('retrieve', 'filter'), ('retrieve2', 'filter'),
                 ('filter', 'dedup'), ('dedup', 'rerank'), ('rerank', 'gate'), ('gate', 'prompt'),
                 ('prompt', 'model'), ('prompt', 'model2'), ('model', 'merge'), ('model2', 'merge'), ('merge', 'output')]
        g['edges'] = [{'id': str(i), 'source': a, 'target': b, 'source_port': 'out', 'target_port': 'in'} for i, (a, b) in enumerate(pairs)]
        overlap = asyncio.Event(); searches = 0
        async def search(**kwargs):
            nonlocal searches
            searches += 1; self.retriever.search_calls.append(kwargs)
            if searches == 2: overlap.set()
            await asyncio.wait_for(overlap.wait(), 1)
            return self.retriever.results
        async def rerank(**kwargs):
            self.assertEqual([row['chunk_id'] for row in kwargs['results']], [first['chunk_id']])
            return [{**second, 'content': 'FORGED'}, {**first, 'content': 'FORGED'}]
        self.retriever.search, self.retriever.rerank = search, rerank
        self.llm.content = 'Supported [1] [2]'
        wid = await self.save(g)
        result = await self.run_flow(wid)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual({tuple(call['kb_ids']) for call in self.retriever.search_calls}, {(self.kb,), (kb2,)})
        self.assertEqual(len(self.llm.calls), 3)
        self.assertEqual([source['chunk_id'] for source in result['sources']], [first['chunk_id']])
        for call in self.llm.calls:
            self.assertNotIn('SECOND', json.dumps(call['messages']))
            self.assertNotIn('FORGED', json.dumps(call['messages']))
        synthesis = self.llm.calls[-1]
        self.assertEqual((synthesis['temperature'], synthesis['max_tokens']), (.4, 345))
        self.assertIn('untrusted_candidate_answers', synthesis['messages'][-1]['content'])
        self.assertIn('Be brief', json.dumps(self.llm.calls[0]['messages']))
        self.assertTrue(all(t['status'] == 'completed' for t in result['trace']))

    async def test_recovery_fences_expired_worker_and_preserves_active_worker(self):
        from app.saas.workflow_models import WorkflowRun
        from app.saas.workflow_engine import WorkflowService, Value, begin_write
        from app.saas.quotas import reserve_answer
        from app.saas.models import utcnow
        pid = await self.profile(); wid = await self.save(graph(self.kb, pid))
        active_id, expired_id = str(uuid4()), str(uuid4())
        owner = str(uuid4())
        async with self.app.state.session_factory() as db:
            await begin_write(db)
            for id, seconds in [(active_id, 60), (expired_id, -60)]:
                reservation = await reserve_answer(db, self.workspace_id)
                db.add(WorkflowRun(id=id, workflow_id=wid, workspace_id=self.workspace_id, created_by=self.user['id'],
                    reservation_id=reservation.id, worker_id=owner, lease_until=utcnow() + timedelta(seconds=seconds), graph={}, status='running'))
            await db.commit()
        service = WorkflowService(self.app.state.session_factory, self.settings, self.retriever, self.app.state.model_dispatcher)
        await service.start()
        self.addAsyncCleanup(service.stop)
        active = (await self.client.get(f'/api/v1/workflows/{wid}/runs/{active_id}')).json()
        expired = (await self.client.get(f'/api/v1/workflows/{wid}/runs/{expired_id}')).json()
        self.assertEqual(active['status'], 'running')
        self.assertEqual(expired['status'], 'error')
        self.assertEqual(expired['error'], 'worker_lease_expired')
        service.worker_id = owner
        await service.finalize(expired_id, 'completed', Value(answer='late', sources=[{}]), [], 1)
        self.assertEqual((await self.client.get(f'/api/v1/workflows/{wid}/runs/{expired_id}')).json()['status'], 'error')
        self.assertEqual(await self.usage(), (0, 1))

    async def test_versions_runs_and_viewer_drafts_are_tenant_scoped(self):
        pid = await self.profile(); g = graph(self.kb, pid)
        wid = await self.save(g)
        v1 = (await self.client.post(f'/api/v1/workflows/{wid}/publish')).json()['published_version_id']
        g['nodes'][3]['config']['temperature'] = .9
        await self.client.patch(f'/api/v1/workflows/{wid}', json={'graph': g})
        v2 = (await self.client.post(f'/api/v1/workflows/{wid}/publish')).json()['published_version_id']
        await self.evidence()
        await self.run_flow(wid, use_published=True, version_id=v1)
        self.assertEqual(self.llm.calls[-1]['temperature'], .3)
        await self.client.post(f'/api/v1/workflows/{wid}/rollback', json={'version_id': v1})
        result = await self.run_flow(wid, use_published=True)
        self.assertEqual(self.llm.calls[-1]['temperature'], .3)
        self.assertNotEqual(v1, v2)
        wid2 = await self.save(g)
        self.assertEqual((await self.client.post(f'/api/v1/workflows/{wid2}/rollback', json={'version_id': v1})).status_code, 404)
        self.assertEqual((await self.client.get(f'/api/v1/workflows/{wid2}/runs/{result["id"]}')).status_code, 404)
        viewer, _ = await self.join('read@example.test', paid_fixture=True)
        self.assertEqual((await viewer.get(f'/api/v1/workflows/{wid}')).json()['graph']['nodes'][3]['config']['temperature'], .3)
        self.assertEqual((await viewer.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})).status_code, 403)
        self.assertEqual((await viewer.get(f'/api/v1/workflows/{wid2}')).status_code, 404)
        outsider = await self.new_client(); await self.register_with(outsider, 'isolated@example.test')
        for suffix in [f'/runs/{result["id"]}', '/versions', '/runs']:
            self.assertEqual((await outsider.get(f'/api/v1/workflows/{wid}' + suffix)).status_code, 404)
        self.assertEqual((await outsider.post(f'/api/v1/workflows/{wid}/runs/{result["id"]}/cancel')).status_code, 404)

    async def test_seven_models_and_aggregate_tokens_rejected_before_calls(self):
        pid = await self.profile(); g = graph(self.kb, pid, dual=True)
        g['nodes'][3]['config']['max_tokens'] = 8192
        g['nodes'][5]['config']['max_tokens'] = 8192
        g['nodes'][6]['config'] = {'mode': 'synthesize', 'profile_id': pid, 'max_tokens': 1}
        response = await self.client.post('/api/v1/workflows/validate', json={'graph': g})
        self.assertEqual(response.status_code, 422)
        g = graph(self.kb, pid, dual=True)
        for index in range(5):
            id = f'extra{index}'
            node = copy.deepcopy(g['nodes'][3]); node['id'] = id; g['nodes'].append(node)
            for a, b in [('gate', id), (id, 'merge')]:
                g['edges'].append({'id': a + b, 'source': a, 'target': b, 'source_port': 'out', 'target_port': 'in'})
        self.assertEqual((await self.client.post('/api/v1/workflows/validate', json={'graph': g})).status_code, 422)
        self.assertEqual(self.retriever.search_calls, [])
        self.assertEqual(self.llm.calls, [])

    async def test_evidence_output_returns_grounded_snippets_without_model(self):
        await self.evidence(content='Evidence only answer')
        g = graph(self.kb, '')
        g['nodes'] = [node for node in g['nodes'] if node['id'] != 'model']
        g['edges'] = [edge for edge in g['edges'] if edge['target'] != 'model' and edge['source'] != 'model']
        g['edges'].append({'id': 'direct', 'source': 'gate', 'target': 'output', 'source_port': 'out', 'target_port': 'in'})
        result = await self.run_flow(await self.save(g))
        self.assertEqual(result['status'], 'completed', result)
        self.assertIn('Evidence only answer', result['answer'])
        self.assertEqual(self.llm.calls, [])

    async def test_synthesis_failure_falls_back_to_grounded_candidates(self):
        pid = await self.profile(); await self.evidence()
        g = graph(self.kb, pid, dual=True)
        g['nodes'][6]['config'] = {'mode': 'synthesize', 'profile_id': pid, 'temperature': .4}
        async def model(**kwargs):
            if kwargs['temperature'] == .4: raise httpx.ConnectError('CANARY_secret')
            return {'content': 'Evidence [1]'}
        self.llm.complete = model
        result = await self.run_flow(await self.save(g))
        self.assertEqual(result['status'], 'degraded', result)
        self.assertIn('Evidence', result['answer'])
        trace = next(item for item in result['trace'] if item['node_id'] == 'merge')
        self.assertEqual(trace['status'], 'degraded')
        self.assertEqual(trace['error'], 'upstream_disconnected')
        self.assertNotIn('CANARY', json.dumps(result))

    async def test_branch_local_numeric_citations_are_remapped_to_distinct_sources(self):
        pid = await self.profile()
        kb2 = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Second'})).json()['id']
        one = await self.evidence(content='One'); two = await self.evidence(kb2, 'Two')
        g = graph(self.kb, pid, dual=True)
        g['nodes'].append({'id': 'retrieve2', 'type': 'retrieval', 'label': 'second', 'position': {'x': 0, 'y': 0}, 'config': {'kb_ids': [kb2]}})
        g['edges'] = [edge for edge in g['edges'] if edge['target'] != 'model2']
        for a, b in [('input', 'retrieve2'), ('retrieve2', 'model2')]:
            g['edges'].append({'id': a + b, 'source': a, 'target': b, 'source_port': 'out', 'target_port': 'in'})
        self.llm.content = 'Local [1]'
        result = await self.run_flow(await self.save(g))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['answer'], 'Local [1]\n\nLocal [2]')
        self.assertEqual([row['chunk_id'] for row in result['sources']], [one['chunk_id'], two['chunk_id']])

    async def test_private_http_opt_in_and_redirect_errors_do_not_echo_secrets(self):
        pid = await self.profile()
        self.settings.model_allowed_hosts.append('127.0.0.1')
        patch_body = {'base_url': 'http://127.0.0.1:9999/v1'}
        self.assertEqual((await self.client.patch('/api/v1/model-profiles/' + pid, json=patch_body)).status_code, 422)
        self.settings.model_allow_http = True
        self.assertEqual((await self.client.patch('/api/v1/model-profiles/' + pid, json=patch_body)).status_code, 422)
        self.settings.model_private_hosts = ['127.0.0.1']
        self.assertEqual((await self.client.patch('/api/v1/model-profiles/' + pid, json=patch_body)).status_code, 200)
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(307, headers={'Location': 'https://evil.test/CANARY_secret'}, text='CANARY_secret')
        from app.saas.llm import OpenAICompatibleLLM
        client = httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        self.app.state.model_dispatcher.llm = OpenAICompatibleLLM(self.settings, client=client)
        result = await self.client.post('/api/v1/model-profiles/' + pid + '/test')
        self.assertFalse(result.json()['ok'])
        self.assertEqual(result.json()['error'], 'upstream_rejected')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('CANARY', result.text)
        self.assertNotIn('evil.test', result.text)

    async def test_immediate_cancel_and_remote_worker_cancel_release_reservations(self):
        pid = await self.profile(); await self.evidence()
        wid = await self.save(graph(self.kb, pid))
        async def slow(**kwargs): await asyncio.sleep(30)
        self.llm.complete = slow
        rid = (await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})).json()['id']
        await self.client.post(f'/api/v1/workflows/{wid}/runs/{rid}/cancel')
        self.assertEqual((await self.poll(wid, rid))['status'], 'canceled')
        from app.saas.workflow_models import WorkflowRun
        from app.saas.workflow_engine import begin_write
        rid = (await self.client.post(f'/api/v1/workflows/{wid}/run', json={'query': 'q', 'use_published': False})).json()['id']
        async with self.app.state.session_factory() as db:
            await begin_write(db)
            row = await db.get(WorkflowRun, rid)
            row.cancel_requested = True; row.status = 'canceling'
            await db.commit()
        self.assertEqual((await self.poll(wid, rid))['status'], 'canceled')
        self.assertEqual(await self.usage(), (0, 0))

    async def test_valid_citation_only_output_is_not_a_usable_billed_answer(self):
        pid = await self.profile(); hit = await self.evidence()
        wid = await self.save(graph(self.kb, pid))
        for answer in ['[1]', '[source:' + hit['chunk_id'] + ']']:
            self.llm.content = answer
            result = await self.run_flow(wid)
            self.assertEqual(result['status'], 'error', result)
            self.assertEqual(result['error'], 'upstream_protocol_error')
        self.assertEqual(await self.usage(), (0, 0))

    async def test_periodic_recovery_survives_transient_database_failure(self):
        from sqlalchemy.exc import OperationalError
        from app.saas.workflow_models import WorkflowRun
        from app.saas.workflow_engine import WorkflowService, begin_write
        from app.saas.quotas import reserve_answer
        from app.saas.models import utcnow
        pid = await self.profile(); wid = await self.save(graph(self.kb, pid))
        rid = str(uuid4())
        async with self.app.state.session_factory() as db:
            await begin_write(db)
            reservation = await reserve_answer(db, self.workspace_id)
            db.add(WorkflowRun(id=rid, workflow_id=wid, workspace_id=self.workspace_id, created_by=self.user['id'],
                reservation_id=reservation.id, worker_id=str(uuid4()), lease_until=utcnow() - timedelta(seconds=60), graph={}, status='running'))
            await db.commit()
        service = WorkflowService(self.app.state.session_factory, self.settings, self.retriever, self.app.state.model_dispatcher)
        self.settings.workflow_lease_seconds = .02
        real_recover, attempts, done = service.recover_expired, [], asyncio.Event()
        async def flaky():
            attempts.append(True)
            if len(attempts) == 1: raise OperationalError('synthetic', {}, Exception('CANARY'))
            await real_recover(); done.set()
        service.recover_expired = flaky
        loop = asyncio.create_task(service._recovery_loop())
        try:
            try:
                await asyncio.wait_for(done.wait(), 2)
            except TimeoutError:
                pass
            self.assertTrue(done.is_set(), 'Janitor stopped after its first transient DB failure')
            self.assertEqual((await self.client.get(f'/api/v1/workflows/{wid}/runs/{rid}')).json()['status'], 'error')
            self.assertEqual(await self.usage(), (0, 0))
        finally:
            loop.cancel(); await asyncio.gather(loop, return_exceptions=True)

    async def test_evidence_minimum_is_checked_against_effective_model_context(self):
        pid = await self.profile()
        await self.evidence(content='A' * 2000)
        await self.evidence(content='B' * 100)
        self.settings.chat_context_chars = 1000
        g = graph(self.kb, pid)
        g['nodes'][1]['config']['context_chars'] = 5000
        g['nodes'][2]['config']['min_sources'] = 2
        result = await self.run_flow(await self.save(g))
        self.assertEqual(result['status'], 'insufficient_evidence', result)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(await self.usage(), (0, 0))

    async def test_blocked_branch_minimum_does_not_discard_surviving_synthesis(self):
        pid = await self.profile()
        await self.evidence(content='A' * 2000); await self.evidence(content='B' * 100)
        self.settings.chat_context_chars = 1000
        g = graph(self.kb, pid, dual=True)
        g['nodes'][1]['config']['context_chars'] = 5000
        g['nodes'][2]['config']['min_sources'] = 2
        g['nodes'][6]['config'] = {'mode': 'synthesize', 'profile_id': pid}
        next(edge for edge in g['edges'] if edge['target'] == 'model2')['source'] = 'retrieve'
        self.llm.content = 'Supported [1]'
        result = await self.run_flow(await self.save(g))
        self.assertEqual(result['status'], 'degraded', result)
        self.assertEqual(result['answer'], 'Supported [1]')
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(await self.usage(), (1, 0))
