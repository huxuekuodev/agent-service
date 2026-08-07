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
from fastapi import FastAPI

# 加载环境变量（必须在导入 app.config 之前）
load_dotenv()

from app.core.log import logger  # noqa: E402
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
