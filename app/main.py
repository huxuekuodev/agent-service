"""Deer Agent Service — FastAPI 入口。

独立 agent 服务：基于 LangGraph planner-execute 模式，
提供会话管理 + SSE 流式对话接口。

启动:
    uv run uvicorn app.main:app --reload --port 8001
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI

# 加载环境变量（必须在导入 app.config 之前）
load_dotenv()

from app.core.log import logger  # noqa: E402
from app.routers import health, sessions  # noqa: E402

# 全局 AgentService 实例（由 startup/shutdown 管理生命周期）
_service = None

app = FastAPI(
    title="Deer Agent Service",
    description="独立 Agent 服务：LangGraph planner-execute 模式",
    version="0.1.0",
)

# 注册路由
app.include_router(health.router)
app.include_router(sessions.router)


@app.on_event("startup")
async def startup() -> None:
    logger.info("Deer Agent Service 启动")
    # 进入服务生命周期（打开 postgres 连接池 + setup 建表；memory 直接可用）
    from app.agent_service import AgentService

    global _service
    _service = await AgentService().__aenter__()


@app.on_event("shutdown")
async def shutdown() -> None:
    logger.info("Deer Agent Service 关闭")
    global _service
    if _service is not None:
        await _service.__aexit__(None, None, None)
        _service = None
