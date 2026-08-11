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
from fastapi.responses import JSONResponse

# 加载环境变量（必须在导入 app.config 之前）
load_dotenv()

from app.core.log import logger  # noqa: E402
from app.core.response import BAD_REQUEST, INTERNAL_ERROR, NOT_FOUND, BizError, err  # noqa: E402
from app.routers import health, sessions  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用 lifespan：管理 AgentService 资源生命周期。"""
    from app.agent_service import AgentService

    # 进入服务生命周期（打开 postgres 连接池 + setup 建表；memory 直接可用）
    service = await AgentService().__aenter__()
    app.state.agent_service = service
    logger.info("Deer Agent Service 启动，AgentService 已初始化")

    try:
        yield
    finally:
        # 释放资源（关闭 postgres 连接池）
        await service.__aexit__(None, None, None)
        logger.info("Deer Agent Service 关闭，AgentService 已释放")


app = FastAPI(
    title="Deer Agent Service",
    description="独立 Agent 服务：LangGraph planner-execute 模式",
    version="0.1.0",
    lifespan=lifespan,
)

# 注册路由
app.include_router(health.router)
app.include_router(sessions.router)


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
