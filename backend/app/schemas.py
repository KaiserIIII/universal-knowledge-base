"""
知序知识库 — Pydantic V2 数据校验与 API 契约定义

设计原则:
  1. 严格使用 Pydantic V2 的 model_config = ConfigDict(from_attributes=True)
  2. ORM 转换时不会抛出多余字段异常
  3. 输入 (Create) 与输出 (Response) 分离
  4. Agent 外部接口使用强类型契约 (AgentSearchRequest / AgentSearchResponse)
  5. 全部 UUID 作为主键，杜绝自增 ID 渗透到 API 层
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from .models import DocStatus, KBType


# ══════════════════════════════════════════════════════════════════
# 基础 Mixin
# ══════════════════════════════════════════════════════════════════
class OrmBase(BaseModel):
    """ORM 友好基类: 允许从 SQLAlchemy 对象直接构造 Pydantic 模型"""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class TimestampOut(BaseModel):
    """审计时间戳输出 Mixin"""
    created_at: datetime
    updated_at: datetime


# ══════════════════════════════════════════════════════════════════
# 通用响应
# ══════════════════════════════════════════════════════════════════
class PaginatedResponse(BaseModel):
    """分页响应包装"""
    total: int = Field(..., ge=0, description="总记录数")
    page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, le=100, description="每页条数")
    items: List[Any] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# Workspace — 工作空间
# ══════════════════════════════════════════════════════════════════
class WorkspaceCreate(BaseModel):
    """创建工作空间入参"""
    name: str = Field(
        ..., min_length=1, max_length=128, description="工作空间名称",
        examples=["研发团队"]
    )
    description: Optional[str] = Field(
        None, max_length=512, description="简要说明", examples=["工业PC研发与测试"]
    )
    department: Optional[str] = Field(None, max_length=128, description="所属部门")
    owner_id: Optional[str] = Field(None, max_length=64, description="负责人标识")

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class WorkspaceUpdate(BaseModel):
    """更新工作空间入参（所有字段可选）"""
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)
    department: Optional[str] = Field(None, max_length=128)
    owner_id: Optional[str] = Field(None, max_length=64)


class WorkspaceResponse(OrmBase, TimestampOut):
    """工作空间输出"""
    id: uuid.UUID
    name: str
    description: Optional[str] = None
    department: Optional[str] = None
    owner_id: Optional[str] = None
    is_deleted: bool = False


# ══════════════════════════════════════════════════════════════════
# KnowledgeBase — 知识库实例
# ══════════════════════════════════════════════════════════════════
class KnowledgeBaseCreate(BaseModel):
    """创建知识库入参"""
    name: str = Field(..., min_length=1, max_length=200, description="知识库名称")
    description: Optional[str] = Field(None, description="知识库描述")
    kb_type: KBType = Field(default=KBType.GENERAL, description="分类")
    category_tags: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="自定义标签 (最多20个)",
        examples=[["工业PC", "检测", "BC-300"]],
    )
    avatar: Optional[str] = Field(None, max_length=512, description="图标URL/emoji")

    @field_validator("category_tags")
    @classmethod
    def _check_tags_unique_and_len(cls, v: List[str]) -> List[str]:
        """标签去重且每个不超过 32 字符"""
        seen = set()
        clean: List[str] = []
        for tag in v:
            tag = tag.strip()
            if tag and tag not in seen and len(tag) <= 32:
                clean.append(tag)
                seen.add(tag)
        return clean


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库入参"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    kb_type: Optional[KBType] = None
    category_tags: Optional[List[str]] = None
    avatar: Optional[str] = None


class KnowledgeBaseResponse(OrmBase, TimestampOut):
    """知识库输出"""
    id: uuid.UUID
    name: str
    description: Optional[str] = None
    kb_type: KBType
    category_tags: List[str]
    avatar: Optional[str] = None
    is_deleted: bool = False
    doc_count: int = 0
    chunk_count: int = 0
    total_chars: int = 0


class KnowledgeBaseListResponse(OrmBase):
    """知识库列表项（精简版，不含时间戳）"""
    id: uuid.UUID
    name: str
    kb_type: KBType
    category_tags: List[str]
    avatar: Optional[str] = None
    doc_count: int = 0
    chunk_count: int = 0


# ══════════════════════════════════════════════════════════════════
# Document — 文档
# ══════════════════════════════════════════════════════════════════
class DocumentResponse(OrmBase, TimestampOut):
    """文档输出"""
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    file_hash: str
    file_type: str
    file_size_bytes: int
    status: DocStatus
    error_msg: Optional[str] = None
    word_count: int = 0
    page_count: int = 0
    chunk_count: int = 0


class DocumentUploadResponse(BaseModel):
    """批量上传结果摘要"""
    accepted: int = Field(..., description="成功接收的文件数")
    skipped: int = Field(0, description="因重复而被跳过的文件数")
    documents: List[DocumentResponse] = Field(default_factory=list)


class DocumentDeleteResponse(BaseModel):
    """删除结果"""
    deleted: bool = True
    message: str = "Document deleted successfully"


# ══════════════════════════════════════════════════════════════════
# Chunk — 切片输出
# ══════════════════════════════════════════════════════════════════
class ChunkResponse(OrmBase):
    """文档切片输出"""
    id: uuid.UUID
    doc_id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    metadata_: Dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    created_at: datetime


# ══════════════════════════════════════════════════════════════════
# Agent 检索契约 — 对外开放的标准接口
# ══════════════════════════════════════════════════════════════════
class AgentSearchRequest(BaseModel):
    """
    外部 Agent（Dify / Coze / 自定义 AI 节点）调用的混合检索入参。
    完整的 Pydantic 校验，包含参数范围约束和自定义错误示例。
    """
    query: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="检索查询文本",
        examples=["BC-300 工业PC 的串口管脚定义是什么？"],
    )
    kb_ids: List[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="限定检索的知识库 ID 列表",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="返回的最大片段数",
    )
    score_threshold: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="最低相似度阈值，低于此值的片段将被过滤",
    )
    hybrid_alpha: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="混合检索权重。0.0=纯BM25关键词, 1.0=纯向量语义",
        examples=[0.7],
    )
    enable_reranker: bool = Field(
        default=False,
        description="是否启用重排模型 (BGE-Reranker) 对结果进行二次排序",
    )


class ChunkMatch(BaseModel):
    """单条检索命中结果"""
    chunk_id: uuid.UUID = Field(..., description="片段唯一标识")
    content: str = Field(..., description="片段正文内容")
    filename: str = Field(..., description="来源文档文件名")
    doc_id: uuid.UUID = Field(..., description="来源文档 ID")
    score: float = Field(..., ge=0.0, le=1.0, description="归一化后的综合得分")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="结构化元数据，包含 page_number、kb_id、headings 等",
        examples=[{"page_number": 3, "kb_id": "550e8400-...", "heading": "串口定义"}],
    )


class AgentSearchResponse(BaseModel):
    """Agent 检索接口的标准输出契约"""
    matches: List[ChunkMatch] = Field(default_factory=list)
    total_found: int = Field(..., ge=0, description="命中片段总数")
    latency_ms: int = Field(..., ge=0, description="检索耗时 (毫秒)")
    search_mode: str = Field(
        default="hybrid",
        description="实际使用的检索模式",
        examples=["hybrid", "vector_only", "keyword_only"],
    )


# ══════════════════════════════════════════════════════════════════
# Chat / 对话接口
# ══════════════════════════════════════════════════════════════════
class ChatRequest(BaseModel):
    """对话请求"""
    query: str = Field(..., min_length=1, max_length=4096, description="用户问题")
    kb_ids: List[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="检索时使用的知识库 ID 列表",
    )
    prompt_template: Optional[str] = Field(
        None,
        max_length=8192,
        description="自定义 Prompt 模板。使用 {{context}} 和 {{query}} 作为占位符",
        examples=["基于以下背景：\n{{context}}\n\n请严格回答：{{query}}"],
    )
    system_prompt: Optional[str] = Field(
        None,
        max_length=12000,
        description="自定义 System Prompt；留空时使用服务端安全默认值",
    )
    top_k: int = Field(default=4, ge=1, le=20)
    hybrid_alpha: float = Field(default=0.6, ge=0.0, le=1.0)
    score_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    enable_reranker: bool = False
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=128, le=16384)
    stream: bool = Field(default=True, description="是否使用 SSE 流式输出")

    @field_validator("prompt_template")
    @classmethod
    def validate_prompt_placeholders(cls, value: Optional[str]) -> Optional[str]:
        if value and ("{{context}}" not in value or "{{query}}" not in value):
            raise ValueError("prompt_template must include {{context}} and {{query}}")
        return value


class ChatMessage(BaseModel):
    """SSE 流式传输中的单条消息"""
    role: str = Field(..., description="assistant / system")
    content: str = ""
    finish_reason: Optional[str] = Field(None, description="stop / length / error")
    citations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="引用来源数组，每个元素包含 chunk_id, filename, content 等",
    )


# ══════════════════════════════════════════════════════════════════
# 系统状态
# ══════════════════════════════════════════════════════════════════
class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    version: str
    timestamp: datetime
    db_connected: bool
    chroma_connected: bool
    bm25_connected: bool
    embedding_loaded: bool
    uptime_seconds: float


class StatsResponse(BaseModel):
    """集群知识库统计"""
    kb_count: int
    doc_count: int
    chunk_count: int
    total_chars: int
    doc_by_status: Dict[str, int]
