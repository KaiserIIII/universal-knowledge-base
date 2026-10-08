"""Concurrent typed DAG execution with SQL-fenced quota and worker leases.

Only immutable run snapshots enter the executor. Branches hold independent read
sessions; no AsyncSession or mutable model settings cross task boundaries.
"""
import asyncio
import copy
import json
import re
from functools import wraps
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from .chat import (INSUFFICIENT, MAX_OUTPUT_CHARS, build_grounded_context,
                   error_category, has_usable_answer, validate_citations)
from .dependencies import lock_workspace
from .llm import OutputLimitError, UpstreamProtocolError
from .models import new_id, utcnow
from .quotas import finish_answer, reserve_answer
from .retrieval import scoped_search
from .security import aware
from .workflow_models import WorkflowRun

ACTIVE = {'queued', 'running', 'canceling'}


@dataclass
class Value:
    sources: list = field(default_factory=list)
    answer: str = ''
    prompts: list = field(default_factory=list)
    blocked: bool = False
    errors: list = field(default_factory=list)
    invalid: bool = False
    degraded: bool = False
    min_sources: int = 1


def unique_sources(values):
    rows, seen = [], set()
    for value in values:
        for source in value.sources:
            if source['chunk_id'] not in seen:
                rows.append(source); seen.add(source['chunk_id'])
    return rows


def bounded(query, sources, limit):
    return build_grounded_context(query, sources, context_chars=limit)[1]


def canonical_citations(content, sources):
    """Numeric citations are local to each model prompt, so bind before merging."""
    safe, invalid = validate_citations(content, sources)
    return re.sub(r'\[(\d+)\]', lambda match: '[source:' + sources[int(match[1]) - 1]['chunk_id'] + ']', safe), invalid


async def begin_write(db):
    if db.bind.dialect.name == 'sqlite':
        await db.execute(text('BEGIN IMMEDIATE'))


def finish_transaction(function):
    """Do not interrupt a DBAPI operation while its worker thread holds a lock."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        task = asyncio.create_task(function(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    return wrapped


class WorkflowService:
    def __init__(self, session_factory, settings, retriever, dispatcher):
        self.sessions, self.settings = session_factory, settings
        self.retriever, self.dispatcher = retriever, dispatcher
        self.worker_id = new_id()
        self.tasks = {}
        self.signals = {}
        self._janitor = None

    async def start(self):
        await self.recover_expired()
        self._janitor = asyncio.create_task(self._recovery_loop())

    async def stop(self):
        if self._janitor:
            self._janitor.cancel()
            await asyncio.gather(self._janitor, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Also cover a task canceled before its coroutine entered the try/finally.
        async with self.sessions() as db:
            rows = (await db.scalars(select(WorkflowRun).where(WorkflowRun.worker_id == self.worker_id,
                WorkflowRun.status.in_(ACTIVE)))).all()
        for row in rows:
            trace = [{**item, 'status': 'canceled' if item['status'] in {'pending', 'running'} else item['status']} for item in row.trace]
            await self.finalize(row.id, 'canceled', Value(), trace, row.duration_ms, 'worker_stopped')

    async def submit(self, db, access, workflow_id, version_id, graph, profiles, query):
        reservation = await reserve_answer(db, access.workspace_id)
        run = WorkflowRun(id=new_id(), workflow_id=workflow_id, workspace_id=access.workspace_id,
            version_id=version_id, created_by=access.user_id, reservation_id=reservation.id,
            worker_id=self.worker_id, lease_until=utcnow() + timedelta(seconds=self.settings.workflow_lease_seconds),
            graph=copy.deepcopy(graph), trace=[self.trace_node(node) for node in graph['nodes']])
        db.add(run); await db.commit()
        # Snapshot the response before scheduling so the 202 never exposes ORM races.
        self.signals[run.id] = asyncio.Event()
        task = asyncio.create_task(self._run(run.id, access, copy.deepcopy(graph), copy.deepcopy(profiles), query))
        self.tasks[run.id] = task
        def settled(done):
            self.tasks.pop(run.id, None)
            self.signals.pop(run.id, None)
            if not done.cancelled():
                done.exception()  # lease recovery handles persistence failure; never log raw SQL payloads
        task.add_done_callback(settled)
        return run

    def cancel(self, run_id):
        # Wake the owner immediately when local. Other workers observe the same
        # durable flag on the next heartbeat, including before their first step.
        signal = self.signals.get(run_id)
        if signal is not None: signal.set()

    @staticmethod
    def trace_node(node):
        config = node['config']
        refs = {key: config[key] for key in ('kb_ids', 'profile_id', 'top_k', 'context_chars', 'temperature',
            'top_p', 'max_tokens', 'timeout_seconds', 'mode', 'min_sources') if key in config}
        return {'node_id': node['id'], 'label': node['label'], 'status': 'pending',
                'duration_ms': 0, 'result_count': 0, 'config_refs': refs}

    @finish_transaction
    async def _update(self, run_id, trace=None):
        async with self.sessions() as db:
            await begin_write(db)
            row = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            if row is None or row.worker_id != self.worker_id or row.status not in ACTIVE:
                return False
            if row.cancel_requested:
                return False
            row.status = 'running'
            row.lease_until = utcnow() + timedelta(seconds=self.settings.workflow_lease_seconds)
            if trace is not None: row.trace = copy.deepcopy(trace)
            await db.commit()
            return True

    async def _watch(self, run_id, task, traces, stop):
        signal = self.signals[run_id]
        while not stop.is_set():
            try:
                await asyncio.wait_for(signal.wait(), .2)
            except TimeoutError:
                pass
            if stop.is_set(): return
            signal.clear()
            if not await self._update(run_id, list(traces.values())):
                if not stop.is_set(): task.cancel()
                return

    async def _run(self, run_id, access, graph, profiles, query):
        started = monotonic()
        traces = {node['id']: self.trace_node(node) for node in graph['nodes']}
        nodes = {node['id']: node for node in graph['nodes']}
        parents = {id: [] for id in nodes}
        for edge in graph['edges']: parents[edge['target']].append(edge['source'])
        tasks = {}
        status, error, result = 'error', None, Value()
        watch_stop = asyncio.Event()
        watcher = asyncio.create_task(self._watch(run_id, asyncio.current_task(), traces, watch_stop))
        try:
            if not await self._update(run_id):
                raise asyncio.CancelledError()

            async def execute(id):
                values = await asyncio.gather(*(tasks[parent] for parent in parents[id]))
                trace, node_started = traces[id], monotonic()
                trace['status'] = 'running'
                try:
                    value = await self._node(access, nodes[id], values, profiles, query)
                    trace['status'] = ('error' if value.errors and not value.answer else
                        'insufficient_evidence' if value.blocked else 'degraded' if value.degraded else 'completed')
                    trace['result_count'] = len(value.sources) if not value.answer else 1
                    if value.errors: trace['error'] = value.errors[0]
                    return value
                except asyncio.CancelledError:
                    trace['status'] = 'canceled'
                    raise
                except Exception as exc:
                    code = error_category(exc)
                    if nodes[id]['type'] in {'retrieval', 'rerank'}:
                        code = 'retrieval_timeout' if isinstance(exc, TimeoutError) else 'retrieval_unavailable'
                    trace['status'], trace['error'] = 'error', code
                    return Value(errors=[code])
                finally:
                    trace['duration_ms'] = round((monotonic() - node_started) * 1000, 3)

            # Tasks resolve dependencies concurrently; dict is populated before
            # the event loop first enters execute (there are no awaits here).
            for id in nodes: tasks[id] = asyncio.create_task(execute(id))
            output_id = next(id for id, node in nodes.items() if node['type'] == 'output')
            async with asyncio.timeout(self.settings.workflow_timeout_seconds):
                result = await tasks[output_id]
            if result.answer.strip() and result.sources:
                status = 'degraded' if result.degraded or result.errors else 'completed'
            elif result.errors:
                status, error = 'error', result.errors[0]
            else:
                status, result.answer = 'insufficient_evidence', INSUFFICIENT
        except asyncio.CancelledError:
            status, error = 'canceled', 'run_canceled'
        except TimeoutError:
            status, error = 'timed_out', 'workflow_timeout'
        except Exception:
            status, error = 'error', 'workflow_error'
        finally:
            watch_stop.set()
            self.signals[run_id].set()
            for task in tasks.values():
                if not task.done(): task.cancel()
            await asyncio.gather(watcher, *tasks.values(), return_exceptions=True)
            for trace in traces.values():
                if trace['status'] in {'running', 'pending'}:
                    trace['status'] = 'canceled' if status in {'canceled', 'timed_out'} else 'skipped'
            await asyncio.shield(self.finalize(run_id, status, result, list(traces.values()),
                round((monotonic() - started) * 1000, 3), error))

    async def _node(self, access, node, values, profiles, query):
        kind, config = node['type'], node['config']
        sources = unique_sources(values)
        errors = [error for value in values for error in value.errors]
        invalid = any(value.invalid for value in values)
        blocked = any(value.blocked for value in values)
        prompts = [prompt for value in values for prompt in value.prompts]
        degraded = any(value.degraded for value in values)
        min_sources = max((value.min_sources for value in values), default=1)
        if kind == 'input': return Value()
        if kind == 'retrieval':
            async with self.sessions() as db:
                sources = await scoped_search(db, access, self.retriever, query=query, **{
                    key: config[key] for key in ('kb_ids', 'top_k', 'hybrid_alpha', 'score_threshold', 'enable_reranker')})
            sources = [source for source in sources if source['score'] >= config['score_threshold']]
            return Value(sources=bounded(query, sources, config['context_chars']))
        if kind in {'merge', 'output'}:
            usable = [value for value in values if value.answer.strip() and value.sources]
            if kind == 'output' and not usable and sources and not blocked and not errors:
                sources = bounded(query, sources, min(self.settings.chat_context_chars, 24000))
                if len(sources) < min_sources: return Value(blocked=True, min_sources=min_sources)
                answer = '\n\n'.join(source['content'] + ' [' + str(index + 1) + ']' for index, source in enumerate(sources))
                return Value(sources=sources, answer=answer, min_sources=min_sources)
            if not usable:
                return Value(sources=sources, blocked=True, errors=errors, invalid=invalid)
            sources = unique_sources(usable)
            # A blocked branch contributes neither an answer nor its evidence
            # contract to synthesis of independently usable surviving branches.
            min_sources = max(value.min_sources for value in usable)
            degraded = degraded or bool(errors) or any(value.blocked for value in values)
            if kind == 'merge' and config['mode'] == 'synthesize':
                # Candidate answers stay inside an untrusted user-data envelope.
                try:
                    generated = await self._generate(query, sources, profiles[config['profile_id']],
                        {key: config[key] for key in ('temperature', 'max_tokens')}, [],
                        candidates=[value.answer for value in usable], degraded=degraded, prior_invalid=invalid,
                        min_sources=min_sources)
                    generated.errors = errors
                    return generated
                except Exception as exc:
                    errors.append(error_category(exc))
                    degraded = True
            answer = '\n\n'.join(value.answer for value in usable)
            if len(answer) > MAX_OUTPUT_CHARS: raise OutputLimitError()
            sources = bounded(query, sources, 100000)
            answer, bad = validate_citations(answer, sources)
            if not answer.strip(): raise UpstreamProtocolError()
            if kind == 'output':
                source_indices = {source['chunk_id']: index + 1 for index, source in enumerate(sources)}
                answer = re.sub(r'\[source:([^\]]+)\]', lambda m: '[' + str(source_indices[m[1]]) + ']', answer)
            return Value(sources=sources, answer=answer, invalid=invalid or bad, degraded=degraded, errors=errors,
                         min_sources=min_sources)
        # A failed/closed evidence dependency cannot be bypassed by another prompt.
        if errors: return Value(errors=errors, blocked=blocked)
        if blocked: return Value(blocked=True)
        if kind == 'filter':
            sources = [source for source in sources if all(source['metadata'].get(key) == value for key, value in config['metadata'].items())]
        elif kind == 'deduplicate':
            sources = bounded(query, sources[:config['top_k']], config['context_chars'])
        elif kind == 'rerank':
            ranked = await self.retriever.rerank(query=query, results=copy.deepcopy(sources), top_k=config['top_k'])
            known = {row['chunk_id']: row for row in sources}
            # The reranker may select/reorder IDs only; it cannot introduce or
            # replace evidence previously filtered by the graph.
            ids = list(dict.fromkeys(row.get('chunk_id') for row in ranked if isinstance(row, dict)))
            sources = [known[id] for id in ids if id in known][:config['top_k']]
        elif kind == 'evidence':
            min_sources = max(min_sources, config['min_sources'])
            if len(sources) < min_sources: return Value(blocked=True, min_sources=min_sources)
        elif kind == 'prompt':
            prompts = [*prompts, config['system_prompt']]
        elif kind == 'model':
            if not sources: return Value(blocked=True)
            return await self._generate(query, sources, profiles[config['profile_id']],
                {key: config[key] for key in ('temperature', 'top_p', 'max_tokens', 'timeout_seconds')}, prompts,
                min_sources=min_sources)
        return Value(sources=sources, prompts=prompts, invalid=invalid, degraded=degraded, min_sources=min_sources)

    async def _generate(self, query, sources, profile, parameters, prompts, *, candidates=None, degraded=False,
                        prior_invalid=False, min_sources=1):
        messages, sources = build_grounded_context(query, sources, context_chars=self.settings.chat_context_chars)
        if len(sources) < min_sources: return Value(blocked=True, min_sources=min_sources)
        if prompts:
            messages.insert(1, {'role': 'system', 'content': '\n'.join(prompts)[:8000]})
        if candidates is not None:
            data = json.loads(messages[-1]['content'])
            data['untrusted_candidate_answers'] = candidates
            messages[-1]['content'] = json.dumps(data, ensure_ascii=False)
            messages[0]['content'] += ' Synthesize candidates only against the supplied evidence; agreement is not factual confidence.'
        result = await self.dispatcher.complete(profile=profile, messages=messages, **parameters)
        if not isinstance(result, dict) or not isinstance(result.get('content'), str) or not result['content'].strip():
            raise UpstreamProtocolError()
        if len(result['content']) > MAX_OUTPUT_CHARS: raise OutputLimitError()
        answer, invalid = canonical_citations(result['content'], sources)
        # References alone are not a usable answer, even when every marker is
        # valid. Do not consume an answer reservation for an empty assertion.
        if not has_usable_answer(answer): raise UpstreamProtocolError()
        return Value(sources=sources, answer=answer, invalid=invalid or prior_invalid, degraded=degraded,
                     min_sources=min_sources)

    async def finalize(self, run_id, status, value, trace, duration_ms, error=None):
        async with self.sessions() as db:
            await begin_write(db)
            workspace_id = await db.scalar(select(WorkflowRun.workspace_id).where(WorkflowRun.id == run_id))
            if workspace_id is None: return
            await lock_workspace(db, workspace_id)
            row = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            if row is None or row.worker_id != self.worker_id or row.status not in ACTIVE:
                return  # stale executor is fenced after recovery or terminal persistence
            if row.cancel_requested: status, error = 'canceled', 'run_canceled'
            row.status, row.error, row.trace = status, error, trace
            row.duration_ms, row.finished_at = duration_ms, utcnow()
            if status in {'completed', 'degraded', 'insufficient_evidence'}:
                row.answer, row.sources, row.invalid_citations = value.answer, value.sources, value.invalid
            await finish_answer(db, row.workspace_id, row.reservation_id, succeeded=status in {'completed', 'degraded'})
            await db.commit()

    @finish_transaction
    async def recover_expired(self):
        """Release only expired leases; active workers retain their reservations."""
        async with self.sessions() as db:
            ids = (await db.scalars(select(WorkflowRun.id).where(WorkflowRun.status.in_(ACTIVE), WorkflowRun.lease_until < utcnow()))).all()
        for id in ids:
            async with self.sessions() as db:
                await begin_write(db)
                workspace_id = await db.scalar(select(WorkflowRun.workspace_id).where(WorkflowRun.id == id))
                if workspace_id is None: continue
                await lock_workspace(db, workspace_id)
                row = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == id).with_for_update())
                if row.status not in ACTIVE or aware(row.lease_until) >= utcnow(): continue
                row.status = 'canceled' if row.cancel_requested else 'error'
                row.error, row.finished_at = 'worker_lease_expired', utcnow()
                row.trace = [{**item, 'status': 'canceled' if item['status'] in {'pending', 'running'} else item['status']} for item in row.trace]
                await finish_answer(db, row.workspace_id, row.reservation_id, succeeded=False)
                await db.commit()

    async def _recovery_loop(self):
        while True:
            await asyncio.sleep(self.settings.workflow_lease_seconds)
            try:
                await self.recover_expired()
            except DBAPIError:
                # A busy/disconnected database must not permanently disable
                # recovery. Retry on the bounded lease interval; never log SQL
                # parameters or swallow task cancellation.
                continue
