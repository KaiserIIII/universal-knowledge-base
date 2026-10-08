# 知序：部署与升级

知序免费、开源、自托管，默认不收集遥测数据。成员、知识库、文档和回答次数默认不限，核心服务不依赖外部商业服务。

## 运行环境

本次离线 API 验证环境为 Windows AMD64、Python 3.13。`requirements-core.lock` 固定轻量服务依赖；安装 core 后可以启动账号、组织和管理接口。实际文档向量索引需要另装 `requirements-rag.lock`，并准备本地 Embedding 模型。后者固定直接依赖版本，其机器学习传递依赖仍由 pip 解析；请在部署平台冻结完整环境后再发布镜像。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements-rag.lock
Copy-Item backend/.env.template backend/.env
python start.py
```

Linux 使用 `.venv/bin/python`。启动器使用已安装的环境并在前台运行，Ctrl+C 停止；不自动升级 pip 或下载其他 Skill。首次使用本地模型时可能从模型仓库下载模型，离线部署应预先准备缓存。默认监听 `127.0.0.1:8000`；远程部署由反向代理提供 HTTPS，设置准确的 `PUBLIC_ORIGIN`、`COOKIE_SECURE=true`，按需要关闭注册。管理页面与 API 同源，不依赖外部字体或脚本 CDN。

原生 Windows 上，已观察到 **Chroma 1.5.9 加载复制的既有 HNSW 索引**时，含中文的仓库路径出现 `chromadb.errors.InternalError: Error loading hnsw index`；同一副本放到绝对 ASCII 路径后可读取向量数量和 embedding（29,244 个向量）。此结论仅限该版本、环境和既有索引副本，不表示所有中文路径或新建索引都会失败。已有索引的本地部署应使用经备份、核对过的 ASCII 路径副本，并在启动前设置例如：

```powershell
$env:CHROMA_PERSIST_DIR = 'C:/knowledge-saas-data/chroma'
python start.py
```

示例路径应指向实际准备好的索引目录。不要通过重置或覆盖索引来消除加载错误，也不要自动迁移原始数据。

## Docker Compose

项目名固定为 `knowledge-saas`，可在中文目录执行。根目录的 `.env` 用于 Compose 插值，不能提交。

```dotenv
PUBLIC_ORIGIN=https://kb.example.com
COOKIE_SECURE=true
REGISTRATION_ENABLED=false
```

首次初始化账号时可临时开启注册，然后关闭。通过部署环境配置模型凭据；不要将密钥填入知识库文档或画布参数。

```bash
docker compose config --quiet
docker compose up --build -d app
```

默认构建 `rag` 阶段，使用具名卷保存 SQLite、索引与上传源文件，容器以非 root 用户运行。`core` 构建阶段适合独立 API 测试或自行注入检索适配器。应用端口仅发布到本机回环地址。

Compose 的 Linux 容器内索引路径为 ASCII `/app/backend/app/data/saas/chroma`；宿主机项目目录含中文本身并不复现上述原生 Windows 既有索引路径条件。Compose 健康检查使用数据库 readiness，见下文。

需要 PostgreSQL 时设置强 `POSTGRES_PASSWORD`，同时把 `DATABASE_URL` 设置为 `postgresql+asyncpg://enterprise_kb:<URL编码密码>@postgres:5432/enterprise_kb`，然后执行：

```bash
docker compose --profile postgres up --build -d
```

应用启动需要数据库已就绪；服务启动失败会依照重启策略重试。SQLite 写操作串行化，适合单实例部署。生产并发应使用 PostgreSQL；共享 Chroma 的多进程写入及长时间外部索引调用需要另行验证，当前不承诺集群恰好一次导入。

本机验证了 Compose 配置解析，尚未执行 Docker 构建、容器运行或真实 PostgreSQL 验收。

## 旧版本数据迁移

升级默认使用新的 `data/saas` 目录。新注册账号只能访问自己创建的组织，不会自动认领旧版 `default` 工作区。迁移前停写并备份关系数据库、Chroma 目录、源文件与模型配置；在副本验证后再处理实际数据。

1. 显式把新服务指向需要升级的关系数据库；表结构以增量创建，不重置已有数据。在该数据库注册管理员账号。
2. 从 `backend` 目录执行只读预览：

```bash
python -m app.saas.migrate_legacy --database-url "sqlite+aiosqlite:////absolute/path/knowledge.db" --workspace-id default --owner-email owner@example.com
```

3. 核对工作区、知识库和文档数量，添加 `--apply` 执行。UUID 工作区可用对应 UUID 替代 `default`。已认领、无有效管理员、重复文档哈希或重复片段序号会拒绝执行；工具不会删除重复内容。

旧 SQLite 的 `default` 工作区会获得 UUID；知识库、文档、片段 ID 保持原值。配置原有 Chroma 目录与相同 Embedding 模型，再抽查检索。迁移命令不复制或重建向量、不自动导入私有文件、不改变旧文档的切片策略。失败时检查副本和备份，避免盲目重复修改数据。

原生 Windows 的既有 Chroma 1.5.9 索引副本若位于中文路径，先按上面的 `CHROMA_PERSIST_DIR` 指引，在绝对 ASCII 路径的受控副本验证加载；保留原索引、备份与校验记录。SQL 迁移命令不会解决 HNSW 文件路径兼容问题。

## 模型凭据

模型连接只保存环境变量引用。部署者同时限定 `MODEL_ALLOWED_HOSTS` 与 `MODEL_ALLOWED_SECRET_REFS`；新增提供者前核对服务地址及出站网络权限。共享环境变量只适用于部署者认可的组织；需要每租户独立密钥时应增加外部密钥管理层。

默认模型的 `LLM_API_KEY` 是具名配置项，可放在 `backend/.env`。管理端模型连接的任意 `api_key_env` 引用从**进程环境**解析，不会从该文件导出。使用进程管理器或容器 Secret 注入后重启服务；本机 PowerShell 可在启动前设置 `$env:SUPPORT_MODEL_API_KEY`。允许列表中只填写变量名称。

Compose 在根目录 `.env` 配置 `SUPPORT_MODEL_API_KEY` 后显式传入 app 服务。需要多家提供者的独立凭据时，在 Compose 覆盖文件的 `services.app.environment` 增加相应变量映射，并把这些名称加入 `MODEL_ALLOWED_SECRET_REFS`；仅增加允许列表不会把宿主机变量传入容器。模型配置中的协议值为 `openai-compatible`，地址和模型 ID 决定调用目标。

自建私有地址须同时列入 `MODEL_ALLOWED_HOSTS` 与 `MODEL_PRIVATE_HOSTS`；默认只允许 HTTPS。无 TLS 的本地开发服务还需要显式设置 `MODEL_ALLOW_HTTP=true`。允许的主机应由部署者控制解析和出站网络策略，不能把允许列表视作完整的网络沙箱。


## 运维检查

- `/api/v1/health` 是轻量进程存活检查，返回 `{"status":"ok"}`。
- `/api/v1/ready` 是 mandatory 数据库 readiness：在 2 秒期限内通过短会话执行 `SELECT 1`，成功返回 200 `{"status":"ready"}`；连接、查询或超时失败返回 503 `{"status":"unavailable"}`，不泄露底层异常。Compose 用此端点判定服务就绪；反向代理就绪探针也应使用它。两个探针均不下载或初始化 Embedding、不联系 LLM；readiness 不证明这些可选能力或真实检索质量可用。
- 导入任务写入数据库，源文件保留供失败重试；达到最大次数后由管理员删除或处理。失败源文件没有自动 TTL，需关注磁盘容量。
- 问答超时、断线与取消会记录终态。进程直接崩溃时，回答可能仍标记为 `running`；运维人员应先确认原执行进程已停止，再核对会话和消息状态，避免改写其他实例正在执行的请求。工作流通过租约恢复失去执行进程的任务。
- 备份数据库、索引和上传文件须保持同一业务时间点；定期用副本恢复并抽查检索。
- 更换 Embedding 模型或维度必须建立新索引并安排显式迁移，不能直接覆盖共享索引。
- 日志不记录密钥、模型响应或文档正文；错误响应使用可定位的请求 ID。生产环境仍需配套反向代理、备份、监控及容量规划。

## 本地验收

```bash
python -m unittest discover -s tests -v
python -m compileall -q backend/app tests start.py
python -m pip check
node --test tests/web/*.test.mjs
```

测试使用临时 SQLite 与合成适配器，无真实模型调用。检索质量与回答忠实度需结合实际资料人工抽检，不能从接口测试通过推断。


## 开源版数据兼容

旧工作区、知识库、文档、会话和工作流记录保持原 ID。旧的账户核算表与字段保留但不用于运行判断。启动迁移只解除工作流历史关联字段的非空约束与外键，允许新任务不依赖历史核算记录；SQLite 在事务中复制全部原列和数据、重建索引与触发器，失败回滚，PostgreSQL 修改对应约束。迁移可重复执行；上线前仍应备份并在副本检查记录数量和实际检索。

旧知识库参数表增量增加 parser_config，默认自动本地解析；已完成文档不会被自动重解析。更换解析模块或参数只影响后续导入与显式重试。

## 本地解析与运行保护

在知识库参数中选择自动、本地原生或纯文本模块，可设置 UTF-8、GB18030、UTF-16、自动识别以及每表行数。详见 [文件解析模块](parsing.md)。画布的“文件资料”节点使用已解析完成的文件，不接收任意服务器路径。没有可提取文本的扫描 PDF 会显示失败，当前不启用 OCR 或图像识别。

管理员在本地配置 MAX_UPLOAD_SIZE_MB、MAX_FILES_PER_BATCH、超时和 Token 预算进行单次任务运行保护；这些参数不限制团队总成员、知识库、文档或回答数。CPU、内存、磁盘和反向代理保护由部署者管理。

Chroma 客户端显式关闭匿名使用统计和遥测端点，Compose 同时设置 ANONYMIZED_TELEMETRY=false。模型权重首次下载与管理员配置的外部模型调用属于主动启用的能力，离线部署应预备权重并使用本地模型端点。

## 许可证与贡献

使用 [Apache-2.0](../LICENSE)。提交改动请遵循 [贡献指南](../CONTRIBUTING.md)，保持中英文界面和文档同步。
