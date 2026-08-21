"""打点数据聚合查询（监控数据源 = 打点数据日志文件）。

从 config.yaml ``tracking.output``（默认 logs/tracking.data）逐行读取打点
（每行 ``{ISO 时间} {base64(protobuf)}``），按监控组件参数聚合：

  - 过滤：page / model（模型角色名）/ 时间范围
  - 指标：Ext 的 p0..p14（数值型，字符串按 float 解析，解析失败跳过）
  - 分组：按模型分组对比（group=model）或不分组
  - 粒度：minute（每分钟） / hour（每小时）
  - 统计：sum（和值） / avg（平均值，附带 count）
"""

from __future__ import annotations

import base64
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.tracking.decoder import DecodeError, decode_tracking_content
from app.core.tracking.model import TrackingContent

__all__ = ["query_tracking", "list_pages", "list_models", "data_log_path"]

_EXT_SLOTS = [f"p{i}" for i in range(15)]


def data_log_path() -> str:
    """打点数据日志路径（config.yaml tracking.output）。"""
    try:
        from app.config import get_app_config

        return get_app_config().tracking.output or "logs/tracking.data"
    except Exception:
        return "logs/tracking.data"


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _read_events(
    start: datetime,
    end: datetime,
    *,
    page: str | None = None,
    model: str | None = None,
) -> list[tuple[datetime, TrackingContent]]:
    """读取时间范围内、匹配 page/model 的打点，返回 [(ts, content)]。"""
    events: list[tuple[datetime, TrackingContent]] = []
    path = Path(data_log_path())
    if not path.exists():
        return events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            ts = _parse_ts(parts[0])
            if ts is None or ts < start or ts > end:
                continue
            try:
                content = decode_tracking_content(base64.b64decode(parts[1], validate=True))
            except (ValueError, DecodeError, base64.binascii.Error):
                continue
            if page and content.page != page:
                continue
            if model and content.model != model:
                continue
            events.append((ts, content))
    return events


def _bucket_key(ts: datetime, granularity: str) -> str:
    """时间桶 key：minute → "YYYY-MM-DD HH:MM"，hour → "YYYY-MM-DD HH:00"。"""
    if granularity == "hour":
        return ts.strftime("%Y-%m-%d %H:00")
    return ts.strftime("%Y-%m-%d %H:%M")


def _metric_value(content: TrackingContent, metric: str) -> float | None:
    """取指标数值（Ext 槽位，字符串按 float 解析）。"""
    if metric not in _EXT_SLOTS:
        return None
    raw = content.ext.to_dict().get(metric)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def query_tracking(
    *,
    page: str,
    metric: str = "p0",
    model: str | None = None,
    start: str,
    end: str,
    granularity: str = "minute",
    stat: str = "sum",
    group: str = "none",
) -> dict[str, Any]:
    """按监控组件参数聚合查询打点数据。

    Args:
        page: 业务名称（TrackingPage）。
        metric: 指标槽位（p0..p14）。
        model: 模型角色过滤；None 表示全部。
        start/end: ISO 时间范围。
        granularity: minute | hour。
        stat: sum | avg。
        group: none | model（按模型分组，多序列返回）。

    Returns:
        {"page", "metric", "model", "granularity", "stat", "group",
         "series": [{"bucket", "model", "value", "count"}], "total": {...}}
    """
    start_dt = _parse_ts(start)
    end_dt = _parse_ts(end)
    if start_dt is None or end_dt is None:
        raise ValueError("start/end 必须是 ISO 时间格式（如 2026-08-01T00:00:00+08:00）")
    if stat not in ("sum", "avg"):
        raise ValueError("stat 仅支持 sum / avg")
    if granularity not in ("minute", "hour"):
        raise ValueError("granularity 仅支持 minute / hour")
    if group not in ("none", "model"):
        raise ValueError("group 仅支持 none / model")

    events = _read_events(start_dt, end_dt, page=page, model=model)

    # 按 (bucket, model 或 "") 累计 sum/count
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts, content in events:
        value = _metric_value(content, metric)
        if value is None:
            continue
        key = _bucket_key(ts, granularity)
        g = content.model or "" if group == "model" else ""
        acc[(key, g)].append(value)

    series = [
        {
            "bucket": bucket,
            "model": g,
            "value": round(sum(vals), 4) if stat == "sum" else round(sum(vals) / len(vals), 4),
            "count": len(vals),
        }
        for (bucket, g), vals in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]

    all_values = [v for vals in acc.values() for v in vals]
    total = {
        "count": len(all_values),
        "sum": round(sum(all_values), 4) if all_values else 0.0,
        "avg": round(sum(all_values) / len(all_values), 4) if all_values else 0.0,
    }
    return {
        "page": page,
        "metric": metric,
        "model": model,
        "granularity": granularity,
        "stat": stat,
        "group": group,
        "series": series,
        "total": total,
    }


def list_pages() -> list[str]:
    """数据日志中出现过的业务名称（去重，按出现顺序）。"""
    seen: list[str] = []
    seen_set: set[str] = set()
    path = Path(data_log_path())
    if not path.exists():
        return seen
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            try:
                content = decode_tracking_content(base64.b64decode(parts[1], validate=True))
            except (ValueError, DecodeError, base64.binascii.Error):
                continue
            if content.page and content.page not in seen_set:
                seen_set.add(content.page)
                seen.append(content.page)
    return seen


def list_models() -> list[str]:
    """数据日志中出现过的模型角色名（去重）。"""
    seen: list[str] = []
    seen_set: set[str] = set()
    path = Path(data_log_path())
    if not path.exists():
        return seen
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            try:
                content = decode_tracking_content(base64.b64decode(parts[1], validate=True))
            except (ValueError, DecodeError, base64.binascii.Error):
                continue
            if content.model and content.model not in seen_set:
                seen_set.add(content.model)
                seen.append(content.model)
    return seen
