# Verification evidence

Core API verification on 2026-10-08 used a clean Python 3.13 virtual environment on Windows, the audited 24-package core lock, temporary SQLite databases and synthetic retriever, model and payment adapters. The final serial suite passed **137 tests** in 413.339 seconds. The browser module suite passed **26 tests**. These durations describe test runs, not service performance. Python compilation, dependency consistency, browser module syntax and Compose configuration checks also passed.

| Area | Exercised behavior |
| --- | --- |
| Identity and organizations | Registration, sessions, CSRF, role changes, last owner, invitation expiry/reuse, API key revocation and foreign organization access |
| Billing and quotas | Signed raw webhook bodies, idempotency, delayed checkout replacement, entitlement periods, reservation completion/release and resource limits |
| Knowledge and imports | Scoped SQL rehydration, deleted/foreign evidence rejection, upload bounds, duplicate hashes, retryable leased jobs and bounded Office XML parsing |
| Conversations | Fragmented SSE, authoritative terminal state, stale refresh rejection, partial upstream failure, cancellation, source-bound citations, citation-only output rejection, strict empty-evidence behavior, feedback, export and answer accounting |
| Retrieval evaluation | Selected document labels, configuration bounds, unique-document precision/recall/MRR/hit rate/nDCG, null versus explicit-empty labels, bounded excerpts and authorization rechecks after unlocked retrieval |
| Models and workflows | Scoped connections, allowed hosts and environment references, actual adapter parameter forwarding, typed graph validation, versions/rollback, independent branches, citation remapping, degraded synthesis, output/evidence budgets, cancellation and expired-run recovery |
| Runtime and migration | Public file boundaries, cache/security headers, separate liveness and bounded database readiness probes, local API reference and explicit legacy ownership migration preserving existing knowledge IDs |

Reproduce the API checks from the repository root:

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m compileall -q backend/app tests start.py backend/run.py
.venv/Scripts/python.exe -m pip check
node --test tests/web/*.test.mjs
docker compose config --quiet
```

On Linux use `.venv/bin/python`. Compose requires a configured `PUBLIC_ORIGIN`; enable optional PostgreSQL only with its required credentials. Configuration validation does not build or start a container.

Browser verification used a disposable local SQLite workspace and synthetic customer support documents. It exercised import/search, model settings, multi-model workflow execution, degraded branches, version rollback, cancellation, insufficient evidence, unfinished draft JSON import/export, conversation feedback/export and retrieval comparisons. A 121-document fixture verified access beyond the first 100 documents and relevance labels retained across pages. A 390px layout check confirmed navigation and no horizontal document overflow. The [workflow screenshot](images/workflow.png) records that synthetic UI, without live provider credentials.

The Chinese [manual evaluation rubric](evaluation.md) and [worked examples](../examples/customer-support/manual-judgments.md) support evidence review. They are illustrations, not a completed human study or model quality benchmark. Automated tests check scope isolation and citation/evidence plumbing; they do not prove a model resists arbitrary prompt injection or that an answer is faithful. No coverage percentage was measured.

An additional offline local integration check exercised native Chroma 1.5.9, cached BGE Chinese embeddings, BM25, SQL evidence rehydration and the evaluation API against a locally copied existing LangBot index. A synthetic TXT upload also completed the actual parser, embedding, index write and tenant-scoped search path. Cross-tenant access was rejected, and full file fingerprints confirmed the original index was unchanged. Private corpus files, queries and per-case reports remain outside version control; this check establishes integration behavior, not generative answer quality. Loading the copied existing HNSW index on Windows required an ASCII path, as documented in [deployment](deployment.md).

Production acceptance remains to be performed for PostgreSQL locking, Docker builds, PDF ingestion, live OpenAI-compatible providers and Stripe subscriptions. RAG direct dependencies are pinned and audited, but ML transitive dependencies need a platform-specific lock. Ordinary chat reservations interrupted by a process crash require operator reconciliation; expired workflow runs release reservations without replaying remote calls. Import job browsing exposes the API's newest 200 jobs. See [deployment](deployment.md) and [workflow behavior](workflows.md).
