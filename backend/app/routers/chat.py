"""
对话路由 — SSE 流式输出 + RAG 上下文自动装配
提供:
  - POST /api/v1/chat/completions       SSE 流式对话（支持 RAG 自动检索）
  - POST /api/v1/chat/completions/sync  同步对话（非流式）
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import AsyncGenerator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import get_db
from ..exceptions import NotFoundError, ServiceUnavailableError
from ..models import KnowledgeBase
from ..schemas import (
    ChatRequest,
    ChatMessage,
    AgentSearchRequest,
)
from ..rag_engine import get_rag_engine

logger = logging.getLogger("chat")

router = APIRouter(prefix="/api/v1/chat", tags=["Chat"])


def _upstream_error_text(response: httpx.Response, body: str | None = None) -> str:
    try:
        payload = json.loads(body) if body is not None else response.json()
        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if isinstance(error, dict):
                return str(error.get("message") or error.get("detail") or error)
            return str(error)
    except (ValueError, TypeError):
        pass
    text = body.strip() if body is not None else response.text.strip()
    return text[:500] or response.reason_phrase or f"HTTP {response.status_code}"

# 默认 System Prompt
DEFAULT_SYSTEM_PROMPT = """你是一个专业的知序知识库助手。请严格基于提供的【参考资料】回答用户问题。

回答要求:
1. 如果参考资料中包含答案，请直接引用，并在对应句尾标上上标引用编号 [1], [2] 等。
2. 如果参考资料中没有相关信息，请诚实说明"当前知识库中未找到相关信息"，不要编造。
3. 回答请使用中文，结构清晰，逻辑分明。
4. 如有多个来源支持同一观点，请综合引用。
5. 在不影响准确性的前提下，回答尽量简洁。"""

DEFAULT_PROMPT_TEMPLATE = """【参考资料】
{{context}}

【用户问题】
{{query}}

请基于以上参考资料回答用户问题。务必在引用具体参考内容时标注上标引用编号。"""


# ══════════════════════════════════════════════════════════════════
# 内部辅助方法
# ══════════════════════════════════════════════════════════════════
async def _assemble_rag_context(
    query: str,
    kb_ids: list,
    top_k: int = 4,
    hybrid_alpha: float = 0.6,
    score_threshold: float = 0.4,
    enable_reranker: bool = False,
) -> tuple[str, list]:
    """
    执行 RAG 检索并组装上下文文本。
    返回: (context_text, citations_list)
    """
    engine = get_rag_engine()

    matches = await engine.hybrid_search(
        query=query,
        kb_ids=[str(k) for k in kb_ids],
        top_k=top_k,
        score_threshold=score_threshold,
        hybrid_alpha=hybrid_alpha,
        enable_reranker=enable_reranker,
    )

    if not matches:
        return "", []

    # 构建带编号的上下文
    context_parts = []
    citations = []
    for i, m in enumerate(matches):
        ref_num = i + 1
        context_parts.append(
            f"[{ref_num}] (来源: {m.filename})\n{m.content}"
        )
        citations.append({
            "ref": ref_num,
            "chunk_id": str(m.chunk_id),
            "filename": m.filename,
            "content": m.content[:300],  # 截断预览
            "score": m.score,
        })

    context_text = "\n\n---\n\n".join(context_parts)
    return context_text, citations


def _build_messages(
    query: str,
    context_text: str,
    prompt_template: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> list[dict]:
    """构建发送给 LLM 的 messages 数组"""
    template = prompt_template or DEFAULT_PROMPT_TEMPLATE
    user_content = template.replace("{{context}}", context_text).replace("{{query}}", query)

    configured_system_prompt = get_settings().system_prompt
    return [
        {"role": "system", "content": system_prompt or configured_system_prompt or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def _ensure_kbs_exist(kb_ids: list[str], db: AsyncSession) -> None:
    result = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(kb_ids),
            KnowledgeBase.is_deleted == False,
        )
    )
    found = set(result.scalars().all())
    missing = [kb_id for kb_id in kb_ids if kb_id not in found]
    if missing:
        raise NotFoundError("KnowledgeBase", ", ".join(missing))


# ══════════════════════════════════════════════════════════════════
# SSE 流式对话
# ══════════════════════════════════════════════════════════════════
@router.post("/completions", summary="大模型对话（SSE 流式输出 + RAG 自动检索）")
async def chat_completions(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """
    RAG 增强的流式对话接口。

    流程:
      1. 接收用户 query + kb_ids
      2. 自动调用混合检索获取相关片段
      3. 将片段组装为带引用编号的上下文
      4. 替换 Prompt 模板中的 {{context}} 和 {{query}}
      5. 以 SSE (Server-Sent Events) 流式返回 LLM 输出
      6. 在最终消息中附带 citations 引用数组

    SSE 事件类型:
      - data: {"delta": "文本片段"}           流式增量
      - data: {"finish_reason": "stop"}       正常结束
      - data: {"error": "错误信息"}            异常
      - data: {"citations": [...]}            引用来源（在 finish 前发送）
    """
    cfg = get_settings()

    # 0. 参数校验
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    if not request.kb_ids:
        raise HTTPException(status_code=400, detail="At least one kb_id is required")
    kb_ids_str = [str(k) for k in request.kb_ids]
    await _ensure_kbs_exist(kb_ids_str, db)
    if not cfg.llm_api_key or cfg.llm_api_key.startswith("sk-your-"):
        raise ServiceUnavailableError("LLM", "LLM_API_KEY is not configured")

    start_time = time.monotonic()

    # 1. RAG 检索
    try:
        context_text, citations = await _assemble_rag_context(
            query=request.query,
            kb_ids=kb_ids_str,
            top_k=request.top_k,
            hybrid_alpha=request.hybrid_alpha,
            score_threshold=request.score_threshold,
            enable_reranker=request.enable_reranker,
        )
    except Exception as error:
        logger.exception("RAG search failed")
        raise ServiceUnavailableError("retrieval", str(error)) from error

    # 2. 组装消息
    messages = _build_messages(
        query=request.query,
        context_text=context_text,
        prompt_template=request.prompt_template,
        system_prompt=request.system_prompt,
    )

    search_time = time.monotonic() - start_time
    logger.info(
        f"RAG context assembled: {len(citations)} sources, "
        f"search latency: {search_time*1000:.0f}ms"
    )

    # 3. 流式代理到 LLM
    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器"""
        full_content = ""

        try:
            if citations:
                citations_payload = json.dumps(
                    {
                        "citations": citations,
                        "context_length": len(context_text),
                    },
                    ensure_ascii=False,
                    default=str,
                )
                yield f"data: {citations_payload}\n\n"

            upstream_url = cfg.llm_api_url
            headers = {
                "Authorization": f"Bearer {cfg.llm_api_key or ''}",
                "Content-Type": "application/json",
            }
            req_payload = {
                "model": cfg.llm_model,
                "messages": messages,
                "stream": True,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }

            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                chunk_index = 0
                async with client.stream(
                    "POST",
                    upstream_url,
                    json=req_payload,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_body = ""
                        async for err_chunk in response.aiter_bytes():
                            error_body += err_chunk.decode(errors="replace")
                            if len(error_body) > 1000:
                                break
                        error_msg = json.dumps({
                            "error": True,
                            "code": response.status_code,
                            "message": f"LLM upstream error: {_upstream_error_text(response, error_body)}",
                        })
                        yield f"data: {error_msg}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue

                        data_str = line[5:].lstrip()

                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            choices = data.get("choices", [])
                            if not choices:
                                continue

                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            finish_reason = choices[0].get("finish_reason")

                            if content:
                                full_content += content
                                # 以 OpenAI 兼容 SSE 格式转发
                                sse_data = json.dumps({
                                    "id": f"chatcmpl-{chunk_index}",
                                    "object": "chat.completion.chunk",
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": content},
                                        "finish_reason": finish_reason,
                                    }],
                                }, ensure_ascii=False)
                                yield f"data: {sse_data}\n\n"
                                chunk_index += 1

                            if finish_reason:
                                finish_data = json.dumps({
                                    "id": f"chatcmpl-{chunk_index}",
                                    "object": "chat.completion.chunk",
                                    "choices": [{
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": finish_reason,
                                    }],
                                }, ensure_ascii=False)
                                yield f"data: {finish_data}\n\n"
                                break

                        except json.JSONDecodeError:
                            continue

            # 发送完成信号
            yield "data: [DONE]\n\n"

            total_time = time.monotonic() - start_time
            logger.info(
                f"Chat completed: {len(full_content)} chars, "
                f"{len(citations)} citations, "
                f"total: {total_time*1000:.0f}ms "
                f"(search: {search_time*1000:.0f}ms)"
            )

        except httpx.TimeoutException:
            error_payload = json.dumps({
                "error": True,
                "message": "LLM upstream timed out after 120s",
            })
            yield f"data: {error_payload}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception(f"Chat SSE error: {e}")
            error_payload = json.dumps({
                "error": True,
                "message": str(e),
            })
            yield f"data: {error_payload}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁止 Nginx 缓冲
            "X-RAG-Citations": str(len(citations)),
            "X-RAG-Search-Time-Ms": str(int(search_time * 1000)),
        },
    )


# ══════════════════════════════════════════════════════════════════
# 同步对话（非流式）
# ══════════════════════════════════════════════════════════════════
@router.post("/completions/sync", summary="同步对话（非流式，等待完整响应）")
async def chat_completions_sync(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    非流式版本的对话接口，返回完整 JSON 响应。
    适用于 API 集成、脚本调用、不支持 SSE 的客户端。
    """
    cfg = get_settings()

    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    if not request.kb_ids:
        raise HTTPException(status_code=400, detail="At least one kb_id is required")
    kb_ids_str = [str(k) for k in request.kb_ids]
    await _ensure_kbs_exist(kb_ids_str, db)
    if not cfg.llm_api_key or cfg.llm_api_key.startswith("sk-your-"):
        raise ServiceUnavailableError("LLM", "LLM_API_KEY is not configured")

    start_time = time.monotonic()

    # 1. RAG 检索
    try:
        context_text, citations = await _assemble_rag_context(
            query=request.query,
            kb_ids=kb_ids_str,
            top_k=request.top_k,
            hybrid_alpha=request.hybrid_alpha,
            score_threshold=request.score_threshold,
            enable_reranker=request.enable_reranker,
        )
    except Exception as error:
        logger.exception("RAG search failed")
        raise ServiceUnavailableError("retrieval", str(error)) from error

    # 2. 组装消息
    messages = _build_messages(
        query=request.query,
        context_text=context_text,
        prompt_template=request.prompt_template,
        system_prompt=request.system_prompt,
    )

    # 3. 调用 LLM（非流式）
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(
                cfg.llm_api_url,
                json={
                    "model": cfg.llm_model,
                    "messages": messages,
                    "stream": False,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {cfg.llm_api_key or ''}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            llm_result = response.json()

        answer = llm_result["choices"][0]["message"]["content"]
        usage = llm_result.get("usage", {})

        total_time = int((time.monotonic() - start_time) * 1000)

        return {
            "answer": answer,
            "citations": citations,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "context": {
                "source_count": len(citations),
                "context_chars": len(context_text),
            },
            "latency_ms": total_time,
        }

    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"LLM upstream error: {_upstream_error_text(e.response)}",
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LLM upstream timed out")
