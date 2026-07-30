"""
企业级知识库 — RAG 核心引擎 (零依赖嵌入式版本)

核心组件:
  ChromaDB        嵌入式向量索引
  本地 BM25       jieba 分词与线程安全倒排索引
  BGE-small       sentence-transformers 本地加载
  BGE-Reranker    FlagEmbedding 本地加载（可选）

零外部服务依赖，全部跑在本地进程内。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import math

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import get_settings
from .schemas import ChunkMatch

logger = logging.getLogger("rag_engine")

# ══════════════════════════════════════════════════════════════════
# 1. LocalEmbedding — 本地 BGE 模型
# ══════════════════════════════════════════════════════════════════
class LocalEmbedding:
    """本地加载 BGE 中文 Embedding 模型，慢启动但零 API 依赖"""

    def __init__(self):
        cfg = get_settings()
        self.model_name = cfg.embedding_model_name
        self.device = cfg.embedding_device
        self.batch_size = cfg.embedding_batch_size
        self._model = None
        self._dim = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self):
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            requested_device = self.device
            if requested_device == "cuda":
                try:
                    import torch
                    if not torch.cuda.is_available():
                        self.device = "cpu"
                        logger.warning("CUDA is not available; falling back to CPU for embeddings")
                except ImportError:
                    self.device = "cpu"
            elif requested_device == "mps":
                try:
                    import torch
                    if not getattr(torch.backends, "mps", None) or not torch.backends.mps.is_available():
                        self.device = "cpu"
                        logger.warning("MPS is not available; falling back to CPU for embeddings")
                except ImportError:
                    self.device = "cpu"
            logger.info(f"Loading embedding model: {self.model_name} on {self.device}...")
            from sentence_transformers import SentenceTransformer
            try:
                model = await asyncio.to_thread(SentenceTransformer, self.model_name, device=self.device)
            except (AssertionError, RuntimeError) as error:
                if self.device == "cpu":
                    raise
                logger.warning("Embedding device %s failed (%s); retrying on CPU", self.device, error)
                self.device = "cpu"
                model = await asyncio.to_thread(SentenceTransformer, self.model_name, device="cpu")
            self._model = model
            dimension_getter = getattr(model, "get_sentence_embedding_dimension", None)
            if callable(dimension_getter):
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        category=FutureWarning,
                        message=r".*get_sentence_embedding_dimension.*",
                    )
                    dimension = dimension_getter()
            else:
                dimension = None
            if not dimension:
                probe = await asyncio.to_thread(
                    model.encode,
                    ["维度探测"],
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                dimension = probe.shape[-1]
            self._dim = int(dimension)
            logger.info(f"Embedding model loaded. Dimension: {self._dim}")

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """批量向量化"""
        await self._ensure_loaded()
        if not texts:
            return []

        results = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            embeddings = await asyncio.to_thread(self._model.encode, batch, normalize_embeddings=True, show_progress_bar=False)
            results.extend(embeddings.tolist())
        return results

    async def embed_single(self, text: str) -> List[float]:
        results = await self.embed([text])
        return results[0] if results else []

    @property
    def dim(self) -> int:
        return self._dim or 512  # bge-small-zh-v1.5 default


# ══════════════════════════════════════════════════════════════════
# 2. LocalReranker — 本地重排模型(可选)
# ══════════════════════════════════════════════════════════════════
class LocalReranker:
    """本地 BGE-Reranker，重启排序精度"""

    def __init__(self):
        cfg = get_settings()
        self.model_name = cfg.reranker_model_name
        self.enabled = cfg.use_reranker
        self._model = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self):
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            logger.info(f"Loading reranker: {self.model_name}...")
            model = await asyncio.to_thread(self._load_sync)
            self._model = model
            logger.info("Reranker loaded.")

    def _load_sync(self):
        from FlagEmbedding import FlagReranker
        return FlagReranker(self.model_name, use_fp16=True)

    async def rerank(self, query: str, docs: list, top_k: int) -> list:
        await self._ensure_loaded()
        if not docs:
            return docs

        pairs = [[query, d["content"]] for d in docs]
        scores = await asyncio.to_thread(self._model.compute_score, pairs, normalize=True)

        if isinstance(scores, float):
            scores = [scores]

        for i, d in enumerate(docs):
            d["score"] = float(scores[i]) if i < len(scores) else 0.0

        docs.sort(key=lambda x: x["score"], reverse=True)
        return docs[:top_k]


# ══════════════════════════════════════════════════════════════════
# 3. LocalBM25 — 自建 BM25 (中文分词 + 倒排索引)
# ══════════════════════════════════════════════════════════════════
class LocalBM25:
    """纯 Python BM25 实现，基于 jieba 中文分词"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[Dict[str, Any]] = []       # [{id, content, filename, kb_id, ...}]
        self._tokens: List[List[str]] = []           # doc_tokens
        self._doc_len: List[int] = []                # doc token counts
        self._avgdl: float = 0.0
        self._df: Dict[str, int] = defaultdict(int)  # document frequency
        self._idf: Dict[str, float] = {}
        self._total_docs: int = 0
        self._jieba_loaded: bool = False
        self._lock = threading.RLock()

    def _ensure_jieba(self):
        if self._jieba_loaded:
            return
        import jieba
        jieba.setLogLevel(20)
        self._jieba_loaded = True

    @property
    def document_count(self) -> int:
        with self._lock:
            return self._total_docs

    def _tokenize(self, text: str) -> List[str]:
        self._ensure_jieba()
        import jieba
        # jieba 切词 + 过滤单字
        tokens = jieba.lcut(text.lower())
        return [t.strip() for t in tokens if len(t.strip()) >= 2]

    def index(self, docs: List[Dict[str, Any]], rebuild: bool = True):
        """批量索引文档；增量写入按 chunk_id 覆盖，保证任务重试幂等。"""
        with self._lock:
            if rebuild:
                merged_docs = [dict(doc) for doc in docs]
            else:
                incoming_ids = {doc.get("chunk_id") for doc in docs}
                merged_docs = [
                    doc for doc in self._docs
                    if doc.get("chunk_id") not in incoming_ids
                ]
                merged_docs.extend(dict(doc) for doc in docs)
            self._rebuild(merged_docs)

    def _rebuild(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = docs
        self._tokens = []
        self._doc_len = []
        self._df = defaultdict(int)
        self._total_docs = len(docs)

        for doc in docs:
            tokens = self._tokenize(doc.get("content", ""))
            self._tokens.append(tokens)
            self._doc_len.append(len(tokens))
            for token in set(tokens):
                self._df[token] += 1

        self._avgdl = (
            sum(self._doc_len) / len(self._doc_len)
            if self._doc_len else 0.0
        )
        self._idf = {
            token: math.log(
                (self._total_docs - frequency + 0.5) / (frequency + 0.5) + 1.0
            )
            for token, frequency in self._df.items()
        }

    def remove_by_ids(self, doc_ids: List[str]):
        """按 doc_id 删除文档"""
        with self._lock:
            remove_set = set(doc_ids)
            self._rebuild([
                doc for doc in self._docs
                if doc.get("doc_id") not in remove_set
            ])

    def remove_by_kb(self, kb_id: str):
        """删除指定知识库的全部 BM25 文档。"""
        with self._lock:
            self._rebuild([
                doc for doc in self._docs
                if str(doc.get("kb_id")) != str(kb_id)
            ])

    def search(self, query: str, top_k: int = 10, kb_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """BM25 检索"""
        with self._lock:
            query_tokens = self._tokenize(query)
            if not query_tokens or not self._docs:
                return []

            scores = []
            for idx, doc_tokens in enumerate(self._tokens):
                if kb_filter and str(self._docs[idx].get("kb_id")) not in kb_filter:
                    continue
                if not doc_tokens:
                    continue
                score = 0.0
                doc_len = self._doc_len[idx]
                tf_map = {}
                for token in doc_tokens:
                    tf_map[token] = tf_map.get(token, 0) + 1

                for query_token in query_tokens:
                    if query_token not in self._idf:
                        continue
                    tf = tf_map.get(query_token, 0)
                    numerator = tf * (self.k1 + 1)
                    denominator = tf + self.k1 * (
                        1 - self.b + self.b * doc_len / max(self._avgdl, 1)
                    )
                    score += self._idf[query_token] * numerator / max(denominator, 0.001)

                if score > 0:
                    scores.append((idx, score))

            scores.sort(key=lambda item: item[1], reverse=True)
            results = []
            for idx, score in scores[:top_k]:
                doc = dict(self._docs[idx])
                doc["score"] = score
                results.append(doc)
            return results


# ══════════════════════════════════════════════════════════════════
# 4. DocumentProcessor — 文档解析 + 语义切分
# ══════════════════════════════════════════════════════════════════
class DocumentProcessor:
    SEMANTIC_SEPARATORS = [
        "\n\n\n", "\n\n", "\n", "。", "！", "？", "；", "：", "…",
        ". ", "! ", "? ", "; ", ": ", "、", "，", ", ", " ", "",
    ]

    def __init__(self, chunk_size: Optional[int] = None, chunk_overlap: Optional[int] = None):
        cfg = get_settings()
        self.chunk_size = chunk_size if chunk_size is not None else cfg.chunk_default_size
        self.chunk_overlap = (
            chunk_overlap if chunk_overlap is not None else cfg.chunk_default_overlap
        )
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.use_unstructured = cfg.use_unstructured

        self.text_splitter = RecursiveCharacterTextSplitter(
            separators=self.SEMANTIC_SEPARATORS,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
            keep_separator=True,
        )

    def parse_and_chunk(self, file_path: str, filename: str, kb_id: str, doc_id: str) -> List[Dict[str, Any]]:
        full_text = self._parse_file(file_path, filename)
        if not full_text or not full_text.strip():
            return []

        chunks = self.text_splitter.split_text(full_text)
        result = []
        for i, chunk_text in enumerate(chunks):
            chunk_text = chunk_text.strip()
            if not chunk_text:
                continue
            chunk_id = str(uuid.uuid5(namespace=uuid.NAMESPACE_DNS, name=f"{doc_id}:{i}"))
            result.append({
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "kb_id": str(kb_id),
                "filename": filename,
                "chunk_index": i,
                "content": chunk_text,
                "token_count": len(chunk_text) // 2,  # 中文约2字符/token
                "metadata": {
                    "chunk_index": i,
                    "total_chunks": 0,
                    "source_file": filename,
                },
            })

        total = len(result)
        for c in result:
            c["metadata"]["total_chunks"] = total

        return result

    def _parse_file(self, file_path: str, filename: str) -> str:
        ext = Path(filename).suffix.lower()

        if self.use_unstructured:
            try:
                return self._parse_unstructured(file_path)
            except Exception as error:
                logger.warning(
                    "unstructured failed for %s, using local parser: %s",
                    filename,
                    error,
                )

        if ext == ".pdf":
            return self._parse_pdf(file_path)
        elif ext in (".docx", ".doc"):
            return self._parse_docx(file_path)
        elif ext in (".html", ".htm"):
            return self._parse_html(file_path)
        else:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")

    def _parse_unstructured(self, file_path: str) -> str:
        from unstructured.partition.auto import partition

        elements = partition(filename=file_path, strategy="fast")
        return "\n\n".join(
            text for element in elements
            if (text := str(element).strip())
        )

    def _parse_pdf(self, file_path: str) -> str:
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text and text.strip():
                    parts.append(text)
            return "\n\n".join(parts)
        except ImportError:
            raise ImportError("PDF parsing needs pypdf. Install: pip install pypdf")

    def _parse_docx(self, file_path: str) -> str:
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(file_path)
            parts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    if para.style and para.style.name and "Heading" in para.style.name:
                        level = para.style.name.replace("Heading", "").strip()
                        hashes = "#" * min(int(level) if level.isdigit() else 1, 4)
                        parts.append(f"{hashes} {para.text}")
                    else:
                        parts.append(para.text)
            for table in doc.tables:
                rows = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(" | ".join(cells))
                if rows:
                    parts.append("\n[表格]\n" + "\n".join(rows))
            return "\n\n".join(parts)
        except ImportError:
            raise ImportError("DOCX parsing needs python-docx. Install: pip install python-docx")

    def _parse_html(self, file_path: str) -> str:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(Path(file_path).read_text(encoding="utf-8", errors="replace"), "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n")
        except ImportError:
            # fallback: strip tags crudely
            import re
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            return text


# ══════════════════════════════════════════════════════════════════
# 5. RAGEngine — 核心引擎 (ChromaDB + 本地 BM25)
# ══════════════════════════════════════════════════════════════════
class RAGEngine:
    def __init__(self):
        cfg = get_settings()
        self.cfg = cfg
        self.embedder: Optional[LocalEmbedding] = None
        self.reranker: Optional[LocalReranker] = None
        self.processor: Optional[DocumentProcessor] = None
        self.bm25: Optional[LocalBM25] = None
        self.chroma_client = None
        self.chroma_collection = None
        self.collection_name = cfg.chroma_collection_name
        self.persist_dir = str(Path(cfg.chroma_persist_dir).resolve())
        self._init_lock = asyncio.Lock()
        self._chroma_write_lock = asyncio.Lock()
        self._ready = False

    def _init_chroma_sync(self):
        import chromadb

        client = chromadb.PersistentClient(path=self.persist_dir)
        try:
            collection = client.get_collection(self.collection_name)
        except Exception:
            collection = client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return client, collection

    async def ensure_ready(self):
        if not self._ready:
            await self.init_stores()

    async def init_stores(self):
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            await asyncio.to_thread(os.makedirs, self.persist_dir, exist_ok=True)
            self.chroma_client, self.chroma_collection = await asyncio.to_thread(
                self._init_chroma_sync
            )
            logger.info("ChromaDB initialized: %s / %s", self.persist_dir, self.collection_name)

            self.embedder = LocalEmbedding()
            await self.embedder._ensure_loaded()

            self.bm25 = LocalBM25(k1=self.cfg.bm25_k1, b=self.cfg.bm25_b)
            await self._restore_bm25_index()

            self.reranker = LocalReranker()
            if self.cfg.use_reranker:
                await self.reranker._ensure_loaded()

            self.processor = DocumentProcessor()
            self._ready = True
            logger.info("RAG Engine ready (embedded mode).")

    async def _restore_bm25_index(self) -> None:
        stored = await asyncio.to_thread(
            self.chroma_collection.get,
            include=["documents", "metadatas"],
        )
        ids = stored.get("ids") or []
        documents = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        restored = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] or {} if index < len(metadatas) else {}
            restored.append({
                "chunk_id": chunk_id,
                "content": documents[index] if index < len(documents) else "",
                "filename": metadata.get("filename", ""),
                "doc_id": metadata.get("doc_id", ""),
                "kb_id": metadata.get("kb_id", ""),
                "chunk_index": metadata.get("chunk_index", 0),
            })
        await asyncio.to_thread(self.bm25.index, restored, True)
        logger.info("BM25 restored from ChromaDB: %s chunks", len(restored))

    async def reconcile_index(self, valid_doc_ids: set[str]) -> int:
        """Remove orphaned/non-completed vectors and rebuild BM25 from valid data."""
        await self.ensure_ready()
        stored = await asyncio.to_thread(
            self.chroma_collection.get,
            include=["metadatas"],
        )
        ids = stored.get("ids") or []
        metadatas = stored.get("metadatas") or []
        stale_chunk_ids = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] or {} if index < len(metadatas) else {}
            if str(metadata.get("doc_id", "")) not in valid_doc_ids:
                stale_chunk_ids.append(chunk_id)
        async with self._chroma_write_lock:
            if stale_chunk_ids:
                await asyncio.to_thread(
                    self.chroma_collection.delete,
                    ids=stale_chunk_ids,
                )
            await self._restore_bm25_index()
        return len(stale_chunk_ids)

    async def close(self):
        self._ready = False
        self.chroma_collection = None
        self.chroma_client = None

    async def ingest_document(self, chunks: List[Dict[str, Any]]) -> int:
        await self.ensure_ready()
        if not chunks:
            return 0

        texts = [c["content"] for c in chunks]
        ids = [c["chunk_id"] for c in chunks]
        metadatas = [{
            "doc_id": str(c["doc_id"]),
            "kb_id": str(c["kb_id"]),
            "filename": str(c["filename"]),
            "chunk_index": int(c["chunk_index"]),
        } for c in chunks]

        start = time.monotonic()

        embeddings = await self.embedder.embed(texts)
        if len(embeddings) != len(chunks):
            raise RuntimeError("Embedding count does not match chunk count")

        batch_size = 200
        async with self._chroma_write_lock:
            try:
                for i in range(0, len(ids), batch_size):
                    await asyncio.to_thread(
                        self.chroma_collection.upsert,
                        ids=ids[i:i + batch_size],
                        embeddings=embeddings[i:i + batch_size],
                        documents=texts[i:i + batch_size],
                        metadatas=metadatas[i:i + batch_size],
                    )
                await asyncio.to_thread(self.bm25.index, chunks, False)
            except Exception:
                await asyncio.to_thread(self.chroma_collection.delete, ids=ids)
                raise

        elapsed = time.monotonic() - start
        logger.info(f"Ingested {len(chunks)} chunks in {elapsed:.2f}s")
        return len(chunks)

    async def delete_document_chunks(self, doc_id: str) -> int:
        await self.ensure_ready()
        async with self._chroma_write_lock:
            results = await asyncio.to_thread(
                self.chroma_collection.get,
                where={"doc_id": str(doc_id)},
            )
            ids_to_delete = results.get("ids") or []
            if ids_to_delete:
                await asyncio.to_thread(self.chroma_collection.delete, ids=ids_to_delete)
            await asyncio.to_thread(self.bm25.remove_by_ids, [str(doc_id)])
            return len(ids_to_delete)

    async def delete_kb_chunks(self, kb_id: str) -> int:
        await self.ensure_ready()
        async with self._chroma_write_lock:
            results = await asyncio.to_thread(
                self.chroma_collection.get,
                where={"kb_id": str(kb_id)},
            )
            ids_to_delete = results.get("ids") or []
            if ids_to_delete:
                await asyncio.to_thread(self.chroma_collection.delete, ids=ids_to_delete)
            await asyncio.to_thread(self.bm25.remove_by_kb, str(kb_id))
            return len(ids_to_delete)

    # ══════════════════════════════════════════════════════════════
    # 混合检索
    # ══════════════════════════════════════════════════════════════
    async def hybrid_search(
        self,
        query: str,
        kb_ids: list,
        top_k: int = 5,
        score_threshold: float = 0.4,
        hybrid_alpha: float = 0.5,
        enable_reranker: bool = False,
    ) -> List[ChunkMatch]:
        if not query.strip():
            return []
        await self.ensure_ready()

        start_time = time.monotonic()
        kb_ids_str = [str(k) for k in kb_ids]
        recall_size = min(top_k * 3, 50)

        async def vector_search() -> dict:
            if hybrid_alpha <= 0.0:
                return {}
            try:
                q_emb = await self.embedder.embed_single(query)
                where = None
                if len(kb_ids_str) == 1:
                    where = {"kb_id": kb_ids_str[0]}
                elif kb_ids_str:
                    where = {"kb_id": {"$in": kb_ids_str}}
                chroma_hits = await asyncio.to_thread(
                    self.chroma_collection.query,
                    query_embeddings=[q_emb],
                    n_results=recall_size,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
                results = {}
                if chroma_hits and chroma_hits["ids"] and chroma_hits["ids"][0]:
                    for j, cid in enumerate(chroma_hits["ids"][0]):
                        distance = chroma_hits["distances"][0][j]
                        score = min(max(1.0 - float(distance), 0.0), 1.0)
                        if score < score_threshold:
                            continue
                        results[cid] = {
                            "score": score,
                            "content": chroma_hits["documents"][0][j] if chroma_hits["documents"] else "",
                            "filename": (chroma_hits["metadatas"][0][j] or {}).get("filename", "") if chroma_hits["metadatas"] else "",
                            "doc_id": (chroma_hits["metadatas"][0][j] or {}).get("doc_id", "") if chroma_hits["metadatas"] else "",
                            "kb_id": (chroma_hits["metadatas"][0][j] or {}).get("kb_id", "") if chroma_hits["metadatas"] else "",
                            "chunk_index": (chroma_hits["metadatas"][0][j] or {}).get("chunk_index", 0) if chroma_hits["metadatas"] else 0,
                        }
                return results
            except Exception as e:
                logger.error(f"Vector search error: {e}")
                return {}

        async def bm25_search() -> dict:
            if hybrid_alpha >= 1.0:
                return {}
            try:
                bm = await asyncio.to_thread(
                    self.bm25.search,
                    query,
                    recall_size,
                    kb_ids_str,
                )
                results = {}
                for hit in bm:
                    cid = hit.get("chunk_id", "")
                    if cid:
                        results[cid] = {
                            "score": float(hit.get("score", 0)),
                            "content": hit.get("content", ""),
                            "filename": hit.get("filename", ""),
                            "doc_id": hit.get("doc_id", ""),
                            "kb_id": hit.get("kb_id", ""),
                            "chunk_index": hit.get("chunk_index", 0),
                        }
                return results
            except Exception as e:
                logger.error(f"BM25 search error: {e}")
                return {}

        vec_results, bm25_results = await asyncio.gather(
            vector_search(),
            bm25_search(),
        )

        # Vector scores are absolute cosine similarities and must not be
        # collection-normalized. BM25 has no bounded absolute scale, so keep
        # its score relative to the current recall set.
        vector_scores = {
            chunk_id: result["score"]
            for chunk_id, result in vec_results.items()
        }

        def normalize_bm25(results: dict) -> dict:
            if not results:
                return {}
            scores = [max(float(value["score"]), 0.0) for value in results.values()]
            mx, mn = max(scores), min(scores)
            if mx == mn:
                normalized = 1.0 if mx > 0.0 else 0.0
                return {chunk_id: normalized for chunk_id in results}
            return {
                chunk_id: (max(float(value["score"]), 0.0) - mn) / (mx - mn)
                for chunk_id, value in results.items()
            }

        bm25_scores = normalize_bm25(bm25_results)

        # If one retrieval path fails or produces no usable candidates, its
        # configured weight must not suppress the healthy path. Explicit pure
        # vector/keyword modes still behave as requested because the other
        # path is not executed at alpha=1/0.
        vector_weight = hybrid_alpha if vector_scores else 0.0
        bm25_weight = (1.0 - hybrid_alpha) if bm25_scores else 0.0
        available_weight = vector_weight + bm25_weight
        if available_weight <= 0.0:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.info(
                "Hybrid search: found=0 alpha=%s effective_weights=(0.000,0.000) time=%sms",
                hybrid_alpha,
                elapsed,
            )
            return []

        vector_weight /= available_weight
        bm25_weight /= available_weight

        fused = {}
        all_ids = set(vector_scores) | set(bm25_scores)
        for cid in all_ids:
            vector_score = vector_scores.get(cid, 0.0)
            bm25_score = bm25_scores.get(cid, 0.0)
            fused_score = (
                vector_weight * vector_score
                + bm25_weight * bm25_score
            )
            if fused_score >= score_threshold:
                src = vec_results.get(cid) or bm25_results.get(cid) or {}
                fused[cid] = {
                    "score": fused_score,
                    "hybrid_score": fused_score,
                    "payload": src,
                    "components": {
                        "vector_similarity": (
                            vector_scores[cid] if cid in vector_scores else None
                        ),
                        "bm25_raw_score": (
                            float(bm25_results[cid]["score"])
                            if cid in bm25_results else None
                        ),
                        "bm25_normalized_score": (
                            bm25_scores[cid] if cid in bm25_scores else None
                        ),
                        "vector_weight": vector_weight,
                        "bm25_weight": bm25_weight,
                    },
                }

        sorted_items = sorted(fused.items(), key=lambda x: x[1]["score"], reverse=True)[:recall_size]

        # Optional rerank
        if enable_reranker and sorted_items:
            docs_for_rerank = [
                {
                    "chunk_id": cid,
                    "content": d["payload"].get("content", ""),
                    "score": d["score"],
                    "hybrid_score": d["hybrid_score"],
                    "payload": d["payload"],
                    "components": d["components"],
                }
                for cid, d in sorted_items
            ]
            reranked = await self.reranker.rerank(query, docs_for_rerank, top_k)
            sorted_items = [
                (
                    doc["chunk_id"],
                    {
                        "score": min(max(float(doc["score"]), 0.0), 1.0),
                        "hybrid_score": doc["hybrid_score"],
                        "payload": doc["payload"],
                        "components": doc["components"],
                        "reranked": True,
                    },
                )
                for doc in reranked[:top_k]
            ]
        else:
            sorted_items = sorted_items[:top_k]

        matches = []
        for cid, data in sorted_items:
            p = data["payload"]
            score_breakdown = {
                key: round(value, 6) if value is not None else None
                for key, value in data["components"].items()
            }
            score_breakdown["hybrid_score"] = round(data["hybrid_score"], 6)
            if data.get("reranked"):
                score_breakdown["reranker_score"] = round(data["score"], 6)
            matches.append(ChunkMatch(
                chunk_id=cid,
                content=p.get("content", ""),
                filename=p.get("filename", "未知文件"),
                doc_id=p.get("doc_id", ""),
                score=round(data["score"], 4),
                metadata={
                    "kb_id": p.get("kb_id", ""),
                    "chunk_index": p.get("chunk_index", 0),
                    "score_breakdown": score_breakdown,
                },
            ))

        elapsed = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "Hybrid search: found=%s alpha=%s effective_weights=(%.3f,%.3f) time=%sms",
            len(matches),
            hybrid_alpha,
            vector_weight,
            bm25_weight,
            elapsed,
        )
        return matches

    def parse_document_sync(self, file_path: str, filename: str, kb_id: str, doc_id: str) -> List[Dict[str, Any]]:
        return self.processor.parse_and_chunk(file_path, filename, kb_id, doc_id)


# ══════════════════════════════════════════════════════════════════
# 单例
# ══════════════════════════════════════════════════════════════════
_rag_engine: Optional[RAGEngine] = None

def get_rag_engine() -> RAGEngine:
    global _rag_engine
    if _rag_engine is None:
        _rag_engine = RAGEngine()
    return _rag_engine
