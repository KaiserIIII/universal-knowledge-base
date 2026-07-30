"""Runtime settings exposed to the local Web UI."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body
from pydantic import ValidationError as PydanticValidationError

from ..config import EDITABLE_SETTING_FIELDS, get_settings, public_settings, update_settings
from ..exceptions import ValidationError

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])
RESTART_REQUIRED_FIELDS = {"database_url", "chroma_persist_dir", "chroma_collection_name", "upload_temp_dir"}


@router.get("", summary="读取 Web UI 设置")
async def get_runtime_settings() -> Dict[str, Any]:
    return {
        "settings": public_settings(),
        "editable": sorted(EDITABLE_SETTING_FIELDS),
        "restart_required": sorted(RESTART_REQUIRED_FIELDS),
    }


@router.patch("", summary="保存 Web UI 设置")
async def patch_runtime_settings(
    payload: Dict[str, Any] = Body(..., description="需要更新的设置字段")
) -> Dict[str, Any]:
    try:
        settings = update_settings(payload)
    except (ValueError, PydanticValidationError) as error:
        raise ValidationError("Invalid settings", {"error": str(error)}) from error
    if set(payload) & {
        "embedding_model_name", "embedding_device", "embedding_batch_size",
        "reranker_model_name", "use_reranker", "bm25_k1", "bm25_b",
    }:
        # Rebuild local retrieval components on the next request so changes
        # take effect without restarting the service.
        from ..rag_engine import get_rag_engine
        engine = get_rag_engine()
        engine.cfg = get_settings()
        await engine.close()
    return {
        "settings": settings,
        "editable": sorted(EDITABLE_SETTING_FIELDS),
        "restart_required": sorted(set(payload) & RESTART_REQUIRED_FIELDS),
    }
