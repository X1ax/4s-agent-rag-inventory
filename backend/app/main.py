from contextlib import asynccontextmanager
from uuid import uuid4
import sqlite3

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.app.api import router
from backend.app.business_api import router as business_router
from backend.app.fitment_api import router as fitment_router
from backend.app.config import Settings, get_settings
from backend.app.db import make_engine
from backend.app.knowledge_api import KnowledgeRuntime, router as knowledge_router
from backend.app.knowledge import ROOT
from backend.app.assistant_store import AssistantStore
from backend.app.assistant_api import router as assistant_router
from backend.app.agent_proposals import ApprovalRuntime
from backend.app.business_issues import BusinessIssue
from backend.app.media import router as media_router
from backend.app.knowledge_uploads import router as submissions_router
from backend.app.reviews import router as reviews_router
from backend.app.pricing_api import router as pricing_router
from backend.app.profits import router as profits_router

PROJECT_NAME = "基于Agent与RAG的4S店备件智能库存管理系统"


def create_app(settings: Settings | None = None, *, knowledge_root=ROOT) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            engine = make_engine(settings)
        except ValueError:
            engine = None
        application.state.engine = engine
        application.state.settings = settings
        application.state.approvals = ApprovalRuntime(engine, knowledge_root / 'data/agent-checkpoints.sqlite3') if engine is not None else None
        application.state.knowledge = KnowledgeRuntime(settings, knowledge_root)
        application.state.knowledge.start()
        application.state.assistant = AssistantStore(knowledge_root)
        try:
            application.state.assistant.start(recover=application.state.knowledge.index is not None)
        except (OSError, sqlite3.Error):
            application.state.assistant = None
        try:
            yield
        finally:
            if application.state.approvals is not None:
                application.state.approvals.close()
            application.state.knowledge.close()
            if engine is not None:
                engine.dispose()

    application = FastAPI(title=PROJECT_NAME, version="0.1.0", lifespan=lifespan)
    application.include_router(router)
    application.include_router(business_router)
    application.include_router(fitment_router)
    application.include_router(knowledge_router)
    application.include_router(assistant_router)
    application.include_router(media_router)
    application.include_router(submissions_router)
    application.include_router(reviews_router)
    application.include_router(pricing_router)
    application.include_router(profits_router)

    @application.exception_handler(BusinessIssue)
    async def business_issue(request, error):
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail, 'business_issue': error.issue})

    @application.exception_handler(IntegrityError)
    async def integrity_error(request, error):
        return JSONResponse(status_code=409, content={"detail": "编码或请求标识重复，或记录仍被其他业务引用"})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "数据库操作失败，请检查服务与迁移状态"})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Validation errors must never echo passwords or other raw request values.
        details = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()]
        content = {"detail": details}
        if request.url.path.startswith(("/api/knowledge/", "/api/assistant/")):
            content.update(error_code="invalid_query", request_id=uuid4().hex)
        return JSONResponse(status_code=422, content=content)

    @application.get("/api/health", tags=["运行状态"])
    def health():
        return {"status": "ok", "service": PROJECT_NAME}

    @application.get("/api/ready", tags=["运行状态"])
    def ready():
        engine = application.state.engine
        if engine is not None:
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                return {"status": "ok", "database": "connected"}
            except SQLAlchemyError:
                pass
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unavailable"},
        )

    return application


app = create_app()
