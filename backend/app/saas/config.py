from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from app.config import APP_DIR, Settings


class AppSettings(Settings):
    # The runtime entrypoint may explicitly select its own .env. Never use the
    # legacy settings singleton or persisted UI settings when constructing SaaS.
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")
    database_url: str = f"sqlite+aiosqlite:///{(APP_DIR / 'data/saas/knowledge.db').as_posix()}"
    chroma_persist_dir: str = str(APP_DIR / "data/saas/chroma")
    upload_temp_dir: str = str(APP_DIR / "data/saas/uploads")
    registration_enabled: bool = True
    session_cookie_name: str = "knowledge_session"
    cookie_secure: bool = True
    session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    invite_ttl_seconds: int = Field(default=172800, ge=60, le=604800)
    api_key_ttl_seconds: int = Field(default=7776000, ge=60, le=31536000)
    public_origin: str = "http://localhost:8000"
    allowed_origins: list[str] = Field(default_factory=list)
    auth_attempt_limit: int = Field(default=10, ge=1, le=1000)
    auth_attempt_window_seconds: int = Field(default=300, ge=1, le=86400)
    auth_attempt_max_buckets: int = Field(default=10000, ge=100, le=1000000)
    chat_timeout_seconds: float = Field(default=60, ge=.05, le=300)
    chat_history_messages: int = Field(default=20, ge=0, le=100)
    chat_history_chars: int = Field(default=12000, ge=0, le=100000)
    chat_context_chars: int = Field(default=24000, ge=1000, le=100000)
    model_allowed_hosts: list[str] = Field(default_factory=list)
    model_allowed_secret_refs: list[str] = Field(default_factory=list)
    model_private_hosts: list[str] = Field(default_factory=list)
    model_allow_http: bool = False
    workflow_timeout_seconds: float = Field(default=180, ge=.05, le=600)
    workflow_output_token_budget: int = Field(default=16384, ge=1, le=49152)
    workflow_lease_seconds: int = Field(default=15, ge=3, le=120)
    ingestion_worker_enabled: bool = True
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    ingestion_lease_seconds: int = Field(default=60, ge=3, le=3600)
    ingestion_poll_seconds: float = Field(default=2, ge=.1, le=60)
    ingestion_job_timeout_seconds: int = Field(default=900, ge=1, le=86400)

    @field_validator("public_origin")
    @classmethod
    def validate_origin(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("public_origin must be an HTTP(S) origin")
        return value.rstrip("/")
