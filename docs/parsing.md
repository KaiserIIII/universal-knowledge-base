# File parsing modules / 文件解析模块

Knowledge base → Parser module → Persistent import job → Parsed chunks → Local index
→ Retrieval or File evidence node → Evidence gate → Model → Citations.

## Choose a module

In knowledge base parameters, configure a parser before importing files:

| Module | Behavior / 行为 |
| --- | --- |
| Automatic / 自动本地解析 | Built-in bounded readers; honors the existing optional local high-precision setting for non-Office formats. |
| Native / 原生文档解析 | Local text PDF, Office XML, visible HTML, CSV/TSV tables and encoding-aware text; no external calls. |
| Text / 纯文本解析 | TXT, Markdown, JSON, CSV/TSV as plain text; HTML still strips scripts and styles. Binary documents are rejected. |

Encoding options are auto, UTF-8, GB18030 and UTF-16. Auto recognizes Unicode BOMs,
then tries UTF-8 and GB18030 without silently replacing invalid text. The row limit
applies to CSV/TSV and each Excel sheet; truncation is marked in the parsed content.
Changing settings affects subsequent imports and explicit retries, leaving completed
documents and their citations intact.

原生模块支持 `.txt`、`.md`、`.markdown`、`.csv`、`.tsv`、`.json`、`.html`、`.htm`、
`.pdf`、`.docx`、`.xlsx`、`.pptx`。解析器不会执行宏、公式或文档中的指令。
Office 压缩包在读取前检查成员路径、重复文件、加密、膨胀率与 XML 声明。
表格、文本输出和上传有本地处理保护。

Images, scanned-document OCR, legacy DOC/XLS/PPT conversion and vision model routing
are not integrated. A scanned PDF without extractable text fails visibly rather
than creating a successful empty document. These are explicit extension points;
they are not advertised as available capabilities.

## Compose file evidence

Drag **File evidence / 文件资料** onto the workflow canvas, connect Question input
to it, select knowledge bases and completed files, then connect to an evidence
gate, model or answer output. Use “Load more files” to browse additional document
pages. The module returns document chunks in selection order with a configured
chunk and context budget. It does not rank relevance; use Retrieval for semantic
matching or combine both paths through deduplication and reranking.

文件资料节点在保存、发布和执行时校验组织、知识库和文档范围；删除中或未解析完成的
文档不能成为证据。节点读取 SQL 原文，不相信索引携带的正文，也不接受服务器文件路径。

## Receipts and extension contract

Each upload has a SHA-256 receipt and an isolated source path. Before parsing, the
worker verifies retained bytes against the receipt. Chunks record `source_sha256`
and `parser_mode`, binding the result to the imported document. A changed source
fails before parsing/indexing. Original files remain available for explicit retry.

`app/parsing.py` owns parser settings, supported formats, decoding and table
extraction. `DocumentProcessor` coordinates native extraction and chunking;
`RAGRetriever.parse` is the asynchronous adapter used by leased ingestion jobs.
`GET /api/v1/parsers` exposes the authenticated catalog. Future adapters must return
the same chunk contract, respect local limits, report unavailable dependencies and
preserve receipts and tenant validation. Do not introduce arbitrary executable
plugins, external calls or path-based nodes through saved workflow configuration.

## Reference provenance

The attachment-receipt and bounded-parser design was reviewed against the author's
[LangBot Unified Attachment Pipeline](https://github.com/KaiserIIII/langbot-unified-attachment-pipeline/tree/3c7ea58a67a9a30bc0c92738b8f9bcde63a34464),
Apache-2.0, commit `3c7ea58a67a9a30bc0c92738b8f9bcde63a34464`, reviewed 2026-10-08.
This integration is independently implemented; no reference library or additional
dependency was installed. Its archive memory and vision features remain separate.
