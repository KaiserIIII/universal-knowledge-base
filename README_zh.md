# 通用知识库

这是一个可自托管、可扩展的本地优先 RAG 通用知识库系统。它适合个人资料、团队文档、技术手册、项目规范和企业内部资料的统一管理与问答。

系统支持多个知识库、批量导入文档、混合检索、带来源引用的流式问答，以及从网页直接删除知识库和文件。所有运行参数都可以在 Web UI 中调整并自动保存，不需要再编辑启动脚本。

技术栈：FastAPI、SQLAlchemy 2.0、SQLite、ChromaDB、本地 BM25、LangChain 文本切分、PyTorch Embedding，以及 Tailwind CDN + 原生 ES6 单页前端。

## 功能概览

- 创建、重命名和删除多个知识库
- 上传并解析 PDF、Word、Markdown、TXT、HTML 等常见文档
- 同时使用向量检索与关键词检索，支持跨知识库问答
- 回答中返回可追溯的文档引用和片段
- Web UI 实时调整模型、检索、切分、上传和运行参数
- NVIDIA GPU 可选；已验证 RTX 4060 + CUDA 12.6
- 提供 OpenAI 兼容 LLM 接口，可接入 DeepSeek、vLLM 等服务

## 启动

Windows 下双击 `start.bat`，或执行：

```powershell
python start.py
```

启动器会创建 `backend/venv`、安装依赖、检查 `backend/.env`，并在服务就绪后打开 [http://localhost:8000](http://localhost:8000)。API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

首次启动需要联网下载 Python 依赖和 Embedding 模型，耗时取决于网络速度。之后再次启动会复用本地虚拟环境和模型缓存。

聊天接口可以在 Web UI 的“检索设置”中配置 OpenAI 兼容模型参数，也可以在 `backend/.env` 中预置：

```dotenv
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=你的密钥
LLM_MODEL=deepseek-chat
```

没有配置密钥时，知识库管理、文档解析和检索仍可使用；聊天接口会明确返回 `503`。API Key 只应保存在本机忽略的配置中，不要提交到 GitHub。

也可以直接在 Web UI 的设置面板填写 API Key。页面不会回显已保存的 Key，留空保存其他设置时会保留原有 Key。

## 使用方式

左侧可以直接创建、选择和删除知识库。当前知识库是文档导入目标，右侧设置可勾选一个或多个知识库作为对话检索范围：导入只写入当前知识库，对话可以跨库检索。知识库和文件均可从 Web UI 删除，设置会自动保存。

## 数据与检索

| 组件 | 默认实现 |
|---|---|
| 关系数据库 | SQLite + aiosqlite，生产可切换 PostgreSQL + asyncpg |
| 向量索引 | 本地持久化 ChromaDB |
| 关键词索引 | jieba 分词 + 本地 BM25 |
| Embedding | `BAAI/bge-small-zh-v1.5`，动态校验 512 维 |
| 文档解析 | unstructured，失败时回退 pypdf/python-docx/BeautifulSoup |
| 文本切分 | LangChain `RecursiveCharacterTextSplitter` |
| LLM | DeepSeek、vLLM 或其他 OpenAI 兼容接口 |

Chroma、Embedding 和 BM25 的同步计算都从 FastAPI 事件循环转移到工作线程。上传文件仅作为后台解析的临时文件，管道结束后会清理。

### GPU（可选）

默认配置使用 CPU，适合没有 NVIDIA 环境的机器。Windows + NVIDIA 用户可以按官方 PyTorch 安装页选择匹配驱动的 CUDA wheel，然后在 Web UI 的“设备”中选择 `CUDA`。本机 RTX 4060 已验证使用 CUDA 12.6：

```powershell
backend\venv\Scripts\python.exe -m pip install --upgrade --force-reinstall --no-deps torch==2.13.0+cu126 --index-url https://download.pytorch.org/whl/cu126
```

服务启动后可在 `/api/v1/agent/status` 查看实际的 `embedding_device`。如果 CUDA 不可用，系统会自动回退到 CPU。

## PostgreSQL

需要 PostgreSQL 时可执行 `docker compose up -d`，然后将连接串改为：

```dotenv
DATABASE_URL=postgresql+asyncpg://enterprise_kb:change-me-before-production@127.0.0.1:5432/enterprise_kb
```

ChromaDB、BM25 与 Embedding 仍在应用进程内运行，不需要 Qdrant、Elasticsearch 或 Redis。

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/kb` | 创建知识库 |
| `GET` | `/api/v1/kb?limit=200` | 获取知识库列表 |
| `DELETE` | `/api/v1/kb/{kb_id}?hard=true` | 删除知识库及其文件 |
| `GET` | `/api/v1/kb/{kb_id}/documents?limit=200` | 获取文档列表 |
| `POST` | `/api/v1/kb/{kb_id}/documents` | 批量接收文档并返回 `202` |
| `DELETE` | `/api/v1/kb/{kb_id}/documents/{doc_id}` | 删除文档 |
| `GET/PATCH` | `/api/v1/settings` | 读取或实时保存 Web UI 设置 |
| `POST` | `/api/v1/agent/search` | Alpha 可调的混合检索 |
| `POST` | `/api/v1/chat/completions` | 带引用的 SSE 流式对话 |
| `GET` | `/health` | 数据库与本地检索组件健康状态 |

## 目录

```text
index.html                  单页前端
start.bat / start.py        本地启动器
backend/app/main.py         FastAPI 入口与启动迁移
backend/app/models.py       SQLAlchemy 2.0 领域模型
backend/app/rag_engine.py   解析、切分、Embedding、Chroma、BM25
backend/app/routers/        知识库、文档、检索、聊天和设置接口
backend/app/data/           SQLite 与 Chroma 持久化数据
backend/uploads/            处理中的临时文件
```

## GitHub Desktop 发布

下面的流程只需要第一次操作一次，适合 Windows 用户：

1. 安装并登录 [GitHub Desktop](https://desktop.github.com/)。登录账号需要有权创建 GitHub 仓库。
2. 打开 GitHub Desktop，选择 `File` > `Add local repository` > `Choose...`，选择本项目目录：
   `E:\学习\实习\知识库`
3. 这个目录已经提前初始化为 Git 仓库，GitHub Desktop 通常会直接识别 `main` 分支，不需要再次创建。如果仍提示“不是 Git 仓库”，才点击 `create a repository`，名称建议使用英文，例如 `universal-knowledge-base`。本项目已经包含 `.gitignore`，本地数据库、上传文件、虚拟环境和密钥不会被加入提交。
4. 在左下角填写提交信息，例如 `Initial release: universal knowledge base`，点击 `Commit to main`。
5. 点击顶部 `Publish repository`，填写仓库名称和简介：
   - Name：`universal-knowledge-base`
   - Description：`A self-hosted RAG knowledge base with hybrid search, citations, and Web UI settings.`
   - 是否公开按你的需要选择；公开发布前请再次确认没有把 API Key 或私密文档放入仓库。
6. 点击 `Publish repository` 完成上传。以后修改代码后，在 GitHub Desktop 中检查 Changes，填写提交信息，点击 `Commit to main`，再点击 `Push origin`。

### 发布前检查

- 确认 `backend/.env` 不在 Changes 列表中
- 确认 `backend/app/data`、`output`、`backend/venv` 不在 Changes 列表中
- 确认没有把内部合同、客户资料或带隐私信息的文档放进项目目录
- GitHub 页面能看到 `README.md`、`start.bat` 和 `index.html`

### 从 GitHub 重新获取项目

在另一台 Windows 电脑上，GitHub Desktop 选择 `File` > `Clone repository`，选择刚发布的仓库和本地目录。克隆完成后双击 `start.bat`，再在 Web UI 中配置 LLM 和其他运行参数。

## 常见问题

### 聊天提示 `LLM_API_KEY is not configured`

打开 Web UI 的设置面板，填写 LLM API URL、模型名和 API Key 后保存。API Key 不会显示在读取接口中；如果只修改其他设置，Key 会继续保留在服务端配置中。

### CUDA 没有生效

确认 NVIDIA 驱动正常，并在 Web UI 的“设备”中选择 `CUDA`。启动后访问 `/api/v1/agent/status`，查看 `models.embedding_device` 是否为 `cuda`。不可用时系统会自动回退到 CPU。

### 端口 8000 已被占用

关闭占用端口的程序后重新运行 `start.bat`。正在运行的服务可以双击 `stop.bat` 关闭。
