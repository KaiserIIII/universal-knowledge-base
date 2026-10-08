# Enterprise Knowledge SaaS Implementation Plan

> **For agentic workers:** Use subagent-driven-development task by task, with scoped specification and quality review. Track completion in `.superpowers/sdd/2026-10-07-enterprise-saas/progress.md`.

**Goal:** Deliver the user-approved multi-tenant SaaS design in `docs/superpowers/specs/2026-10-07-multitenant-saas-design.md` and publish a verified GitHub draft PR.

**Architecture:** Keep the existing knowledge ORM and retrieval engine. Add isolated identity, billing, knowledge, conversation and evaluation services behind a new application factory. Every authenticated resource operation carries a server-validated workspace context.

**Tech Stack:** Python 3.11+, existing FastAPI/SQLAlchemy/Pydantic/httpx, SQLite or PostgreSQL, local Chroma/BM25, browser ES Modules and local CSS. Use stdlib unittest with asynchronous API integration tests to avoid additional test dependencies.

## Global Constraints

- Repository root: isolated managed worktree; branch `codex/enterprise-knowledge-saas`; base `09b7af893fd0570bafa701bc5be5b7244ffeda86`.
- Python for local tests: `.venv/Scripts/python.exe` on Windows; `.venv/bin/python` on Linux.
- Never use the original checkout's `.env`, databases, uploaded documents or private README changes.
- UUID identities; shared existing `Base` and `Workspace` represent organization resources.
- Every knowledge, conversation, message, search, export and billing operation verifies organization membership and role. Cross-tenant resource IDs return 404.
- Roles: owner/admin/editor/viewer; last owner cannot be removed/demoted. All new registration accounts have no access to legacy unclaimed workspaces.
- Secrets never appear in responses, audit or logs. Passwords PBKDF2-HMAC-SHA256 with random salt and 600000 iterations; sessions and API keys store only SHA256 digests.
- Session cookies HttpOnly, SameSite=Lax, production Secure; state-changing cookie requests require CSRF matching the authenticated session. Bearer API keys use current membership and can be revoked.
- Default plan limits members/KBs/documents/monthly answers: Free 1/3/50/100; Team 10/20/1000/5000; Business 50/100/10000/50000.
- Strict evidence mode avoids any LLM call when retrieval is empty. Upstream failures are not completed answers.
- Tests use temporary SQLite, FakeRetriever, FakeLLM and FakePayment, never real network or hardware. Run `python -m unittest discover -s tests -v` from repository root.
- Only additive database migrations. No existing vector rename, reset or bulk reimport.
- No new runtime libraries without audited source/commit/license provenance. CI Actions pinned to audited commit. No Skill clone/install.
- No real invitation email, payment account operation, production deployment or automatic PR merge.

## Shared Interfaces

`app.saas.config.AppSettings` extends existing configuration with SaaS cookie/origin/session/invite/Stripe settings. `app.saas.application.create_app(settings=None, retriever=None, llm=None, payment=None) -> FastAPI` stores injected dependencies and SQLAlchemy session factory on `app.state`.

`app.saas.dependencies.get_db(request)` yields the app's async session. `get_user(request, db)` validates session/Bearer. `get_access(request, db) -> AccessContext` validates `X-Workspace-ID`; context has `user_id`, `workspace_id`, `role`, `credential_id`. `require_role(access, *roles)` raises 403; `get_scoped_kb(db, access, kb_id)` returns a live knowledge base or raises 404. Context uses current membership, not client role claims.

Retriever adapter supports async `search(query, kb_ids, top_k, hybrid_alpha, score_threshold, enable_reranker) -> list[dict]`, `parse(file_path, filename, kb_id, doc_id, chunk_size, chunk_overlap) -> list[dict]`, `upsert(chunks) -> int`, `delete_document(doc_id) -> int`, `delete_kb(kb_id) -> int`, `close()`. Search result dictionaries contain chunk_id/doc_id/content/filename/score/metadata with metadata.kb_id.

LLM adapter supports async `complete(messages, temperature, max_tokens) -> dict` with content/usage, and `stream(messages, temperature, max_tokens) -> AsyncIterator[str]` yielding content deltas. Both can raise timeout or upstream exceptions. Payment adapter encapsulates Stripe HTTP requests; webhook signature validation stays a local pure function.

`tests/support.py` defines `ApiTestCase` with temporary storage, app lifespan and async httpx client; `register(email, organization_name='Example Team', password='ExampleStrong123!')` returns the auth JSON and updates the test's CSRF/workspace request headers. Provide independent clients for cross-tenant tests. Auth registration JSON fields are email/password/organization_name; response contains user, organizations, csrf_token. Tests must not globally share credentials or DB.

## Task 1: Identity, organizations and app foundation

**Files:** `backend/app/saas/{__init__,config,models,security,dependencies,auth,organizations,application}.py`, `tests/{__init__,support,test_identity}.py`.

**Consumes:** existing ORM Base/Workspace/GUID. **Produces:** shared app factory, DB/user/access dependencies, user/session/membership/invitation/API-key/audit models and routes.

- [ ] Write API tests for register/login/logout/me; two users' organizations; invite acceptance/expiry/reuse; role updates/removal/last owner; session expiry/revocation; API-key revoke and CSRF.
- [ ] Run missing-feature tests and record the failure.
- [ ] Implement factory with additive table creation, no heavyweight model startup and injectable adapters. Mount auth/organization routes; later routers are mounted through the factory. Server determines access context from DB membership.
- [ ] Use this behavior test as the minimum tenant boundary:

```python
async def test_new_account_cannot_read_other_organization(self):
    first = await self.register('first@example.test')
    foreign_id = first['organizations'][0]['id']
    second_client = await self.new_client()
    await self.register_with(second_client, 'second@example.test')
    response = await second_client.get(f'/api/v1/organizations/{foreign_id}')
    self.assertEqual(response.status_code, 404)
```

- [ ] Run identity tests, inspect diff, commit intended files, write scoped report.

## Task 2: Subscriptions, quotas and Stripe adapter

**Files:** `backend/app/saas/{billing_models,billing,quotas,payments}.py`, `tests/test_billing.py`.

**Consumes:** app factory, AccessContext/get_db, organizations. **Produces:** `get_entitlements(db, workspace_id)`, atomic `reserve_answer(...)`/`finish_answer(...)`, resource quota checks and billing routes.

- [ ] Write tests for Free limits, active Team/Business, canceled/past_due fallback, monthly rollover, atomic reservation, failure release, checkout/portal authorization, signed/replayed/old/wrong-tenant webhooks.
- [ ] Verify failures; implement unique monthly usage rows and bounded atomic counters. Ensure owner/admin only and server-owned Price IDs.
- [ ] Implement fixed-origin Stripe Checkout/portal via httpx; do not trust browser-returned payment state. Validate raw-body signature, replay window, event uniqueness and event ordering.
- [ ] Minimum webhook behavior:

```python
async def test_invalid_signature_never_changes_subscription(self):
    await self.register('billing@example.test')
    response = await self.client.post('/api/v1/billing/webhook', content=b'{"id":"evt_fake"}', headers={'Stripe-Signature':'t=1,v1=bad'})
    self.assertEqual(response.status_code, 400)
    status = await self.client.get('/api/v1/billing/subscription', headers=self.headers)
    self.assertEqual(status.json()['plan'], 'free')
```

- [ ] Run tests, inspect and commit; document exact quota service signatures for later tasks.

## Task 3: Scoped knowledge and persistent ingestion

**Files:** `backend/app/saas/{knowledge,ingestion,ingestion_models,retrieval}.py`, `tests/test_knowledge.py`.

**Consumes:** access dependencies, entitlement/quota functions, Retriever interface, existing knowledge ORM. **Produces:** scoped existing knowledge/document/search endpoints, durable jobs and default RAG adapter.

- [ ] Test KB/document/chunk/search isolation, viewer writes, mixed-ID batch rejection, duplicate/empty/oversized uploads, per-plan limits, interrupted and retried jobs and index failure cleanup.
- [ ] Verify failures; implement organization-filtered CRUD and background job worker with committed status transitions, retained source on retry and bounded attempts.
- [ ] Keep jobs in relational DB and lease/recover them across restarts. Do not claim clustered exactly-once semantics; use idempotent chunk IDs and conflict-safe processing.
- [ ] Lazy-load existing RAG engine in retrieval adapter; injected Fake supports tests without model downloads.
- [ ] Minimum search behavior:

```python
async def test_search_rejects_mixed_tenant_kb_ids_before_retrieval(self):
    first_kb, foreign_kb = await self.two_tenant_kbs()
    response = await self.client.post('/api/v1/agent/search', json={'query':'policy','kb_ids':[first_kb,foreign_kb]}, headers=self.headers)
    self.assertEqual(response.status_code,404)
    self.assertEqual(self.retriever.search_calls,[])
```

- [ ] Run tests, review and commit with retry/retention limitations in report.

## Task 4: Grounded conversations and knowledge operations

**Files:** `backend/app/saas/{conversation_models,conversations,chat,llm,insights}.py`, `tests/test_conversations.py`.

**Consumes:** scoped knowledge/access, answer quota reservation, adapters. **Produces:** compatible sync/SSE completions, conversation CRUD/messages/export, feedback and insights.

- [ ] Test strict no-evidence zero-model calls, session restoration, bounded current-session history, citation validation, cross-tenant message/feedback/export access, upstream timeout/disconnect/partial SSE/cancel states and correct usage finalization.
- [ ] Verify failures; implement shared chat service and persist final and failure states. Treat retrieved document instructions as untrusted data and validate all citations against retrieved IDs.
- [ ] Persist feedback idempotently and compute knowledge gaps only from insufficient evidence/negative feedback, excluding transport errors.
- [ ] Minimum groundedness behavior:

```python
async def test_empty_retrieval_does_not_call_model(self):
    kb = await self.create_kb()
    self.retriever.results=[]
    response=await self.client.post('/api/v1/chat/completions/sync',json={'query':'unknown warranty','kb_ids':[kb]},headers=self.headers)
    self.assertEqual(response.status_code,200)
    self.assertEqual(response.json()['status'],'insufficient_evidence')
    self.assertEqual(self.llm.calls,[])
```

- [ ] Run tests, inspect SSE protocol and commit with actual route/response contracts.

## Task 5: Retrieval comparisons and honest evaluation

**Files:** `backend/app/saas/evaluation.py`, `tests/test_evaluation.py`, `examples/customer-support/*`, `docs/evaluation.md`.

**Consumes:** access/scoped knowledge and Retriever. **Produces:** evaluation API, pure `retrieval_metrics(retrieved_doc_ids, relevant_doc_ids)` and synthetic sample package/manual rubric.

- [ ] Test duplicate IDs, zero truth/zero matches, known ranks, bounds, foreign label IDs and unlabeled null metrics. Verify failure before implementation.
- [ ] Implement unique document-level metrics; compare at most 3 configs/50 queries/top-k 20. Do not represent score similarity as confidence or automatic faithfulness.
- [ ] Known-rank acceptance:

```python
def test_rank_two_relevance(self):
    result=retrieval_metrics(['wrong','right'],['right'])
    self.assertEqual(result['mrr'],0.5)
    self.assertEqual(result['context_precision'],0.5)
    self.assertEqual(result['context_recall'],1.0)
```

- [ ] Add synthetic warranty/manual documents and Chinese review rubric/manual samples, run tests and commit.

## Task 6: Visual graph configuration and execution

**Files:** `backend/app/saas/{workflow_models,workflows,workflow_engine,model_profiles}.py`, `tests/test_workflows.py`, `web/{canvas,workflow}.js`.

**Consumes:** access, scoped knowledge, model/retriever adapters and quotas. **Produces:** versioned DAG, bounded execution, model profiles and drag editor. Requirements: `docs/superpowers/specs/2026-10-07-visual-orchestration-addendum.md`; user explicitly requested addition and continuation.

- [ ] Test cycle/dangling edge/port/tenant resource/budget validation before implementation.
- [ ] Implement input/retrieval/filter/deduplicate/rerank/evidence/prompt/model/merge/output nodes with real adapter parameters, parallel model branches, cancellation and timeout.
- [ ] Persist draft, immutable versions, rollback and actual traces; secrets are environment references and hosts deployer-allowlisted.
- [ ] Verify dual-KB/dual-model graph routes correct IDs and temperature/top-k; empty strict evidence calls no model.
- [ ] Build canvas drag/connect, sidebar parameters, pan/zoom, undo/redo, templates, import/export, save/publish and trial trace.
- [ ] Run scoped API/browser tests, inspect and commit.

## Task 7: Product UI, runtime and delivery

**Files:** `index.html`, `web/{app,api,views}.js`, `web/styles.css`, `backend/app/main.py`, `backend/run.py`, `.env.template`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, public README and docs.

**Consumes:** final route contracts. **Produces:** integrated runnable SaaS and reproducible deployment.

- [ ] Root controller wires all completed routers into app factory/main. Preserve legacy migration logic in an explicit migration command and never auto-claim legacy tenant data.
- [ ] Implement plain local browser modules for auth, organization switch, overview, KB/imports, chat/history/evidence/feedback/export, evaluation, members/invites/API keys and billing.
- [ ] All writes pass auth and CSRF, workspace changes clear active KB/conversation state, and render document/model text safely with textContent or safe rendering.
- [ ] Validate browser full flow on synthetic local server plus narrow viewport; verify failed payments, missing LLM, insufficient evidence and disconnected server leave actionable UI.
- [ ] Lock existing versions without adding unaudited libraries. Record action sources/license/commit and environment; CI runs offline unit/API tests and Python syntax checks.
- [ ] Rewrite README/README_zh around actual delivered behavior, deployment prerequisites and verification evidence. No invented traction/performance/production claims or new license.
- [ ] Commit explicit files after meaningful checks pass.

## Task 8: Review and GitHub publication

- [ ] Run full suite and syntax checks from clean worktree; measure actual coverage if available and report scope/untested risks honestly.
- [ ] Review tenant access, payments, quotas, jobs and SSE independently against design. Resolve material findings and repeat affected tests.
- [ ] Inspect complete branch diff for personal paths, API keys, runtime data and generated files. Verify original checkout's existing five files remain untouched.
- [ ] Push branch using local git. If git transport remains unreliable, use connected GitHub Git Data APIs with verified base/tree/commit and no force update.
- [ ] Open draft PR through GitHub plugin with concrete behavior, validation and limitations; attach PR to current chat.
- [ ] Report PR URL, delivered changes, test evidence and operational prerequisites.
