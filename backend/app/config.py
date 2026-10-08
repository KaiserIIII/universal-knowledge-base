"""
知序 — 全局配置中心（本地嵌入式服务）
基于 pydantic-settings，支持 .env 文件和 OS 环境变量覆盖
"""
from pathlib import Path
from typing import Any, Dict, Optional
import json
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).resolve().parent
PERSISTED_SETTINGS_FILE = APP_DIR / "data" / "settings.json"


class Settings(BaseSettings):
    """应用配置单例"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 应用基础 ──────────────────────────────────
    app_name: str = "Zhixu · Enterprise Knowledge"
    app_version: str = "3.1.0"
    debug: bool = False

    # ── 数据库 (SQLite) ───────────────────────────
    database_url: str = "sqlite+aiosqlite:///data/knowledge_base.db"

    # ── ChromaDB (嵌入式向量库) ────────────────────
    chroma_persist_dir: str = "data/chroma_db"
    chroma_collection_name: str = "enterprise_kb_vectors"

    # ── 本地 Embedding 模型 ───────────────────────
    embedding_model_name: str = "BAAI/bge-small-zh-v1.5"
    # 可选更强模型: "BAAI/bge-m3" (1024维, 需更多内存)
    embedding_device: str = "cpu"  # "cpu" | "cuda" | "mps"
    embedding_batch_size: int = 32

    # ── 本地 Reranker 模型(可选) ──────────────────
    reranker_model_name: str = "BAAI/bge-reranker-v2-m3"
    use_reranker: bool = False  # 默认关(吃内存), 需要时在设置里开

    # ── BM25 参数 ─────────────────────────────────
    bm25_k1: float = 1.5
    bm25_b: float = 0.75

    # ── 大模型 API (DeepSeek / OpenAI 兼容) ────────
    llm_api_url: str = "https://api.deepseek.com/v1/chat/completions"
    llm_api_key: Optional[str] = None
    llm_model: str = "deepseek-chat"

    @field_validator("llm_api_url")
    @classmethod
    def normalize_llm_api_url(cls, value: str) -> str:
        value = value.strip()
        if "api.deepseek.com" in value and value.rstrip("/").endswith("/anthropic"):
            return value.rstrip("/")[:-len("/anthropic")] + "/v1/chat/completions"
        return value

    # ── 文档处理 ──────────────────────────────────
    chunk_default_size: int = 1000
    chunk_default_overlap: int = 200
    max_upload_size_mb: int = 50
    max_files_per_batch: int = 20
    upload_temp_dir: str = "data/uploads"

    # ── 检索默认值 ────────────────────────────────
    search_default_top_k: int = 5
    search_default_alpha: float = 0.5
    search_default_threshold: float = 0.4

    # ── 高精度文档解析（失败时自动回退至本地轻量解析器）───────────
    use_unstructured: bool = True
    system_prompt: str = ""

    @field_validator("database_url")
    @classmethod
    def resolve_sqlite_database(cls, value: str) -> str:
        """Keep relative SQLite paths stable regardless of the launch directory."""
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if not value.startswith(prefix):
                continue
            database_path = value[len(prefix):]
            if database_path == ":memory:" or database_path.startswith("file:"):
                return value
            path = Path(database_path)
            if not path.is_absolute():
                path = (APP_DIR / path).resolve()
            return f"{prefix}{path.as_posix()}"
        return value

    @field_validator("chroma_persist_dir", "upload_temp_dir")
    @classmethod
    def resolve_local_data_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = (APP_DIR / path).resolve()
        return str(path)


_settings: Optional[Settings] = None

# Paths and connection strings stay in .env; these fields are safe to adjust
# from the local Web UI while the service is running.
EDITABLE_SETTING_FIELDS = {
    "app_name", "app_version", "debug",
    "database_url", "chroma_persist_dir", "chroma_collection_name",
    "embedding_model_name", "embedding_device", "embedding_batch_size",
    "reranker_model_name", "use_reranker", "bm25_k1", "bm25_b",
    "llm_api_url", "llm_api_key", "llm_model",
    "chunk_default_size", "chunk_default_overlap", "max_upload_size_mb", "max_files_per_batch",
    "search_default_top_k", "search_default_alpha", "search_default_threshold",
    "use_unstructured", "system_prompt", "upload_temp_dir",
}


def _read_persisted_settings() -> Dict[str, Any]:
    try:
        if not PERSISTED_SETTINGS_FILE.exists():
            return {}
        payload = json.loads(PERSISTED_SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_persisted_settings(values: Dict[str, Any]) -> None:
    PERSISTED_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PERSISTED_SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(values, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(PERSISTED_SETTINGS_FILE)


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        base = Settings()
        overrides = {
            key: value
            for key, value in _read_persisted_settings().items()
            if key in EDITABLE_SETTING_FIELDS
        }
        if overrides:
            try:
                _settings = Settings.model_validate({**base.model_dump(), **overrides})
            except Exception:
                # A manually edited settings file must never prevent startup.
                _settings = base
        else:
            _settings = base
    return _settings


def public_settings() -> Dict[str, Any]:
    settings = get_settings()
    result = {
        field: getattr(settings, field)
        for field in EDITABLE_SETTING_FIELDS
        if field != "llm_api_key"
    }
    result["llm_api_key_configured"] = bool(settings.llm_api_key)
    return result


def update_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    unknown = set(values) - EDITABLE_SETTING_FIELDS
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    validated = Settings.model_validate({**settings.model_dump(), **values})
    if validated.chunk_default_overlap >= validated.chunk_default_size:
        raise ValueError("chunk_default_overlap must be smaller than chunk_default_size")
    if validated.search_default_top_k < 1:
        raise ValueError("search_default_top_k must be at least 1")
    if validated.max_upload_size_mb < 1 or validated.max_files_per_batch < 1:
        raise ValueError("upload limits must be positive")
    settings.__dict__.update(validated.__dict__)
    persisted = {
        field: getattr(settings, field)
        for field in EDITABLE_SETTING_FIELDS
        if getattr(settings, field) is not None
    }
    _write_persisted_settings(persisted)
    return public_settings()
