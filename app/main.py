"""Deer Agent Service — FastAPI 入口。

独立 agent 服务：基于 LangGraph planner-execute 模式，
提供会话管理 + SSE 流式对话接口。

资源生命周期用 FastAPI lifespan 管理（替代已弃用的 @app.on_event）：
  - startup: 创建 AgentService（打开 postgres 连接池 + setup 建表），注入 app.state
  - shutdown: 释放连接池

启动:
    uv run uvicorn app.main:app --reload --port 8001
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 加载环境变量（必须在导入 app.config 之前）
load_dotenv()

from app.core.log import logger  # noqa: E402
from app.core.response import BAD_REQUEST, INTERNAL_ERROR, NOT_FOUND, BizError, err  # noqa: E402
from app.routers import health, knowledge, sessions  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用 lifespan：管理 AgentService / 知识库摄取服务资源生命周期。"""
    from app.agent_service import AgentService
    from app.config import get_app_config
    from app.rag.ingest_service import KnowledgeIngestService

    # 进入服务生命周期（打开 postgres 连接池 + setup 建表；memory 直接可用）
    service = await AgentService().__aenter__()
    app.state.agent_service = service
    logger.info("Deer Agent Service 启动，AgentService 已初始化")

    # 知识库摄取服务（语雀 → 分块 → 图片转文字 → ES；独立于主对话 Agent）
    # 手动接口可用性取决于 yuque.enabled；自动同步再叠加 ingest.enabled + auto_interval_seconds。
    ingest_service: KnowledgeIngestService | None = None
    config = get_app_config()
    if config.ingest.enabled or config.yuque.enabled:
        try:
            ingest_service = KnowledgeIngestService(app_config=config)
            await ingest_service.start_auto_sync()
            app.state.knowledge_ingest_service = ingest_service
            logger.info("知识库摄取服务已初始化（自动同步: {}）", "开" if ingest_service.auto_task_running else "关")
        except Exception as exc:
            logger.warning("知识库摄取服务初始化失败（跳过）: {}", exc)
            ingest_service = None
    else:
        logger.info("知识库摄取服务未启用（config.ingest.enabled=false 且 config.yuque.enabled=false）")

    try:
        yield
    finally:
        # 释放资源（关闭 postgres 连接池 + 摄取服务）
        await service.__aexit__(None, None, None)
        if ingest_service is not None:
            try:
                await ingest_service.aclose()
            except Exception as exc:
                logger.warning("知识库摄取服务关闭异常: {}", exc)
        logger.info("Deer Agent Service 关闭，AgentService 已释放")


app = FastAPI(
    title="Deer Agent Service",
    description="独立 Agent 服务：LangGraph planner-execute 模式",
    version="0.1.0",
    lifespan=lifespan,
)

# 前后端分离：允许 Vite 开发服务器（5173）跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(knowledge.router)


# ---------------------------------------------------------------------------
# 全局异常处理器：统一转为 {data, msg, status} 信封，HTTP 200
# ---------------------------------------------------------------------------
@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    """业务异常：透传 status / msg。"""
    return JSONResponse(status_code=200, content=err(exc.status, exc.msg, exc.data))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """请求参数校验失败。"""
    return JSONResponse(status_code=200, content=err(BAD_REQUEST, "请求参数错误"))


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    """HTTP 异常（含未匹配路由 404）：转为统一信封。"""
    status = NOT_FOUND if exc.status_code == 404 else BAD_REQUEST
    return JSONResponse(status_code=200, content=err(status, str(exc.detail)))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """未捕获异常：统一兜底，避免 500 泄露堆栈。"""
    logger.exception("未处理的异常: {}", exc)
    return JSONResponse(status_code=200, content=err(INTERNAL_ERROR, "服务内部错误"))
