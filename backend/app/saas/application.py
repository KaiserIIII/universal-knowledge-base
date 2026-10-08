from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from . import models  # register additive identity tables in the shared metadata
from . import billing_models
from .auth import router as auth_router
from .billing import router as billing_router
from .config import AppSettings
from .organizations import keys_router, router as organizations_router
from .security import AuthThrottle, hash_password, new_token
from .payments import StripePayment
from .quotas import check_member_quota
from .knowledge import router as knowledge_router
from .ingestion import IngestionWorker
from .retrieval import RAGRetriever
from .conversations import router as conversations_router
from .chat import ChatService, recover_failed_answers, router as chat_router
from .insights import router as insights_router
from .evaluation import router as evaluation_router
from .llm import OpenAICompatibleLLM
from .model_profiles import ModelDispatcher, router as model_profiles_router
from .workflows import router as workflows_router
from .workflow_engine import WorkflowService


def create_app(settings=None, retriever=None, llm=None, payment=None) -> FastAPI:
    settings = settings if settings is not None else AppSettings()

    @asynccontextmanager
    async def lifespan(app):
        if settings.database_url.startswith("sqlite+aiosqlite:///"):
            db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
            if db_path != ":memory:" and not db_path.startswith("file:"):
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(settings.database_url, echo=False)
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine.sync_engine, "connect")
            def sqlite_foreign_keys(connection, record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.close()
        app.state.engine = engine
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False, info={"settings": settings})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        app.state.chat_service = ChatService(app.state.session_factory, settings, app.state.retriever, app.state.model_dispatcher)
        app.state.workflow_service = WorkflowService(app.state.session_factory, settings, app.state.retriever, app.state.model_dispatcher)
        await app.state.workflow_service.start()
        await recover_failed_answers(app.state.session_factory)
        app.state.dummy_password_hash = await hash_password(new_token())
        app.state.ingestion_worker = IngestionWorker(app.state.session_factory, settings, app.state.retriever)
        app.state.ingestion_worker.start()
        try:
            yield
        finally:
            await app.state.workflow_service.stop()
            await app.state.ingestion_worker.stop()
            for adapter in (app.state.retriever, app.state.llm, app.state.payment):
                close = getattr(adapter, "close", None)
                if close:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
            await engine.dispose()

    app = FastAPI(title="Enterprise Knowledge SaaS", version="3.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.retriever = retriever if retriever is not None else RAGRetriever(settings)
    app.state.llm = llm if llm is not None else OpenAICompatibleLLM(settings)
    app.state.model_dispatcher = ModelDispatcher(settings, app.state.llm)
    app.state.payment = payment if payment is not None else StripePayment(settings)
    app.state.auth_throttle = AuthThrottle()
    app.state.member_quota_check = check_member_quota

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = {settings.public_origin, *settings.allowed_origins}
            if origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_router)
    app.include_router(organizations_router)
    app.include_router(keys_router)
    app.include_router(billing_router)
    app.include_router(knowledge_router)
    app.include_router(conversations_router)
    app.include_router(chat_router)
    app.include_router(insights_router)
    app.include_router(evaluation_router)
    app.include_router(model_profiles_router)
    app.include_router(workflows_router)

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/ready")
    async def ready():
        try:
            async with asyncio.timeout(2):
                async with app.state.session_factory() as db:
                    await db.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    return app
