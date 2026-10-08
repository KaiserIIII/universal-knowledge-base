# 多租户企业知识 SaaS 设计

日期：2026-10-07。用户已选择路线 C：完整多租户 SaaS；本文件明确实现范围与验收。

## 定位

面向企业内部知识库与客服团队的“证据可查、效果可测、缺口可运营”知识 SaaS。保留本地 Embedding、ChromaDB、中文 BM25 与 OpenAI 兼容 LLM，并提供组织账号、团队权限、订阅配额和计费接入。

## 交付边界

交付能运行的产品源码、界面、测试、部署配置和 GitHub PR。租户使用共享数据库与向量索引，所有读取、写入与检索实施组织范围校验。支付使用可配置的 Stripe Checkout、客户门户和签名 webhook；没有支付凭证时明确显示未配置，不模拟已付款。真实收款、生产域名与正式上线需要运营者配置，本次不访问真实支付账户或发送团队邀请邮件。

## 分阶段实现，一次集成验收

### 1. 身份与租户基础

- 用户注册、登录、退出、会话撤销与账号资料；密码采用随机盐与 PBKDF2-HMAC-SHA256，登录令牌只存摘要并限时失效。
- 登录使用 HttpOnly、SameSite cookie，生产配置 Secure；写请求校验 CSRF，API 集成使用可撤销的 Bearer 凭证。令牌与 API Key 不写入浏览器永久存储或响应日志。
- 每次注册创建组织与 owner 关系；已有账号可加入多个组织。组织内角色为 owner、admin、editor、viewer。
- 成员邀请使用限时、一次性链接，由管理员复制分享；受邀账号与邀请目标邮箱必须匹配，不自动发送邮件。
- 最后一个 owner 不得被移除或降级；成员被移除后权限立即失效。
- 每个请求必须解析组织上下文，数据库资源与检索知识库 ID 双重验证。未知、其他组织或删除资源统一返回 404。
- 现有工作空间作为组织资源；旧数据保持存在且默认不向新注册用户公开，提供显式管理员迁移/认领流程。

### 2. 订阅、配额和支付

- 套餐为 Free、Team、Business。默认配额分别为：成员 1/10/50，知识库 3/20/100，文档 50/1000/10000，每月问答 100/5000/50000；价格由部署者的 Stripe Price ID 决定，不捏造市场定价。
- 组织拥有订阅记录、月度用量和限制；服务器执行配额，原子更新防止并发绕过。失败的上游调用不计入成功问答使用量，查询保留执行记录。
- Checkout session 由服务端套餐与 Price ID 创建，不接受客户端任意价格、客户 ID 或组织 ID。
- webhook 使用原始请求体验证 HMAC-SHA256 与时间窗口；事件 ID 幂等，旧事件不能覆盖新状态；订阅状态、price 与绑定组织都核对后才变更权益。
- active/trialing 订阅使用对应付费权益；past_due、unpaid、canceled 与 deleted 状态展示真实状态并按明确策略退回 Free 权益。成功跳转本身不升级套餐。
- 提供客户门户入口和未配置错误；测试使用 Fake HTTP，不联系 Stripe。

### 3. 知识管理与可靠导入

- 租户范围内的知识库、文档上传、状态、片段查看与删除。viewer 可读，editor 可维护知识，admin/owner 管理团队与订阅。
- 新增持久导入任务：排队、处理、完成、失败，保存处理参数与重试次数；恢复可重试任务，重试使用仍保留的上传文件并有上限。
- 临时文件只在任务终态按保留策略清理；失败、重复上传和任务中断有明确状态，不删除失败记录。
- 原有文档和向量集合不强制改名、不整库重建；迁移只新增表/索引，重复执行安全。

### 4. 问答、证据和客服运营

- 持久会话、消息、历史、来源快照、耗时与状态。服务端只使用当前租户当前会话的有界历史。
- 严格依据模式默认启用：无合格证据时返回资料不足，不请求 LLM。
- 同步/SSE 共用问答服务；保持 OpenAI 风格 delta 与 DONE，增加会话/消息 ID、来源、证据状态和完成元数据。
- 来源展示全文片段、文档与知识库 ID、片段序号和真实分数拆解；没有实际页码时不制造页码。
- 检索资料按不可信数据处理，固定约束优先；检查无效引用编号并明确报告，不把引用覆盖率叫作回答准确率。
- 消息支持有帮助/无帮助与原因反馈；运营看板展示文档失败、资料不足、负反馈问题与实测耗时。上游故障与资料缺口分开统计。
- 会话导出 Markdown，导出与引用查询也验证租户范围。

### 5. 检索实验室与评测

- 无 LLM 密钥也可比较最多 3 组配置，显示排名、来源、分数和耗时。
- 每次最多 50 个标注问题、每组 top-k 最多 20；按去重文档 ID 计算 Context Precision、Context Recall、MRR、Hit Rate、nDCG。
- 未标注时质量指标为空。Faithfulness 使用中文人工评审记录；未经评审为空，保留检查表和抽检样本，不向外部 judge 发送企业资料。
- 合成售后数据覆盖正常问答、型号混淆、资料不足与恶意文档，绝不使用本机真实知识库作公开演示。

### 6. SaaS 界面与可交付工程

- 前端使用本地 CSS 与 ES Modules，提供注册/登录、组织切换、概览、知识库、会话、证据侧栏、检索实验室、团队、订阅与设置视图，适配桌面和手机。
- 后端提供应用工厂与可替换检索/LLM/支付依赖；路由、权限、领域服务和适配器分离。
- 支持 SQLite 本机试用和 PostgreSQL SaaS 部署配置；未实测的环境明确记录，不宣称验证通过。
- 增加依赖版本锁、Dockerfile、Compose、健康与就绪检查、操作审计、GitHub Actions、部署手册与中英文 README。
- 外部新依赖如需引入，必须审核来源、许可证与 commit 并留审计；不克隆或递归安装 Skill。CI Actions 固定审核 commit。
- 不擅自新增许可证；保留未提交的个人化改动，公共文档清除个人路径。默认配置不读取、上传或发布本机资料。

## 模块和接口约定

`app/saas/` 管理用户、会话、成员、邀请、API 凭证、订阅、用量和审计；现有知识模型继续使用 workspace_id 关联组织。`app/services/` 管理问答、会话、评测和运营；`app/routers/` 负责协议。

认证接口 `/api/v1/auth/{register,login,logout,me}`；组织接口 `/api/v1/organizations` 与成员/邀请子资源；账单接口 `/api/v1/billing/{plans,subscription,checkout,portal,webhook}`。知识与聊天保留现有前缀并增加服务端租户上下文。会话、消息反馈、评测与看板分别使用 `/api/v1/conversations`、`/api/v1/messages/{id}/feedback`、`/api/v1/evaluation/retrieval`、`/api/v1/insights`。

所有领域记录使用 UUID。认证依赖输出 `AccessContext(user_id, workspace_id, role, credential_id)`，角色检查在后端完成，不能依赖 UI 或 UUID 不可猜测性。

## 验收

1. 两个账号创建独立组织，跨租户的知识库、文档、片段、会话、消息、反馈、搜索与导出均不可访问。
2. 完成注册 -> 登录 -> 导入合成资料 -> 问答 -> 证据查看 -> 反馈 -> 知识缺口 -> 邀请成员 -> 角色限制流程；刷新后会话可恢复。
3. 过期/撤销令牌、CSRF、重复邀请、移除成员、最后 owner、伪造组织 ID、批量跨租户请求均有测试。
4. 配额并发、每月周期、失败回退、webhook 伪造/重放/乱序、付款失败与订阅取消均有验证。
5. 无证据时模型调用数为零；上游超时、断线、半包、异常和取消不产生假完成结果。
6. 导入任务可恢复，失败记录保留；迁移对新库和旧结构均可重复执行。
7. 指标与已知排名一致，未标注不显示虚构效果；报告真实测试覆盖率、浏览器验证与未验证的部署/模型路径。
8. 基于 GitHub 最新 main 创建 `codex/enterprise-knowledge-saas`，通过验证后更新 GitHub并创建 draft PR。源代码交付完成不等于已完成真实支付开户、收款或生产上线。

## 官方参考

- [Dify 知识管线](https://www.dify.ai/rag)、[AnythingLLM 权限](https://docs.anythingllm.com/features/security-and-access)：基础 RAG 与团队权限已是现有产品能力；本项目差异化定位是产品判断，效果需通过客户数据验证。
- [Stripe Checkout](https://docs.stripe.com/api/checkout/sessions)、[Webhook 签名](https://docs.stripe.com/webhooks/signature)：支付接入按官方协议实现。
- [FastAPI 依赖](https://fastapi.tiangolo.com/tutorial/dependencies/global-dependencies/)、[Python hashlib](https://docs.python.org/3.11/library/hashlib.html)：权限注入与密码摘要采用已有运行时能力。
