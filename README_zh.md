# 通用知识库

[English](README.md)

可自托管的 RAG 文档问答应用，将向量检索与 BM25 关键词检索结合，通过 Web 界面完成文档导入、多知识库搜索和模型配置，并为回答提供来源引用。

## 功能

- 创建和管理多个知识库。
- 批量导入 PDF、Word、Markdown、文本和 HTML 文档。
- 结合语义检索与关键词匹配。
- 跨选定知识库搜索，以流式方式输出带引用的回答。
- 配置检索、切片、上传、模型和 Embedding 运行设备。
- 使用本地向量索引，并连接可配置的 OpenAI 兼容聊天接口。

## 架构

| 模块 | 实现 |
| --- | --- |
| API | FastAPI |
| 关系存储 | SQLite + SQLAlchemy；可选 PostgreSQL |
| 向量存储 | 持久化 ChromaDB |
| 关键词检索 | jieba + BM25 |
| 默认 Embedding | `BAAI/bge-small-zh-v1.5` |
| 文档解析 | unstructured 与各格式的回退解析器 |
| 界面 | JavaScript 单页应用 |

## 快速开始

在 Windows 中执行：

```powershell
python start.py
```

也可运行 `start.bat`。启动器准备 `backend/venv`、安装依赖、检查配置，并打开 <http://localhost:8000>。API 文档位于 <http://localhost:8000/docs>。

首次启动会下载依赖与 Embedding 模型，后续启动复用环境和模型缓存。

## 模型配置

通过 Web 界面配置，或编辑 `backend/.env`：

```dotenv
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=replace-with-your-key
LLM_MODEL=deepseek-chat
```

未配置 API Key 时，文档管理和检索仍可使用，聊天请求返回 `503`。默认使用 CPU 生成 Embedding；CUDA 为可选项，不可用时回退到 CPU。

## PostgreSQL

仓库中的 Compose 文件用于启动 PostgreSQL：

```bash
docker compose up -d
```

在 `backend/.env` 中将 `DATABASE_URL` 配置为匹配的数据库凭据。ChromaDB、BM25 与 Embedding 仍在应用本地运行。

## 代码导航

- [backend/app/routers/](backend/app/routers/)：知识库、文档、设置、检索与聊天接口。
- [backend/app/rag_engine.py](backend/app/rag_engine.py)：文档处理与检索。
- [backend/app/database.py](backend/app/database.py)：数据库初始化。
- [index.html](index.html)：Web 界面。
- [backend/.env.template](backend/.env.template)：配置参考。

## 部署

使用外部聊天接口时，生成回答所选的上下文会发送至该接口，应按数据要求选择服务。凭据应保存在 Git 之外；备份数据库与索引，并将远程访问限制在可信网络内。

仓库尚未指定覆盖整个项目的许可证。
