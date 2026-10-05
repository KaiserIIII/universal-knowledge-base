# Universal Knowledge Base

[简体中文](README_zh.md)

A self-hosted RAG application for organizing documents and answering questions with source citations. It combines vector retrieval and BM25 keyword search, with document ingestion, multi-knowledge-base search, and model settings in a Web UI.

## Features

- Create and manage multiple knowledge bases.
- Batch-import PDF, Word, Markdown, text, and HTML documents.
- Combine semantic search with keyword matching.
- Search across selected knowledge bases and stream cited answers.
- Configure retrieval, chunking, uploads, models, and embedding devices.
- Use a local vector index with a configurable OpenAI-compatible chat endpoint.

## Architecture

| Component | Implementation |
| --- | --- |
| API | FastAPI |
| Relational storage | SQLite + SQLAlchemy; optional PostgreSQL |
| Vector storage | Persistent ChromaDB |
| Keyword retrieval | jieba + BM25 |
| Default embeddings | `BAAI/bge-small-zh-v1.5` |
| Parsing | unstructured with format-specific fallbacks |
| Interface | JavaScript single-page UI |

## Quick start

On Windows:

```powershell
python start.py
```

Alternatively, run `start.bat`. The launcher prepares `backend/venv`, installs dependencies, checks configuration, and opens <http://localhost:8000>. API documentation is at <http://localhost:8000/docs>.

The first launch downloads dependencies and the embedding model; subsequent launches reuse the environment and model cache.

## Model configuration

Use the Web UI or `backend/.env`:

```dotenv
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=replace-with-your-key
LLM_MODEL=deepseek-chat
```

Without a configured API key, document management and retrieval remain available; chat requests return `503`. CPU embeddings are the default. CUDA is optional and falls back to CPU when unavailable.

## PostgreSQL

The included Compose file starts PostgreSQL:

```bash
docker compose up -d
```

Configure `DATABASE_URL` in `backend/.env` to match the database credentials. ChromaDB, BM25, and embeddings remain local to the application.

## Code map

- [backend/app/routers/](backend/app/routers/): knowledge bases, documents, settings, search, and chat.
- [backend/app/rag_engine.py](backend/app/rag_engine.py): ingestion and retrieval.
- [backend/app/database.py](backend/app/database.py): database setup.
- [index.html](index.html): Web UI.
- [backend/.env.template](backend/.env.template): configuration reference.

## Deployment

An external chat endpoint receives the context selected for answer generation. Configure it to match your data requirements. Keep credentials outside Git, back up the database and indexes, and restrict remote access to trusted networks.

No project-wide license is currently specified.
