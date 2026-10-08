"""Bounded visual DAG schema, tenant validation, drafts and immutable versions."""
import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from .dependencies import get_access, get_db, require_role, valid_id
from .model_profiles import load_profile
from .models import utcnow
from .retrieval import validate_kb_ids
from .workflow_models import Workflow, WorkflowRun, WorkflowVersion
from .file_evidence import validate_files

router = APIRouter(prefix='/api/v1/workflows')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RetrievalConfig(Strict):
    kb_ids: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=6, ge=1, le=50)
    hybrid_alpha: float = Field(default=.5, ge=0, le=1, allow_inf_nan=False)
    score_threshold: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    enable_reranker: bool = False
    context_chars: int = Field(default=16000, ge=1000, le=50000)


class FilterConfig(Strict):
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=20)


class FilesConfig(Strict):
    kb_ids: list[str] = Field(default_factory=list, max_length=20)
    doc_ids: list[str] = Field(default_factory=list, max_length=50)
    top_k: int = Field(default=12, ge=1, le=50)
    context_chars: int = Field(default=16000, ge=1000, le=50000)


class DedupConfig(Strict):
    top_k: int = Field(default=12, ge=1, le=50)
    context_chars: int = Field(default=20000, ge=1000, le=50000)


class RerankConfig(Strict):
    top_k: int = Field(default=6, ge=1, le=50)


class EvidenceConfig(Strict):
    min_sources: int = Field(default=1, ge=1, le=50)


class PromptConfig(Strict):
    system_prompt: str = Field(default='', max_length=8000)


class ModelConfig(Strict):
    profile_id: str = Field(default='', max_length=36)
    temperature: float = Field(default=.2, ge=0, le=2, allow_inf_nan=False)
    top_p: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    timeout_seconds: float = Field(default=60, ge=.05, le=120, allow_inf_nan=False)


class MergeConfig(Strict):
    mode: Literal['concatenate', 'synthesize'] = 'concatenate'
    profile_id: str = Field(default='', max_length=36)
    temperature: float = Field(default=.2, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int = Field(default=1024, ge=1, le=8192)


CONFIGS = {'input': Strict, 'retrieval': RetrievalConfig, 'files': FilesConfig, 'filter': FilterConfig, 'deduplicate': DedupConfig,
           'rerank': RerankConfig, 'evidence': EvidenceConfig, 'prompt': PromptConfig, 'model': ModelConfig,
           'merge': MergeConfig, 'output': Strict}
TYPES = {'input': ('query', set()), 'retrieval': ('evidence', {'query'}), 'files': ('evidence', {'query'}),
         'filter': ('evidence', {'evidence'}), 'deduplicate': ('evidence', {'evidence'}),
         'rerank': ('evidence', {'evidence'}), 'evidence': ('evidence', {'evidence'}),
         'prompt': ('prompt', {'query', 'evidence'}), 'model': ('answer', {'prompt', 'evidence'}),
         'merge': ('answer', {'answer'}), 'output': (None, {'answer', 'evidence'})}


class Position(Strict):
    x: float = Field(ge=-5000, le=5000, allow_inf_nan=False)
    y: float = Field(ge=-5000, le=5000, allow_inf_nan=False)


class Node(Strict):
    id: str = Field(min_length=1, max_length=100)
    type: str = Field(max_length=30)
    label: str = Field(default='', max_length=200)
    position: Position
    config: dict


class Edge(Strict):
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    source_port: str = Field(max_length=20)
    target_port: str = Field(max_length=20)


class Graph(Strict):
    nodes: list[Node] = Field(max_length=40)
    edges: list[Edge] = Field(max_length=80)


async def validate_graph(db, access, raw, settings, *, complete=True, resolve=False):
    try:
        if len(json.dumps(raw, allow_nan=False)) > 200000:
            raise ValueError()
        graph = Graph.model_validate(raw).model_dump()
        ids = set()
        for node in graph['nodes']:
            if node['type'] not in CONFIGS or node['id'] in ids:
                raise ValueError()
            ids.add(node['id'])
            # Drafts may omit selections/edges, but unknown settings (including keys) never persist.
            node['config'] = CONFIGS[node['type']].model_validate(node['config']).model_dump()
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(422, 'Invalid or oversized graph configuration') from None
    profiles = {}
    for node in graph['nodes']:
        config = node['config']
        if config.get('kb_ids'):
            await validate_kb_ids(db, access, config['kb_ids'])
        if node['type'] == 'files':
            await validate_files(db, access, config['kb_ids'], config['doc_ids'], complete=complete)
        if config.get('profile_id'):
            profiles[config['profile_id']] = await load_profile(db, access, config['profile_id'], settings, resolve=resolve)
    if not complete:
        return graph, profiles
    nodes = {node['id']: node for node in graph['nodes']}
    parents, children = {id: [] for id in nodes}, {id: [] for id in nodes}
    seen, edge_ids = set(), set()
    for edge in graph['edges']:
        source, target = edge['source'], edge['target']
        if (source not in nodes or target not in nodes or (source, target) in seen or edge['id'] in edge_ids
                or edge['source_port'] != 'out' or edge['target_port'] != 'in'
                or TYPES[nodes[source]['type']][0] not in TYPES[nodes[target]['type']][1]):
            raise HTTPException(422, 'Invalid, duplicate or incompatible graph edge')
        parents[target].append(source); children[source].append(target)
        seen.add((source, target)); edge_ids.add(edge['id'])
    inputs = [id for id, node in nodes.items() if node['type'] == 'input']
    outputs = [id for id, node in nodes.items() if node['type'] == 'output']
    if len(inputs) != 1 or len(outputs) != 1:
        raise HTTPException(422, 'Exactly one input and output are required')
    def reached(start, adjacency):
        seen, todo = set(), [start]
        while todo:
            id = todo.pop()
            if id not in seen:
                seen.add(id); todo.extend(adjacency[id])
        return seen
    if reached(inputs[0], children) != set(nodes) or reached(outputs[0], parents) != set(nodes):
        raise HTTPException(422, 'Every node must connect input to output')
    indegree = {id: len(prev) for id, prev in parents.items()}
    todo, order = [id for id, count in indegree.items() if count == 0], []
    while todo:
        id = todo.pop(0); order.append(id)
        for target in children[id]:
            indegree[target] -= 1
            if indegree[target] == 0: todo.append(target)
    if len(order) != len(nodes):
        raise HTTPException(422, 'Graph contains a cycle')
    calls, tokens = 0, 0
    for node in nodes.values():
        config, kind = node['config'], node['type']
        if kind == 'retrieval' and not config['kb_ids']:
            raise HTTPException(422, 'Select a knowledge base')
        if kind == 'model' or (kind == 'merge' and config['mode'] == 'synthesize'):
            if not config['profile_id']:
                raise HTTPException(422, 'Select a model profile')
            calls += 1; tokens += config['max_tokens']
        if kind == 'rerank' or (kind == 'retrieval' and config['enable_reranker']):
            if not settings.use_reranker:
                raise HTTPException(422, 'Local reranking capability is disabled')
    if calls > 6 or tokens > settings.workflow_output_token_budget:
        raise HTTPException(422, 'Workflow model or output-token budget exceeded')
    return graph, profiles


async def scoped_workflow(db, access, id):
    row = await db.scalar(select(Workflow).where(Workflow.id == valid_id(id), Workflow.workspace_id == access.workspace_id))
    if row is None or (access.role == 'viewer' and not row.published_version_id):
        raise HTTPException(404, 'Resource not found')
    return row


async def scoped_version(db, workflow, id):
    row = await db.scalar(select(WorkflowVersion).where(WorkflowVersion.id == valid_id(id), WorkflowVersion.workflow_id == workflow.id))
    if row is None: raise HTTPException(404, 'Resource not found')
    return row


async def workflow_view(db, access, row):
    graph = row.graph
    if access.role == 'viewer':
        graph = (await scoped_version(db, row, row.published_version_id)).graph
    return {'id': row.id, 'name': row.name, 'graph': graph, 'published_version_id': row.published_version_id,
            'created_at': row.created_at, 'updated_at': row.updated_at}


def run_view(row):
    return {key: getattr(row, key) for key in ('id', 'workflow_id', 'version_id', 'status', 'answer', 'sources', 'trace',
        'duration_ms', 'error', 'invalid_citations', 'created_at', 'finished_at')}


class CreateWorkflow(Strict):
    name: str = Field(min_length=1, max_length=200)
    graph: dict


class PatchWorkflow(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    graph: dict | None = None


class ValidateRequest(Strict):
    graph: dict


class VersionRequest(Strict):
    version_id: str


class RunRequest(Strict):
    query: str = Field(min_length=1, max_length=8000)
    use_published: bool = True
    version_id: str | None = None


@router.post('/validate')
async def validate(body: ValidateRequest, request: Request, access=Depends(get_access), db=Depends(get_db)):
    await validate_graph(db, access, body.graph, request.app.state.settings)
    return {'valid': True, 'errors': []}


@router.get('')
async def list_workflows(access=Depends(get_access), db=Depends(get_db)):
    query = select(Workflow).where(Workflow.workspace_id == access.workspace_id)
    if access.role == 'viewer': query = query.where(Workflow.published_version_id.is_not(None))
    rows = (await db.scalars(query.order_by(Workflow.updated_at.desc()))).all()
    return {'workflows': [await workflow_view(db, access, row) for row in rows]}


@router.post('', status_code=201)
async def create(body: CreateWorkflow, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin', 'editor')
    graph, _ = await validate_graph(db, access, body.graph, request.app.state.settings, complete=False)
    row = Workflow(workspace_id=access.workspace_id, name=body.name, graph=graph)
    db.add(row); await db.commit()
    return await workflow_view(db, access, row)


@router.get('/{id}')
async def detail(id: str, access=Depends(get_access), db=Depends(get_db)):
    return await workflow_view(db, access, await scoped_workflow(db, access, id))


@router.patch('/{id}')
async def edit(id: str, body: PatchWorkflow, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin', 'editor')
    row = await scoped_workflow(db, access, id)
    if body.graph is not None:
        row.graph, _ = await validate_graph(db, access, body.graph, request.app.state.settings, complete=False)
    if body.name is not None: row.name = body.name
    row.updated_at = utcnow(); await db.commit()
    return await workflow_view(db, access, row)


@router.post('/{id}/publish')
async def publish(id: str, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin', 'editor')
    row = await scoped_workflow(db, access, id)
    graph, _ = await validate_graph(db, access, row.graph, request.app.state.settings)
    number = (await db.scalar(select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == id)) or 0) + 1
    version = WorkflowVersion(workflow_id=id, version=number, graph=graph)
    db.add(version); await db.flush()
    row.published_version_id, row.updated_at = version.id, utcnow()
    await db.commit()
    return await workflow_view(db, access, row)


@router.get('/{id}/versions')
async def versions(id: str, access=Depends(get_access), db=Depends(get_db)):
    await scoped_workflow(db, access, id)
    rows = (await db.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == id).order_by(WorkflowVersion.version.desc()))).all()
    return {'versions': [{'id': row.id, 'version': row.version, 'created_at': row.created_at} for row in rows]}


@router.post('/{id}/rollback')
async def rollback(id: str, body: VersionRequest, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin', 'editor')
    row = await scoped_workflow(db, access, id)
    version = await scoped_version(db, row, body.version_id)
    row.published_version_id, row.updated_at = version.id, utcnow()
    await db.commit()
    return await workflow_view(db, access, row)


@router.post('/{id}/run', status_code=202)
async def run(id: str, body: RunRequest, request: Request, access=Depends(get_access), db=Depends(get_db)):
    row = await scoped_workflow(db, access, id)
    if not body.query.strip(): raise HTTPException(422, 'Query must contain text')
    if access.role == 'viewer' and not body.use_published and not body.version_id:
        raise HTTPException(403, 'Viewers can only run published versions')
    version_id = body.version_id or (row.published_version_id if body.use_published else None)
    if body.use_published and not version_id: raise HTTPException(422, 'Publish this workflow before running it')
    graph = (await scoped_version(db, row, version_id)).graph if version_id else row.graph
    graph, profiles = await validate_graph(db, access, graph, request.app.state.settings, resolve=True)
    return run_view(await request.app.state.workflow_service.submit(db, access, row.id, version_id, graph, profiles, body.query))


async def scoped_run(db, access, id, run_id):
    await scoped_workflow(db, access, id)
    row = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == valid_id(run_id), WorkflowRun.workflow_id == id,
                                                   WorkflowRun.workspace_id == access.workspace_id))
    if row is None: raise HTTPException(404, 'Resource not found')
    return row


@router.get('/{id}/runs')
async def runs(id: str, access=Depends(get_access), db=Depends(get_db)):
    await scoped_workflow(db, access, id)
    rows = (await db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == id).order_by(WorkflowRun.created_at.desc()).limit(100))).all()
    return {'runs': [run_view(row) for row in rows]}


@router.get('/{id}/runs/{run_id}')
async def get_run(id: str, run_id: str, access=Depends(get_access), db=Depends(get_db)):
    return run_view(await scoped_run(db, access, id, run_id))


@router.post('/{id}/runs/{run_id}/cancel')
async def cancel(id: str, run_id: str, request: Request, access=Depends(get_access), db=Depends(get_db)):
    row = await scoped_run(db, access, id, run_id)
    if access.role == 'viewer' and row.created_by != access.user_id:
        raise HTTPException(403, 'Only the run author can cancel this run')
    from .workflow_engine import ACTIVE
    if row.status in ACTIVE:
        row.cancel_requested, row.status = True, 'canceling'
        await db.commit()
        request.app.state.workflow_service.cancel(run_id)
    return run_view(row)
