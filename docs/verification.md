# Verification evidence

The final backend run on 2026-10-08 passed **120 tests** in 337.913 seconds using Python 3.13 on Windows, temporary SQLite databases and synthetic adapters. The frontend module suite passed **33 tests** with no failures. These durations describe verification runs, not service performance. Python compilation, JavaScript syntax, dependency consistency, Compose configuration and Git whitespace checks also passed.

Zhixu is free, open-source and self-hosted. Fresh databases have no commercial account tables or endpoints, and resource totals are unlimited. Historical accounting data is retained and ignored. The application disables Chroma telemetry by default.

## Reproduce core checks

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m compileall -q backend/app tests start.py backend/run.py
.venv/Scripts/python.exe -m pip check
node --test tests/web/*.test.mjs
Get-ChildItem web -Filter *.js | ForEach-Object { node --check $_.FullName }
docker compose config --quiet
```

On Linux use `.venv/bin/python`. Compose needs a configured `PUBLIC_ORIGIN`; configuration validation does not build or start containers.

## Covered behavior

| Area | Exercised behavior |
| --- | --- |
| Identity and organizations | Registration, sessions, CSRF, role changes, last-owner protection, invitations, API-key revocation and foreign-organization rejection |
| Knowledge and imports | Scoped SQL rehydration, upload bounds, duplicate hashes, retryable leased jobs, bounded Office XML parsing, source retention and changed-source rejection before parsing |
| Parser modules | Chinese CSV/TSV extraction, row truncation, explicit encoding failures, encoded HTML with scripts removed, unsupported format rejection, settings forwarded to workers and SHA-256 chunk receipts |
| Conversations | Fragmented SSE, terminal state reconciliation, cancellation, source-bound citations, insufficient evidence, feedback, export and knowledge-gap insights |
| Retrieval evaluation | Selected document labels, configuration bounds, unique-document precision/recall/MRR/Hit Rate/nDCG, excerpts and authorization rechecks |
| Models and workflows | Scoped connections, approved hosts and environment references, typed graph validation, versions/rollback, parallel branches, citation remapping, degraded synthesis, evidence limits and cancellation |
| File evidence | Completed document scope checked during validation and execution, SQL-backed excerpts without a vector index, and deleted/foreign document rejection |
| Runtime and migration | Public file boundaries, headers, liveness/readiness, legacy workspace claims, repeated SQLite updates preserving rows, generated columns, collation, indexes, trigger literals and default values |

## Actual browser acceptance

The committed [browser smoke check](../tests/browser/smoke.cjs) ran in isolated Chrome 155.0.8059.40 with the already installed Playwright 1.62.1. It used the [local synthetic fixture](../tests/browser/preview.py), which creates a temporary database and never calls a model provider. It verified:

- English branding and navigation without commercial pages; parser mode, encoding and row limits saved through the UI.
- File-node dragging, visible graph connections, validation, saving, publishing, published execution, evidence and node traces.
- Saved file selection and historical runs restored after reload; deselected files remain deselected when reopening a node.
- English deployment settings, Chinese language switching and reload persistence.
- A 390px knowledge page with document tables scrolling inside their container and no horizontal page overflow.

No JavaScript page errors were observed. The [workflow screenshot](images/workflow.jpg), [parser settings](images/parser-settings.jpg) and [browser record](images/browser-acceptance.json) contain disposable synthetic data.

Run the fixture in one terminal and the check in another, using an already installed Playwright module and Chrome:

```powershell
.venv/Scripts/python.exe tests/browser/preview.py
node tests/browser/smoke.cjs
```

The default fixture origin is `http://127.0.0.1:8788`. `ZHIXU_PLAYWRIGHT_MODULE` can point to an existing Playwright installation; `ZHIXU_BROWSER_CHANNEL` selects an installed compatible browser. Artifacts default to `output/playwright`. These checks do not install dependencies or use deployment data. Stop the fixture with Ctrl+C after testing.

## Corpus and evidence limits

An earlier offline integration check exercised native Chroma 1.5.9, cached Chinese BGE embeddings, BM25, SQL evidence rehydration and the evaluation API against a copied existing LangBot index. A synthetic TXT import completed the actual parser, embedding, index write and tenant-scoped search path. Cross-tenant access was rejected, and file fingerprints confirmed that the original index was unchanged. Private corpus files, queries and reports remain outside version control. This integration was not repeated as part of the final open-source conversion.

The Chinese [manual evaluation rubric](evaluation.md) and [worked examples](../examples/customer-support/manual-judgments.md) support human review. They are illustrations, not a completed human study or an answer-quality benchmark. Tests cover tenant leakage boundaries, adversarial evidence scope and untrusted-document instructions; they do not prove resistance to arbitrary prompt injection or answer faithfulness. No coverage percentage was measured.

Production acceptance remains for PostgreSQL locking, Docker image builds, representative PDF ingestion and live model providers. OCR, vision and legacy Office conversion are unavailable, as documented in [parser modules](parsing.md). RAG direct dependencies are pinned; platform-specific ML transitive locking remains deployment work. See [deployment](deployment.md) for operational limits and migration steps.
