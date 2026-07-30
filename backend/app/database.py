"""
企业级知识库 — 数据库会话管理 (SQLite + aiosqlite)
零依赖嵌入式版本 — 无需 PostgreSQL
"""
import os
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from .config import get_settings

_settings = get_settings()

# ── 自动创建 SQLite 数据库的父目录（防止目录不存在导致启动崩溃）──
_db_url = _settings.database_url
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_is_sqlite = _db_url.startswith("sqlite")
if _is_sqlite:
    # 从 URL 中提取文件路径：sqlite+aiosqlite:///data/baichuan_kb.db → data/baichuan_kb.db
    _db_path = _db_url.split("///")[-1]
    _db_dir = os.path.dirname(os.path.abspath(_db_path))
    os.makedirs(_db_dir, exist_ok=True)

_is_memory_sqlite = _is_sqlite and ":memory:" in _db_url
_engine_options = {
    "echo": _settings.debug,
    "pool_pre_ping": True,
}
if _is_sqlite:
    _engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    if not _is_memory_sqlite:
        _engine_options["pool_size"] = 1
else:
    _engine_options.update(
        pool_size=20,
        max_overflow=40,
        pool_recycle=1800,
    )

engine = create_async_engine(_db_url, **_engine_options)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# SQLite 不支持 PostgreSQL 的 UUID / JSONB，添加适配
if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """启用 WAL 模式、外键约束和繁忙等待。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入: 获取异步数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """健康检查: 测试数据库是否可达"""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
            return True
    except Exception:
        return False
