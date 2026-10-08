# Zhixu · Enterprise Knowledge — Original Release

[简体中文](README_zh.md) · [Current development](https://github.com/KaiserIIII/universal-knowledge-base/tree/main)

This branch preserves the application code from the original **July 30, 2026**
release. Only bilingual documentation and the Apache-2.0 license have been added.
The repository slug `universal-knowledge-base` remains for link compatibility.

Zhixu is a free, open-source, self-hosted knowledge base with local RAG retrieval.
The original release is suited to personal collections and local team documents.
For authenticated organizations, tenant isolation and visual workflows, use `main`.

## Features

- Create, rename and delete multiple knowledge bases.
- Import PDF, Word, Markdown, TXT and HTML documents in batches.
- Combine local vector search with Chinese BM25 keyword search across libraries.
- Stream answers with source documents and retrieved passages.
- Configure model, retrieval, chunking and upload settings from the Web UI.
- Connect DeepSeek, vLLM or other OpenAI-compatible model endpoints.
- Run embeddings on CPU or an optionally configured NVIDIA GPU.

## Run locally

Requires Python and a local environment suitable for the dependencies listed in
`backend/requirements.txt`. On Windows, run `start.bat`, or:

```powershell
python start.py
```

The original launcher creates `backend/venv`, installs dependencies and opens
[http://localhost:8000](http://localhost:8000). The API reference is served at
`/docs`. Initial setup downloads dependencies and embedding weights.

Configure an OpenAI-compatible LLM in the Web UI or `backend/.env`:

```dotenv
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=your-local-key
LLM_MODEL=deepseek-chat
```

Without a configured model key, knowledge management, parsing and retrieval remain
available; answer generation reports the missing configuration. Keep credentials,
private documents, databases and virtual environments out of Git.

## Architecture

```text
Browser UI
  -> FastAPI routes
     -> SQLAlchemy + SQLite: libraries and document records
     -> Local parsing + LangChain text splitting
     -> Local embedding + ChromaDB vector index
     -> jieba + BM25 keyword index
     -> OpenAI-compatible LLM -> streamed answer and citations
```

```text
index.html                  Original single-page frontend
start.bat / start.py         Local launcher
backend/app/main.py          FastAPI entrypoint
backend/app/models.py        SQLAlchemy models
backend/app/rag_engine.py    Parsing, chunks, embeddings, Chroma and BM25
backend/app/routers/         Knowledge, documents, retrieval, chat and settings
backend/app/data/            Local SQL and vector storage
backend/uploads/             Temporary import files
```

SQLite is the default relational store; PostgreSQL can be configured with
`DATABASE_URL`. Vector retrieval and embeddings remain in the application process.
Choose a compatible PyTorch build for optional GPU acceleration. Inspect
`/api/v1/agent/status` to confirm the effective embedding device.

This historical version retains its original dependencies and frontend CDN usage.
It does not include the current branch's security, deployment or telemetry changes.
Use `main` for the current self-hosted edition and its verification guidance.

## Contributing and license

Report reproducible issues or submit pull requests against `main`. Describe the
expected behavior, include relevant tests, and update both language versions when
changing user-facing behavior. Historical code stays preserved on this branch.

Released under the [Apache License 2.0](LICENSE). The project has no built-in
pricing, paid tiers or subscription service.
