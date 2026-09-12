"""Token 用量记账：业务库累计（按用户/按模型）+ 打点数据链路。

一次对话请求结束后调用 :func:`record_usage`：

  1. ``messages.token_input / token_output``（**按问题**）：由调用方写入消息行；
  2. ``user_token_usage``（**按用户 × 模型**）：本模块 upsert 累计；
  3. 打点 ``token_usage``（page=token，p0 总量 / p1 输入 / p2 输出 / p3 费用）：
     写入独立数据日志（``logs/tracking.data``），供监控平台的趋势分析消费。

费用按 ``token_pricing``（模型角色 → 输入/输出单价，元 / 1K tokens）计算；
未配置单价的模型按 0 计费，不影响 token 计数。
"""

from __future__ import annotations

from typing import Any

from app.core.log import logger

__all__ = ["record_usage"]


async def record_usage(*, user_id: str, session_id: str, usage: dict[str, Any], model_role: str = "") -> dict[str, Any]:
    """记录一次请求的 token 用量（按用户累计 + 打点），返回写入结果摘要。

    Args:
        user_id: 用户 id（``users.id``；监控按它聚合）。
        session_id: 会话 id（打点里用于串联同一次对话）。
        usage: ``UsageCollector.as_dict()`` 的结果（含 ``by_model`` 明细）。
        model_role: 主模型角色（写入打点 ``model`` 字段）。

    Returns:
        ``{"persisted": bool, "cost": float, "by_model": {...}}``；存储不可用时
        ``persisted=False``（打点仍会尝试写入，用量统计不阻塞对话）。
    """
    by_model: dict[str, dict[str, Any]] = dict(usage.get("by_model") or {})
    if not by_model:
        return {"persisted": False, "cost": 0.0, "by_model": {}}

    persisted = False
    try:
        from app.monitor import store as monitor_store

        if monitor_store.is_available():
            for model, item in by_model.items():
                await monitor_store.upsert_user_token_usage(
                    user_id=user_id,
                    model=model,
                    input_tokens=int(item.get("input_tokens") or 0),
                    output_tokens=int(item.get("output_tokens") or 0),
                    total_tokens=int(item.get("total_tokens") or 0),
                    cost=float(item.get("cost") or 0.0),
                )
            persisted = True
    except Exception as exc:
        logger.warning("用户 token 累计写入失败（不影响对话）: {}", exc)

    await _emit_tracking(user_id=user_id, session_id=session_id, usage=usage, model_role=model_role)
    return {"persisted": persisted, "cost": float(usage.get("cost") or 0.0), "by_model": by_model}


async def _emit_tracking(*, user_id: str, session_id: str, usage: dict[str, Any], model_role: str) -> None:
    """把用量写入打点数据日志（每个模型一条）。"""
    try:
        from app.core.tracking import TrackingPage, TrackingSource, TrackingType
        from app.core.tracking.tracker import track

        for model, item in (usage.get("by_model") or {}).items():
            await track(
                TrackingType.TOKEN_USAGE,
                TrackingPage.TOKEN,
                source=TrackingSource.SERVER,
                model=model or model_role,
                p0=str(item.get("total_tokens") or 0),
                p1=str(item.get("input_tokens") or 0),
                p2=str(item.get("output_tokens") or 0),
                p3=str(item.get("cost") or 0),
                p4=user_id,
                p5=session_id,
                p6=str(usage.get("llm_calls") or 0),
                p7=str(usage.get("cache_read_tokens") or 0),
            )
    except Exception as exc:
        logger.warning("用量打点写入失败（不影响对话）: {}", exc)
