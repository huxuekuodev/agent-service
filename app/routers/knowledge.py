"""知识库摄取接口（语雀 → 分块 → 图片转文字 → ES）。

独立于主对话 Agent 的知识库更新通道，提供：

  - POST /knowledge/sync          手动触发一次全量摄取（同步等待完成）
  - GET  /knowledge/sync/status   查看最近一次同步结果与自动同步状态

所有接口统一返回信封 ``{data, msg, status}``（见 app/core/response.py）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.config import get_app_config
from app.core.log import logger
from app.core.response import INTERNAL_ERROR, SERVICE_NOT_READY, BizError, err, ok
from app.rag.ingest_service import KnowledgeIngestService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class SyncRequest(BaseModel):
    namespaces: list[str] | None = Field(default=None, description="可选，需要同步的 namespace 白名单；缺省用配置")
    reindex: bool | None = Field(default=None, description="可选，是否重建 ES 索引；缺省用配置")


def get_ingest_service(request: Request) -> KnowledgeIngestService:
    """从 app.state 获取摄取服务（由 FastAPI lifespan 注入）。"""
    service = getattr(request.app.state, "knowledge_ingest_service", None)
    if service is None:
        raise BizError(SERVICE_NOT_READY, "知识库摄取服务未初始化：请通过 FastAPI app 启动（uvicorn app.main:app）")
    return service


@router.post("/sync")
async def sync_knowledge(req: SyncRequest, svc: KnowledgeIngestService = Depends(get_ingest_service)) -> dict[str, Any]:
    """手动触发一次全量知识库摄取（同步等待完成）。"""
    try:
        stats = await svc.ingest_all(namespaces=req.namespaces, reindex=req.reindex)
        return ok(stats.model_dump())
    except BizError:
        raise
    except Exception as exc:
        logger.exception("知识库摄取失败: {}", exc)
        return err(INTERNAL_ERROR, f"知识库摄取失败: {exc}")


@router.get("/sync/status")
async def sync_status(svc: KnowledgeIngestService = Depends(get_ingest_service)) -> dict[str, Any]:
    """查看最近一次同步结果与自动同步状态。"""
    cfg = get_app_config()
    return ok(
        {
            "last_run": svc.last_run,
            "auto_sync_enabled": cfg.ingest.auto_interval_seconds > 0 and cfg.yuque.enabled,
            "auto_interval_seconds": cfg.ingest.auto_interval_seconds,
            "running": svc.auto_task_running,
        }
    )
