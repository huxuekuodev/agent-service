"""监控接口（/monitor/*）。

- 监控组件配置 CRUD（持久化到 PostgreSQL）
- Ext 槽位含义配置（按 page，用户在监控平台设置）
- 打点数据聚合查询（时间范围 / 粒度 / 统计方式 / 按模型分组）
- 用户 token 消耗汇总（按模型区分）

统一响应信封 {data, msg, status}，见 app/core/response.py。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.log import logger
from app.core.response import BAD_REQUEST, INTERNAL_ERROR, NOT_FOUND, BizError, ok
from app.monitor import query as tracking_query
from app.monitor import store

router = APIRouter(prefix="/monitor", tags=["monitor"])

#: 各业务名称下 Ext 槽位的默认含义（用户可在监控平台覆盖，存 PG）
DEFAULT_FIELD_MEANINGS: dict[str, dict[str, str]] = {
    "call_model": {"p0": "请求总耗时(ms)", "p1": "优化指标", "p2": "辅助指标", "p3": "", "p4": ""},
    "plan": {"p0": "plan_id", "p1": "子任务数", "p2": "action", "p3": "耗时(ms)", "p4": "澄清摘要"},
    "execute": {"p0": "task_id", "p1": "agent", "p2": "状态", "p3": "耗时(ms)", "p4": "工具名"},
    "evaluation": {"p0": "评估器", "p1": "指标", "p2": "得分", "p3": "耗时(ms)", "p4": "passed"},
    "token": {"p0": "总 token", "p1": "输入 token", "p2": "输出 token", "p3": "费用(元)", "p4": ""},
}

_PAGE_LIST = ["call_model", "chat", "plan", "execute", "evaluation", "knowledge", "system", "token"]


def _store_or_500(exc: Exception) -> BizError:
    """把存储层异常转为统一业务错误。"""
    if isinstance(exc, store.MonitorStoreError):
        return BizError(INTERNAL_ERROR, f"监控存储不可用: {exc}")
    logger.warning("监控接口异常: {}", exc)
    return BizError(INTERNAL_ERROR, f"监控接口异常: {exc}")


# ---------------------------------------------------------------------------
# 基础元信息
# ---------------------------------------------------------------------------


@router.get("/pages")
async def list_pages() -> dict[str, Any]:
    """可用业务名称列表（含默认槽位含义）。"""
    return ok(
        {
            "pages": [
                {
                    "page": p,
                    "default_meanings": [{"slot": f"p{i}", "label": DEFAULT_FIELD_MEANINGS.get(p, {}).get(f"p{i}", ""), "description": ""} for i in range(15)],
                }
                for p in _PAGE_LIST
            ]
        }
    )


@router.get("/models")
async def list_models() -> dict[str, Any]:
    """数据日志中出现过的模型角色名（监控过滤用）。"""
    return ok({"models": tracking_query.list_models()})


@router.get("/pages-in-log")
async def list_pages_in_log() -> dict[str, Any]:
    """数据日志中出现过的业务名称。"""
    return ok({"pages": tracking_query.list_pages()})


# ---------------------------------------------------------------------------
# Ext 槽位含义（按 page 配置）
# ---------------------------------------------------------------------------


class FieldMeaningItem(BaseModel):
    slot: str = Field(..., description="槽位名，p0..p14")
    label: str = Field(default="", description="含义/展示名")
    description: str = Field(default="", description="详细说明")


class FieldMeaningsRequest(BaseModel):
    page: str = Field(..., description="业务名称")
    meanings: list[FieldMeaningItem] = Field(default_factory=list)


@router.get("/field-meanings")
async def get_field_meanings(page: str = Query(..., description="业务名称")) -> dict[str, Any]:
    """取某 page 的槽位含义（PG 优先，缺省用默认表）。"""
    try:
        stored = await store.get_field_meanings(page)
    except Exception as exc:
        raise _store_or_500(exc) from exc
    by_slot = {m["slot"]: m for m in stored}
    defaults = DEFAULT_FIELD_MEANINGS.get(page, {})
    meanings = [
        {
            "slot": f"p{i}",
            "label": by_slot.get(f"p{i}", {}).get("label") or defaults.get(f"p{i}", ""),
            "description": by_slot.get(f"p{i}", {}).get("description") or "",
        }
        for i in range(15)
    ]
    return ok({"page": page, "meanings": meanings})


@router.put("/field-meanings")
async def put_field_meanings(req: FieldMeaningsRequest) -> dict[str, Any]:
    """保存某 page 的槽位含义。"""
    try:
        await store.upsert_field_meanings(req.page, [m.model_dump() for m in req.meanings])
    except Exception as exc:
        raise _store_or_500(exc) from exc
    return ok({"page": req.page})


# ---------------------------------------------------------------------------
# 监控组件配置 CRUD
# ---------------------------------------------------------------------------


class ComponentRequest(BaseModel):
    name: str = Field(..., description="组件名")
    page: str = Field(..., description="业务名称")
    metric: str = Field(default="p0", description="监控指标槽位 p0..p14")
    model: str | None = Field(default=None, description="模型角色过滤；空=全部")
    stat: str = Field(default="sum", description="统计方式 sum|avg")
    granularity: str = Field(default="minute", description="展示粒度 minute|hour")
    range_type: str = Field(default="30", description="时间范围 30|7|yesterday|custom")
    start_time: str | None = Field(default=None, description="自定义范围开始（ISO）")
    end_time: str | None = Field(default=None, description="自定义范围结束（ISO）")


class ComponentUpdate(BaseModel):
    name: str | None = None
    page: str | None = None
    metric: str | None = None
    model: str | None = None
    stat: str | None = None
    granularity: str | None = None
    range_type: str | None = None
    start_time: str | None = None
    end_time: str | None = None


@router.get("/components")
async def list_components() -> dict[str, Any]:
    try:
        return ok({"components": await store.list_components()})
    except Exception as exc:
        raise _store_or_500(exc) from exc


@router.post("/components")
async def create_component(req: ComponentRequest) -> dict[str, Any]:
    try:
        cid = await store.create_component(**req.model_dump())
    except Exception as exc:
        raise _store_or_500(exc) from exc
    return ok({"id": cid})


@router.put("/components/{component_id}")
async def update_component(component_id: int, req: ComponentUpdate) -> dict[str, Any]:
    try:
        found = await store.update_component(component_id, **req.model_dump(exclude_none=True))
    except Exception as exc:
        raise _store_or_500(exc) from exc
    if not found:
        raise BizError(NOT_FOUND, f"监控组件不存在: {component_id}")
    return ok({"id": component_id})


@router.delete("/components/{component_id}")
async def delete_component(component_id: int) -> dict[str, Any]:
    try:
        found = await store.delete_component(component_id)
    except Exception as exc:
        raise _store_or_500(exc) from exc
    if not found:
        raise BizError(NOT_FOUND, f"监控组件不存在: {component_id}")
    return ok({"id": component_id})


# ---------------------------------------------------------------------------
# 打点数据聚合查询
# ---------------------------------------------------------------------------


@router.get("/query")
async def query_tracking(
    page: str = Query(..., description="业务名称"),
    metric: str = Query("p0", description="指标槽位 p0..p14"),
    model: str | None = Query(None, description="模型角色过滤"),
    start: str = Query(..., description="开始时间（ISO）"),
    end: str = Query(..., description="结束时间（ISO）"),
    granularity: str = Query("minute", description="minute|hour"),
    stat: str = Query("sum", description="sum|avg"),
    group: str = Query("none", description="none|model"),
) -> dict[str, Any]:
    """按参数聚合查询打点数据。"""
    try:
        result = tracking_query.query_tracking(
            page=page,
            metric=metric,
            model=model,
            start=start,
            end=end,
            granularity=granularity,
            stat=stat,
            group=group,
        )
    except ValueError as exc:
        raise BizError(BAD_REQUEST, str(exc)) from exc
    return ok(result)


# ---------------------------------------------------------------------------
# 用户 token 消耗
# ---------------------------------------------------------------------------


@router.get("/token-usage")
async def user_token_usage(user_id: str = Query(..., description="用户标识（当前为 session_id）")) -> dict[str, Any]:
    """某用户各模型的累计 token 消耗。"""
    try:
        rows = await store.get_user_token_usage(user_id)
    except Exception as exc:
        raise _store_or_500(exc) from exc
    return ok({"user_id": user_id, "usage": rows})
