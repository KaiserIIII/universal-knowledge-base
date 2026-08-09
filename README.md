# Universal Knowledge Base

> A self-hosted, local-first RAG system with hybrid retrieval, source citations, multi-knowledge-base search, and a configurable Web UI.

[中文说明](README_zh.md)

## Features

- Create, rename, and delete multiple knowledge bases.
- Import PDF, Word, Markdown, text, and HTML documents in batches.
- Combine vector search with local BM25 keyword retrieval.
- Search across multiple knowledge bases while keeping imports scoped to the selected base.
- Stream answers with traceable source snippets.
- Configure models, retrieval, chunking, uploads, and runtime settings in the Web UI.
- Use CPU by default or an optional NVIDIA GPU for embeddings.
- Connect to DeepSeek, vLLM, or another OpenAI-compatible chat endpoint.

## Architecture

| Component | Default implementation |
|---|---|
| API | FastAPI |
| Relational data | SQLite + SQLAlchemy 2.0; PostgreSQL supported |
| Vector index | Persistent local ChromaDB |
| Keyword retrieval | jieba tokenization + local BM25 |
| Embeddings | `BAAI/bge-small-zh-v1.5` |
| Parsing | unstructured with pypdf, python-docx, and BeautifulSoup fallbacks |
| Front end | Tailwind CDN + native ES6 single-page UI |

## Quick start

On Windows, run `start.bat` or:

```powershell
python start.py
```

The launcher creates `backend/venv`, installs dependencies, checks `backend/.env`, starts the service, and opens `http://localhost:8000`. Interactive API documentation is available at `http://localhost:8000/docs`.

The first launch requires internet access to download Python packages and the embedding model. Later launches reuse the local environment and model cache.

## Configure an LLM

Settings can be entered in the Web UI or preconfigured in `backend/.env`:

```dotenv
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=replace-with-your-key
LLM_MODEL=deepseek-chat
```

Without an API key, knowledge-base management, ingestion, parsing, and retrieval continue to work; chat requests return `503`. Never commit API keys or a populated `.env` file.

## Core API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/kb` | Create a knowledge base |
| `GET` | `/api/v1/kb?limit=200` | List knowledge bases |
| `DELETE` | `/api/v1/kb/{kb_id}?hard=true` | Delete a knowledge base and its files |
| `GET` | `/api/v1/kb/{kb_id}/documents?limit=200` | List documents |
| `POST` | `/api/v1/kb/{kb_id}/documents` | Queue a document batch |
| `DELETE` | `/api/v1/kb/{kb_id}/documents/{doc_id}` | Delete a document |
| `GET/PATCH` | `/api/v1/settings` | Read or update UI settings |
| `POST` | `/api/v1/agent/search` | Run hybrid retrieval |
| `POST` | `/api/v1/chat/completions` | Stream a cited answer over SSE |
| `GET` | `/health` | Check database and retrieval health |

## PostgreSQL

Start the included service:

```bash
docker compose up -d
```

Then set:

```dotenv
DATABASE_URL=postgresql+asyncpg://enterprise_kb:change-me-before-production@127.0.0.1:5432/enterprise_kb
```

ChromaDB, BM25, and embeddings remain local to the application process.

## Optional GPU acceleration

CPU is the default. On Windows with an NVIDIA GPU, install the PyTorch wheel that matches your driver and select CUDA in the Web UI. The service reports the active embedding device at `/api/v1/agent/status` and falls back to CPU when CUDA is unavailable.

## Data and security notes

- Uploaded files are used as temporary ingestion inputs and cleaned up after the pipeline finishes.
- API keys are not echoed by the settings page.
- Binding the service beyond localhost should only be done on a trusted network.
- Back up the relational database and local indexes before destructive knowledge-base operations.

## License

No license file is currently included. Add one before distributing or accepting external contributions.
